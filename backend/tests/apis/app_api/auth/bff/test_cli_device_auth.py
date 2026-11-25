"""Tests for the CLI device-authorization routes and the callback's device branch.

Covers the full three-leg flow end to end, plus the invariants that are easy
to regress:

* the device callback path sets **no** cookies (writing them would log the
  browser in as a side effect and make two holders share one refresh token);
* an unapprovable code renders the same page whatever the reason, so the
  endpoint is not a user-code existence oracle;
* pending polls are HTTP 400 with an RFC 8628 `error`, not 200;
* opening the verification link starts nothing: the identity provider is
  contacted, and the grant can be approved, only after an explicit Continue
  posted from the confirmation page (RFC 8628 §3.3.1, §5.4).
"""

from __future__ import annotations

import re
import time
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import cli_routes
from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.cli_routes import router as cli_router
from apis.app_api.auth.bff.cookies import CLI_CONFIRM_COOKIE_NAME, OAUTH_STATE_COOKIE_NAME
from apis.app_api.auth.bff.routes import router as bff_router
from apis.app_api.auth.bff.token_exchange import ExchangeResult
from apis.shared.auth.device_grants.models import (
    GrantStatus,
    hash_device_code,
    normalise_user_code,
)
from apis.shared.auth.device_grants.repository import DeviceGrantRepository
from apis.shared.auth.device_grants.service import DeviceGrantService
from apis.shared import rate_limit
from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

from .conftest import BFF_SESSIONS_TABLE, COGNITO_DOMAIN_URL, make_id_token


#: A multiple of 300 (and so of 60), plus 30.
_FROZEN_RATE_LIMIT_NOW = 1_800_000_030.0


@pytest.fixture(autouse=True)
def _reset_cli_state():
    cli_routes._reset_for_tests()
    yield
    cli_routes._reset_for_tests()


@pytest.fixture
def grants(moto_aws) -> DeviceGrantRepository:
    return DeviceGrantRepository(table_name=BFF_SESSIONS_TABLE)


@pytest.fixture
def cli_app(monkeypatch, moto_aws, codec, repository, grants) -> FastAPI:
    """Both routers mounted, with the device-grant service pre-injected.

    The service singleton lives in `routes`, so injecting it there is what
    both the CLI routes and the callback branch pick up.
    """
    # The limiter counts in fixed windows (`now // window`). A burst test
    # that crosses a window boundary on a slow runner splits its requests
    # across two counters and never sees a 429, so the limiter's clock is
    # held mid-window: 30 s into a 60 s token window and a 300 s
    # authorize/verify window alike.
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: _FROZEN_RATE_LIMIT_NOW))
    bff_routes._repository = repository
    from apis.shared.sessions_bff.cookie import _set_default_codec_for_tests

    _set_default_codec_for_tests(codec)
    monkeypatch.setattr(bff_routes, "resolve_bff_client_secret", lambda **_: "test-client-secret")

    bff_routes._device_grant_service = DeviceGrantService(
        repository=grants,
        session_repository=repository,
        codec=codec,
        verification_uri="http://localhost:8000/auth/cli/verify",
    )

    app = FastAPI()
    app.include_router(bff_router)
    app.include_router(cli_router)
    return app


def _patch_token_exchange(monkeypatch, id_token: str) -> MagicMock:
    mock = AsyncMock(
        return_value=ExchangeResult(
            access_token="access.tok",
            refresh_token="refresh.tok",
            id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        )
    )
    monkeypatch.setattr(bff_routes, "exchange_code_for_tokens", mock)
    return mock


