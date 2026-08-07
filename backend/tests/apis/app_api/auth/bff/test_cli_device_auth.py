"""Tests for the CLI device-authorization routes and the callback's device branch.

Covers the full three-leg flow end to end, plus the invariants that are easy
to regress:

* the device callback path sets **no** cookies (writing them would log the
  browser in as a side effect and make two holders share one refresh token);
* an unapprovable code renders the same page whatever the reason, so the
  endpoint is not a user-code existence oracle;
* pending polls are HTTP 400 with an RFC 8628 `error`, not 200.
"""

from __future__ import annotations

import time
import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import cli_routes
from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.cli_routes import router as cli_router
from apis.app_api.auth.bff.routes import router as bff_router
from apis.app_api.auth.bff.token_exchange import ExchangeResult
from apis.shared.auth.device_grants.models import (
    GrantStatus,
    hash_device_code,
    normalise_user_code,
)
from apis.shared.auth.device_grants.repository import DeviceGrantRepository
from apis.shared.auth.device_grants.service import DeviceGrantService
from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

from .conftest import BFF_SESSIONS_TABLE, COGNITO_DOMAIN_URL, make_id_token


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
    def test_redirects_to_cognito_with_state(self, cli_app):
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = client.get(f"/auth/cli/verify?user_code={user_code}")

        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith(f"{COGNITO_DOMAIN_URL}/oauth2/authorize")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        assert query["response_type"] == ["code"]
        assert "state" in query

    def test_state_carries_the_device_code_not_the_url(self, cli_app):
        """The user code must not ride in the redirect the browser can see."""
        client = TestClient(cli_app, follow_redirects=False)
        user_code = client.post("/auth/cli/authorize").json()["user_code"]

        response = client.get(f"/auth/cli/verify?user_code={user_code}")
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
        response = client.get(f"/auth/cli/verify?user_code={typed}")
        assert response.status_code == 302

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
        id_token = make_id_token()
        _patch_token_exchange(monkeypatch, id_token)
        loc = client.get(f"/auth/cli/verify?user_code={user_code}").headers["location"]
        state = _state_from_redirect(loc)
        client.get(f"/auth/callback?code=abc&state={state}")

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
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]
        state = _state_from_redirect(loc)

        # 3. Cognito returns; the callback mints the CLI session.
        _patch_token_exchange(monkeypatch, make_id_token())
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
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]
        _patch_token_exchange(monkeypatch, make_id_token())
        client.get(f"/auth/callback?code=auth-code&state={_state_from_redirect(loc)}")
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
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]
        _patch_token_exchange(monkeypatch, make_id_token())

        callback = client.get(f"/auth/callback?code=auth-code&state={_state_from_redirect(loc)}")

        assert callback.status_code == 200
        assert not callback.headers.get_list("set-cookie")
        assert SESSION_COOKIE_NAME not in client.cookies
        assert CSRF_COOKIE_NAME not in client.cookies

    def test_normal_spa_callback_still_sets_cookies(self, cli_app, monkeypatch):
        """The device branch must not have changed the SPA's behaviour."""
        from apis.shared.auth.state_store import OIDCStateData

        from .conftest import CALLBACK_URL

        bff_routes._get_state_store().store_state(
            "spa-state",
            OIDCStateData(redirect_uri=CALLBACK_URL, provider_id="cognito-bff"),
            ttl_seconds=600,
        )
        _patch_token_exchange(monkeypatch, make_id_token())

        client = TestClient(cli_app, follow_redirects=False)
        response = client.get("/auth/callback?code=abc&state=spa-state")

        assert response.status_code == 302
        blob = " ".join(response.headers.get_list("set-cookie"))
        assert SESSION_COOKIE_NAME in blob
        assert CSRF_COOKIE_NAME in blob

    def test_second_poll_after_claim_is_refused(self, cli_app, monkeypatch):
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]
        _patch_token_exchange(monkeypatch, make_id_token())
        client.get(f"/auth/callback?code=auth-code&state={_state_from_redirect(loc)}")

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
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]

        # Expire the grant while the human is still at the IdP.
        stored = await grants.get_by_device_code_hash(hash_device_code(auth["device_code"]))
        await grants.delete(stored)

        _patch_token_exchange(monkeypatch, make_id_token())
        callback = client.get(f"/auth/callback?code=auth-code&state={_state_from_redirect(loc)}")

        assert callback.status_code == 200
        assert "expired" in callback.text.lower()
        assert not callback.headers.get_list("set-cookie")

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
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]
        _patch_token_exchange(monkeypatch, make_id_token())
        client.get(f"/auth/callback?code=auth-code&state={_state_from_redirect(loc)}")
        client.post("/auth/cli/token", json={"device_code": auth["device_code"]})

        stored = await grants.get_by_device_code_hash(hash_device_code(auth["device_code"]))
        assert stored is not None
        assert stored.status is GrantStatus.CLAIMED

    def test_state_is_single_use(self, cli_app, monkeypatch):
        """Replaying the callback URL must not mint a second session."""
        client = TestClient(cli_app, follow_redirects=False)
        auth = client.post("/auth/cli/authorize").json()
        loc = client.get(f"/auth/cli/verify?user_code={auth['user_code']}").headers["location"]
        state = _state_from_redirect(loc)
        _patch_token_exchange(monkeypatch, make_id_token())

        first = client.get(f"/auth/callback?code=abc&state={state}")
        assert first.status_code == 200

        replay = client.get(f"/auth/callback?code=abc&state={state}")
        assert replay.status_code == 302
        assert "auth_error=bad_state" in replay.headers["location"]