def _state_from_redirect(location: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    return query["state"][0]


def _issued_cookie(response, name: str) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            value = header.split(";", 1)[0].split("=", 1)[1]
            return value or None
    return None


def _issued_binding_cookie(response) -> str | None:
    return _issued_cookie(response, OAUTH_STATE_COOKIE_NAME)


_HIDDEN_FIELD = re.compile(r'<input type="hidden" name="(user_code|confirm_token)" value="([^"]*)">')


def _open_confirmation(client: TestClient, user_code: str) -> dict[str, str]:
    """Open the verification URL the way a browser does; return the form fields.

    TestClient's jar won't replay a `Secure` cookie over http, so the
    confirmation cookie is re-seated by hand, as the binding cookie is below.
    """
    response = client.get(f"/auth/cli/verify?user_code={user_code}")
    assert response.status_code == 200
    assert "location" not in response.headers
    secret = _issued_cookie(response, CLI_CONFIRM_COOKIE_NAME)
    assert secret, "the confirmation page must issue its cookie"
    client.cookies.set(CLI_CONFIRM_COOKIE_NAME, secret)
    fields = dict(_HIDDEN_FIELD.findall(response.text))
    assert set(fields) == {"user_code", "confirm_token"}
    return fields


def _answer(client: TestClient, user_code: str, action: str, *, headers: dict | None = None):
    fields = _open_confirmation(client, user_code)
    return client.post("/auth/cli/verify", data={**fields, "action": action}, headers=headers or {})


def _continue(client: TestClient, user_code: str):
    response = _answer(client, user_code, "continue")
    assert response.status_code == 303
    return response


def _verify(client: TestClient, user_code: str) -> tuple[str, str]:
    """Open the link, choose Continue, and return (state, nonce).

    The callback is the web app's and requires the browser-binding cookie and
    an ID token echoing the nonce, so the CLI's verify leg must issue both.
    """
    response = _continue(client, user_code)
    binding = _issued_binding_cookie(response)
    assert binding, "Continue must issue the browser-binding cookie"
    client.cookies.set(OAUTH_STATE_COOKIE_NAME, binding)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)
    return query["state"][0], query["nonce"][0]


def _poll_error(client: TestClient, device_code: str) -> str:
    response = client.post("/auth/cli/token", json={"device_code": device_code})
    assert response.status_code == 400
    return response.json()["error"]


def _non_binding_set_cookies(response) -> list[str]:
    """Set-Cookie headers other than the binding cookie's clearing directive."""
    return [h for h in response.headers.get_list("set-cookie") if not h.startswith(OAUTH_STATE_COOKIE_NAME)]


# =====================================================================
# POST /auth/cli/authorize
# =====================================================================


class TestAuthorize:
    def test_returns_both_codes_and_the_verification_url(self, cli_app):
        client = TestClient(cli_app)
        response = client.post("/auth/cli/authorize")

        assert response.status_code == 200
        body = response.json()
        assert body["verification_uri"].endswith("/auth/cli/verify")
        assert body["user_code"] in body["verification_uri_complete"]
        assert body["interval"] > 0
        assert body["expires_in"] > 0
        assert len(body["device_code"]) >= 40

    def test_needs_no_credential(self, cli_app):
        """It is the entry point of the login flow — there is nothing to send."""
        client = TestClient(cli_app)
        assert client.post("/auth/cli/authorize").status_code == 200

    def test_503_when_unconfigured(self, cli_app, monkeypatch):
        bff_routes._device_grant_service = DeviceGrantService(
            repository=DeviceGrantRepository(table_name=""),
            session_repository=bff_routes._repository,
            codec=None,
            verification_uri="http://localhost:8000/auth/cli/verify",
        )
        client = TestClient(cli_app)
        assert client.post("/auth/cli/authorize").status_code == 503

    def test_rate_limited_after_the_burst_budget(self, cli_app):
        client = TestClient(cli_app)
        codes = [client.post("/auth/cli/authorize").status_code for _ in range(12)]
        assert codes[0] == 200
        assert 429 in codes


# =====================================================================
# GET /auth/cli/verify
# =====================================================================


class TestVerify:
    def test_opening_the_link_starts_nothing(self, cli_app, monkeypatch):
        """The prefilled link alone must not reach the identity provider.

        Whoever opens a `verification_uri_complete` link may not be whoever
        started the sign-in. If the GET went straight to Cognito, a browser
        with a live Hosted UI session would approve someone else's terminal
        without seeing a prompt.
        """
        spy = MagicMock(wraps=bff_routes._get_state_store())
        monkeypatch.setattr(cli_routes, "_get_state_store", lambda: spy)
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()

        response = client.get(f"/auth/cli/verify?user_code={auth['user_code']}")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "location" not in response.headers
        assert COGNITO_DOMAIN_URL not in response.text
        assert _issued_binding_cookie(response) is None
        spy.store_state.assert_not_called()
        # Still waiting for an answer.
        assert _poll_error(client, auth["device_code"]) == "authorization_pending"

    def test_the_page_shows_the_code_as_the_terminal_printed_it(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = client.get(f"/auth/cli/verify?user_code={user_code.lower().replace('-', '')}")

        assert f'<p class="code">{user_code}</p>' in response.text
        assert 'value="continue"' in response.text
        assert 'value="cancel"' in response.text

    def test_the_page_cannot_be_framed_cached_or_leak_its_url(self, cli_app):
        """A page one click approves must never be shown inside another site's."""
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = client.get(f"/auth/cli/verify?user_code={user_code}")

        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_the_confirmation_cookie_is_strict_and_host_only(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = client.get(f"/auth/cli/verify?user_code={user_code}")

        header = next(h for h in response.headers.get_list("set-cookie") if h.startswith(CLI_CONFIRM_COOKIE_NAME))
        lowered = header.lower()
        assert "samesite=strict" in lowered
        assert "httponly" in lowered
        assert "secure" in lowered
        assert "path=/" in lowered
        assert "domain=" not in lowered

    def test_continue_redirects_to_cognito_with_state(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = _continue(client, user_code)

        location = response.headers["location"]
        assert location.startswith(f"{COGNITO_DOMAIN_URL}/oauth2/authorize")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        assert query["response_type"] == ["code"]
        assert "state" in query

    def test_issues_the_same_authorize_contract_as_the_web_login(self, cli_app):
        """PKCE, nonce and browser binding — the callback rejects anything less."""
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = _continue(client, user_code)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)

        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"][0]
        assert query["nonce"][0]
        binding = _issued_binding_cookie(response)
        assert binding

        ok, data = bff_routes._get_state_store().get_and_delete_state(query["state"][0])
        assert ok
        assert data.code_verifier and data.nonce == query["nonce"][0]
        assert data.browser_binding_hash == bff_routes._browser_binding_digest(binding)

    def test_state_carries_the_device_code_not_the_url(self, cli_app):
        """The user code must not ride in the redirect the browser can see."""
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = _continue(client, user_code)
        location = response.headers["location"]
        assert normalise_user_code(user_code) not in location

        state = _state_from_redirect(location)
        ok, data = bff_routes._get_state_store().get_and_delete_state(state)
        assert ok
        assert data.device_user_code == normalise_user_code(user_code)

    def test_accepts_human_typing(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        typed = user_code.lower().replace("-", "")
        assert _continue(client, typed).status_code == 303

    def test_missing_code_renders_a_page(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        response = client.get("/auth/cli/verify")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "doesn't look right" in response.text

    def test_malformed_code_renders_a_page(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        # `0`, `O` and vowels are all outside the alphabet.
        response = client.get("/auth/cli/verify?user_code=AEIO-0OU1")
        assert response.status_code == 200
        assert "doesn't look right" in response.text

    def test_unknown_and_used_codes_are_indistinguishable(self, cli_app, monkeypatch):
        """No existence oracle: this caller has not authenticated yet."""
        client = TestClient(cli_app, follow_redirects=False)

        unknown = client.get("/auth/cli/verify?user_code=CDFG-HJKM")

        # Now a real code that has already been answered.
        user_code = client.post("/auth/cli/authorize").json()["user_code"]
        state, nonce = _verify(client, user_code)
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        assert client.get(f"/auth/callback?code=abc&state={state}").status_code == 200

        used = client.get(f"/auth/cli/verify?user_code={user_code}")

        assert unknown.status_code == used.status_code == 200
        assert unknown.text == used.text

    def test_does_not_reflect_input_into_the_page(self, cli_app):
        """The pages take no request input — guard against that regressing."""
        client = TestClient(cli_app, follow_redirects=False)
        payload = "<script>alert(1)</script>"
        response = client.get("/auth/cli/verify?user_code=" + urllib.parse.quote(payload))
        assert response.status_code == 200
        assert "<script>alert(1)</script>" not in response.text
        assert "alert(1)" not in response.text


# =====================================================================
# POST /auth/cli/verify — the confirmation page's answer
# =====================================================================


class TestVerifyAnswer:
    def test_cancel_is_access_denied_on_the_next_poll(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()

        response = _answer(client, auth["user_code"], "cancel")

        assert response.status_code == 200
        assert "Sign-in cancelled" in response.text
        assert "location" not in response.headers
        assert _issued_binding_cookie(response) is None
        assert _poll_error(client, auth["device_code"]) == "access_denied"

    @pytest.mark.asyncio
    async def test_cancel_records_a_denial(self, cli_app, grants):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()

        _answer(client, auth["user_code"], "cancel")

        stored = await grants.get_by_device_code_hash(hash_device_code(auth["device_code"]))
        assert stored.status is GrantStatus.DENIED

    def test_a_cancelled_code_cannot_be_continued(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        fields = _open_confirmation(client, auth["user_code"])
        client.post("/auth/cli/verify", data={**fields, "action": "cancel"})

        # The same page resubmitted (TestClient keeps the cookie, as a second
        # tab opened on the same page would).
        replay = client.post("/auth/cli/verify", data={**fields, "action": "continue"})
        assert "location" not in replay.headers
        reopened = client.get(f"/auth/cli/verify?user_code={auth['user_code']}")
        assert "expired" in reopened.text
        assert _poll_error(client, auth["device_code"]) == "access_denied"

    def _assert_refused(self, grants, response, device_code: str) -> None:
        """Nothing happened: no redirect, no binding, and the grant still pending."""
        import asyncio

        assert response.status_code == 200
        assert "location" not in response.headers
        assert _issued_binding_cookie(response) is None
        assert "This page has expired" in response.text
        stored = asyncio.run(grants.get_by_device_code_hash(hash_device_code(device_code)))
        assert stored.status is GrantStatus.PENDING

    def test_continue_without_the_confirmation_cookie_is_refused(self, cli_app, monkeypatch, grants):
        spy = MagicMock(wraps=bff_routes._get_state_store())
        monkeypatch.setattr(cli_routes, "_get_state_store", lambda: spy)
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        fields = _open_confirmation(client, auth["user_code"])
        client.cookies.delete(CLI_CONFIRM_COOKIE_NAME)

        response = client.post("/auth/cli/verify", data={**fields, "action": "continue"})

        self._assert_refused(grants, response, auth["device_code"])
        spy.store_state.assert_not_called()

    def test_continue_posted_from_another_site_is_refused(self, cli_app, grants):
        """Even holding the cookie and a valid token, a cross-site post is refused."""
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()

        for site in ("cross-site", "same-site"):
            response = _answer(client, auth["user_code"], "continue", headers={"Sec-Fetch-Site": site})
            self._assert_refused(grants, response, auth["device_code"])

    def test_a_forged_post_with_no_page_behind_it_is_refused(self, cli_app, grants):
        """The shape a hostile form would send: the code, a guessed token, no cookie."""
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()

        response = client.post(
            "/auth/cli/verify",
            data={"user_code": auth["user_code"], "confirm_token": "0" * 64, "action": "continue"},
        )

        self._assert_refused(grants, response, auth["device_code"])

    def test_a_token_for_one_code_cannot_answer_another(self, cli_app, grants):
        client = TestClient(cli_app, follow_redirects=False)
        mine = client.post("/auth/cli/authorize").json()
        theirs = client.post("/auth/cli/authorize").json()
        fields = _open_confirmation(client, mine["user_code"])

        response = client.post(
            "/auth/cli/verify",
            data={**fields, "user_code": theirs["user_code"], "action": "continue"},
        )

        self._assert_refused(grants, response, theirs["device_code"])

    def test_an_unknown_action_is_refused(self, cli_app, grants):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()

        for action in ("approve", ""):
            response = _answer(client, auth["user_code"], action)
            self._assert_refused(grants, response, auth["device_code"])

    def test_an_expired_grant_is_reported_like_the_page_does(self, cli_app, grants):
        import asyncio

        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        fields = _open_confirmation(client, auth["user_code"])
        stored = asyncio.run(grants.get_by_device_code_hash(hash_device_code(auth["device_code"])))
        asyncio.run(grants.delete(stored))

        response = client.post("/auth/cli/verify", data={**fields, "action": "continue"})

        assert "location" not in response.headers
        assert "That code has expired" in response.text

    def test_the_answer_spends_the_same_rate_limit_as_the_page(self, cli_app):
        """A sign-in now costs two verify requests; the guessing budget is unchanged."""
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        fields = _open_confirmation(client, auth["user_code"])
        for _ in range(25):
            client.get("/auth/cli/verify?user_code=CDFG-HJKM")

        response = client.post("/auth/cli/verify", data={**fields, "action": "continue"})

        assert response.status_code == 429


# =====================================================================
# POST /auth/cli/token
# =====================================================================


class TestToken:
    def test_pending_poll_is_400_with_an_rfc8628_code(self, cli_app):
        client = TestClient(cli_app)
        device_code = client.post("/auth/cli/authorize").json()["device_code"]

        response = client.post("/auth/cli/token", json={"device_code": device_code})

        assert response.status_code == 400
        assert response.json()["error"] == "authorization_pending"
        assert response.json()["error_description"]

    def test_unknown_device_code_is_invalid_grant(self, cli_app):
        client = TestClient(cli_app)
        response = client.post("/auth/cli/token", json={"device_code": "x" * 40})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    def test_short_device_code_is_rejected_by_validation(self, cli_app):
        client = TestClient(cli_app)
        response = client.post("/auth/cli/token", json={"device_code": "tiny"})
        assert response.status_code == 422

    def test_rate_limited_per_device_code(self, cli_app):
        client = TestClient(cli_app)
        device_code = client.post("/auth/cli/authorize").json()["device_code"]

        codes = [client.post("/auth/cli/token", json={"device_code": device_code}).status_code for _ in range(45)]
        assert 429 in codes

    def test_one_client_cannot_throttle_another(self, cli_app):
        """The token limit is keyed on the device code, not the caller's IP."""
        client = TestClient(cli_app)
        noisy = client.post("/auth/cli/authorize").json()["device_code"]
        quiet = client.post("/auth/cli/authorize").json()["device_code"]

        for _ in range(45):
            client.post("/auth/cli/token", json={"device_code": noisy})

        # Same source address, different grant — still served.
        response = client.post("/auth/cli/token", json={"device_code": quiet})
        assert response.status_code == 400
        assert response.json()["error"] in {"authorization_pending", "slow_down"}


# =====================================================================
# The full flow, and the no-cookie invariant
# =====================================================================


class TestEndToEnd:
    def test_full_flow_hands_the_cli_a_usable_session(self, cli_app, monkeypatch, codec, repository):
        client = TestClient(cli_app, follow_redirects=False)

        # 1. CLI asks for a grant.
        auth = client.post("/auth/cli/authorize").json()

        # 2. Browser opens the verification URL and bounces to Cognito.
        state, nonce = _verify(client, auth["user_code"])

        # 3. Cognito returns; the callback mints the CLI session.
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        callback = client.get(f"/auth/callback?code=auth-code&state={state}")
        assert callback.status_code == 200
        assert "You're signed in" in callback.text

        # 4. CLI polls and receives the sealed session.
        token = client.post("/auth/cli/token", json={"device_code": auth["device_code"]})
        assert token.status_code == 200
        body = token.json()
        assert body["user_id"] == "user-sub-001"
        assert body["username"] == "alice"
        assert body["expires_in"] > 0

        # The sealed value is what SessionRefreshMiddleware will unseal, and
        # it resolves to a real persisted session row.
        session_id = codec.unseal(body["session"]).session_id
        assert session_id not in body["session"]

    @pytest.mark.asyncio
    async def test_sealed_session_resolves_to_a_persisted_row(self, cli_app, monkeypatch, codec, repository):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        state, nonce = _verify(client, auth["user_code"])
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        client.get(f"/auth/callback?code=auth-code&state={state}")
        body = client.post("/auth/cli/token", json={"device_code": auth["device_code"]}).json()

        record = await repository.get(codec.unseal(body["session"]).session_id)
        assert record is not None
        assert record.user_id == "user-sub-001"
        assert record.cognito_refresh_token == "refresh.tok"

    def test_device_callback_sets_no_cookies(self, cli_app, monkeypatch):
        """The invariant. Cookies here would log the browser in as a side
        effect of approving a terminal, and would leave the browser and the
        CLI sharing one session row — two holders of one refresh token."""
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        state, nonce = _verify(client, auth["user_code"])
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))

        callback = client.get(f"/auth/callback?code=auth-code&state={state}")

        assert callback.status_code == 200
        # The only cookie touched is the binding cookie being cleared, exactly
        # as the web-login path clears it. No session or CSRF cookie.
        assert not _non_binding_set_cookies(callback)
        for header in callback.headers.get_list("set-cookie"):
            assert header.split(";", 1)[0] in {f"{OAUTH_STATE_COOKIE_NAME}=", f'{OAUTH_STATE_COOKIE_NAME}=""'}
        assert SESSION_COOKIE_NAME not in client.cookies
        assert CSRF_COOKIE_NAME not in client.cookies

    def test_normal_spa_callback_still_sets_cookies(self, cli_app, monkeypatch):
        """The device branch must not have changed the SPA's behaviour."""
        client = TestClient(cli_app, follow_redirects=False)
        login = client.get("/auth/login")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(login.headers["location"]).query)
        client.cookies.set(OAUTH_STATE_COOKIE_NAME, _issued_binding_cookie(login))
        _patch_token_exchange(monkeypatch, make_id_token(nonce=query["nonce"][0]))

        response = client.get(f"/auth/callback?code=abc&state={query['state'][0]}")

        assert response.status_code == 302
        blob = " ".join(response.headers.get_list("set-cookie"))
        assert SESSION_COOKIE_NAME in blob
        assert CSRF_COOKIE_NAME in blob

    def test_second_poll_after_claim_is_refused(self, cli_app, monkeypatch):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        state, nonce = _verify(client, auth["user_code"])
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        client.get(f"/auth/callback?code=auth-code&state={state}")

        first = client.post("/auth/cli/token", json={"device_code": auth["device_code"]})
        assert first.status_code == 200

        second = client.post("/auth/cli/token", json={"device_code": auth["device_code"]})
        assert second.status_code == 400
        assert second.json()["error"] in {"invalid_grant", "slow_down"}

    @pytest.mark.asyncio
    async def test_expired_grant_at_callback_discards_the_session(self, cli_app, monkeypatch, grants, repository):
        """A sign-in that crosses the deadline must not strand a live session.

        Nobody would hold it — the browser gets no cookie and the CLI can no
        longer claim it — so the row is deleted rather than left to TTL.
        """
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        state, nonce = _verify(client, auth["user_code"])

        # Expire the grant while the human is still at the IdP.
        stored = await grants.get_by_device_code_hash(hash_device_code(auth["device_code"]))
        await grants.delete(stored)

        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        callback = client.get(f"/auth/callback?code=auth-code&state={state}")

        assert callback.status_code == 200
        assert "expired" in callback.text.lower()
        assert not _non_binding_set_cookies(callback)

        # No orphaned session rows left behind.
        from boto3.dynamodb.conditions import Attr

        import boto3

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(BFF_SESSIONS_TABLE)
        sessions = table.scan(FilterExpression=Attr("PK").begins_with("SESSION#"))
        assert sessions["Items"] == []

    @pytest.mark.asyncio
    async def test_grant_reaches_claimed_after_the_full_flow(self, cli_app, monkeypatch, grants):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        state, nonce = _verify(client, auth["user_code"])
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        client.get(f"/auth/callback?code=auth-code&state={state}")
        client.post("/auth/cli/token", json={"device_code": auth["device_code"]})

        stored = await grants.get_by_device_code_hash(hash_device_code(auth["device_code"]))
        assert stored is not None
        assert stored.status is GrantStatus.CLAIMED

    def test_state_is_single_use(self, cli_app, monkeypatch):
        """Replaying the callback URL must not mint a second session."""
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        state, nonce = _verify(client, auth["user_code"])
        _patch_token_exchange(monkeypatch, make_id_token(nonce=nonce))
        binding = next(c.value for c in client.cookies.jar if c.name == OAUTH_STATE_COOKIE_NAME)

        first = client.get(f"/auth/callback?code=abc&state={state}")
        assert first.status_code == 200

        # Even a replay that still holds the binding is refused on state.
        client.cookies.clear()
        client.cookies.set(OAUTH_STATE_COOKIE_NAME, binding)

        replay = client.get(f"/auth/callback?code=abc&state={state}")
        assert replay.status_code == 302
        assert "auth_error=bad_state" in replay.headers["location"]


# =====================================================================
# Operational edges
# =====================================================================


class TestOperationalEdges:
    def test_the_authorize_rate_limit_keys_on_the_forwarded_address(self, cli_app):
        """Behind the ALB the socket peer is the load balancer; the per-IP limit
        has to read X-Forwarded-For or one noisy client throttles everyone."""
        client = TestClient(cli_app)
        a = {"X-Forwarded-For": "198.51.100.7, 10.0.0.1"}
        b = {"X-Forwarded-For": "198.51.100.8"}
        codes_a = [client.post("/auth/cli/authorize", headers=a).status_code for _ in range(25)]
        assert 429 in codes_a
        # A different forwarded address, same socket peer: still served.
        assert client.post("/auth/cli/authorize", headers=b).status_code == 200

    def test_an_empty_forwarded_header_falls_back_to_the_peer(self, cli_app):
        client = TestClient(cli_app)
        response = client.post("/auth/cli/authorize", headers={"X-Forwarded-For": " , "})
        assert response.status_code == 200

    def test_verify_is_503_when_bff_auth_is_not_configured(self, cli_app, monkeypatch):
        """Misconfiguration surfaces on the request, not as a startup crash."""
        from apis.app_api.auth.bff import config as bff_config

        monkeypatch.setattr(bff_config.BFFAuthConfig, "is_ready", lambda self: False)
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]
        response = client.get(f"/auth/cli/verify?user_code={user_code}")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_a_failed_orphan_delete_is_logged_not_raised(self, cli_app, monkeypatch, caplog):
        """Best effort: the DynamoDB TTL is the backstop."""

        class Boom:
            async def delete(self, session_id):
                raise RuntimeError("dynamo down")

        monkeypatch.setattr(bff_routes, "_get_repository", lambda config: Boom())
        with caplog.at_level("WARNING"):
            await bff_routes._discard_orphan_session("sess-x")
        assert any("orphaned device session" in record.getMessage() for record in caplog.records)
