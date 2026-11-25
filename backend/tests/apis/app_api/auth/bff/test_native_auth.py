"""Native-app sign-in (docs/specs/native-app-auth.md, PR 1).

Covers the three legs the iOS app drives, end to end through the real
routes, the in-memory state store and a moto sessions table:

* `GET /auth/login?client=native…` — allowlist, challenge shape, state row.
* `GET /auth/callback` — the native exit: provisional row, handoff code on a
  redirect to the app, **no session cookie**, and failures that redirect to
  the app with `?error=` without clearing session cookies.
* `POST /auth/native/exchange` — happy path, and missing / spent / expired /
  wrong-verifier handoffs, each `400 invalid_grant`, with the orphaned row
  deleted where the handoff names one.

The request and response shapes are the ones the merged iOS client sends and
decodes (`ios/AgentCoreKit/Sources/AgentCoreKit/AgentCoreClient.swift`).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import native_routes
from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.cookies import OAUTH_STATE_COOKIE_NAME
from apis.app_api.auth.bff.native_routes import router as native_router
from apis.app_api.auth.bff.routes import router as bff_router
from apis.app_api.auth.bff.token_exchange import ExchangeResult, TokenExchangeError
from apis.shared import rate_limit
from apis.shared.auth.models import User
from apis.shared.auth.state_store import NativeHandoff
from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

from .conftest import make_id_token

APP_REDIRECT = "edu.example.agentcore://auth/callback"
LOOPBACK_REDIRECT = "http://127.0.0.1/callback"
ABSOLUTE_LIFETIME = 30 * 24 * 3600

#: A multiple of 300, plus 30, so a burst never straddles a limiter window.
_FROZEN_RATE_LIMIT_NOW = 1_800_000_030.0


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def _session_cookie_writes(response) -> list[str]:
    """Set-Cookie headers that touch the session or CSRF cookie (set or clear)."""
    return [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{SESSION_COOKIE_NAME}=") or header.startswith(f"{CSRF_COOKIE_NAME}=")
    ]


@pytest.fixture(autouse=True)
def _reset_native_state():
    native_routes._reset_for_tests()
    yield
    native_routes._reset_for_tests()


@pytest.fixture
def native_env(monkeypatch, bff_env):
    monkeypatch.setenv("NATIVE_AUTH_REDIRECT_URIS", f"{APP_REDIRECT}, {LOOPBACK_REDIRECT}")
    yield


@pytest.fixture
def native_app(monkeypatch, native_env, app) -> FastAPI:
    """The BFF router plus the native router, with a real rate limiter on
    the moto table (its clock frozen mid-window) and a fake access-token
    validator standing in for Cognito's JWKS."""
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: _FROZEN_RATE_LIMIT_NOW))
    app.include_router(native_router)
    return app


@pytest.fixture
def validator():
    fake_user = User(
        email="member@example.edu",
        user_id="user-sub-001",
        name="A Member",
        roles=["Faculty"],
        picture=None,
    )
    validator = MagicMock()
    validator.validate_token.return_value = fake_user

    async def _noop(_user):
        return None

    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator", return_value=validator
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store", side_effect=_noop
    ), patch(
        "apis.shared.auth.dependencies._get_user_sync_service", return_value=None
    ):
        yield validator


def _tokens(
    monkeypatch, result: ExchangeResult | Exception | None = None, *, nonce: str | None = None
) -> None:
    """Stub Cognito's token endpoint. `nonce` must echo the one `/auth/login`
    sent in the authorize URL, as the IdP would."""
    if result is None:
        result = ExchangeResult(
            access_token="access.tok",
            refresh_token="refresh.tok",
            id_token=make_id_token(nonce=nonce),
            access_token_exp=int(time.time()) + 3600,
        )
    mock = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(bff_routes, "exchange_code_for_tokens", mock)


def _login(client: TestClient, *, challenge: str, redirect_uri: str = APP_REDIRECT, provider: str | None = None):
    params = {"client": "native", "redirect_uri": redirect_uri, "code_challenge": challenge}
    if provider:
        params["provider"] = provider
    return client.get("/auth/login?" + urllib.parse.urlencode(params))


def _sign_in(native_app, monkeypatch, *, redirect_uri: str = APP_REDIRECT):
    """Login → callback inside one cookie jar, as the sign-in sheet does.

    Returns (callback response, verifier). The binding cookie set by
    `/auth/login` rides to `/auth/callback` exactly as in the real sheet.
    """
    verifier, challenge = _pkce()
    client = TestClient(native_app, follow_redirects=False)
    login = _login(client, challenge=challenge, redirect_uri=redirect_uri)
    assert login.status_code == 302
    authorize = _query(login.headers["location"])
    state = authorize["state"]
    _tokens(monkeypatch, nonce=authorize["nonce"])
    # TestClient's base URL is http, so the __Host- cookie is not replayed by
    # the jar; carry it across by hand as the sheet's jar would over HTTPS.
    binding = login.cookies.get(OAUTH_STATE_COOKIE_NAME)
    client.cookies.set(OAUTH_STATE_COOKIE_NAME, binding)
    callback = client.get(f"/auth/callback?code=auth-code&state={state}")
    return callback, verifier


def _exchange(native_app, handoff: str, verifier: str, **extra):
    body = {"handoff": handoff, "code_verifier": verifier, **extra}
    return TestClient(native_app).post("/auth/native/exchange", json=body)


def _rows(repository) -> list[dict]:
    return [i for i in repository._table.scan().get("Items", []) if i["PK"].startswith("SESSION#")]


# =====================================================================
# GET /auth/login?client=native
# =====================================================================


class TestNativeLogin:
    def test_empty_allowlist_is_404(self, app_for_login, monkeypatch):
        monkeypatch.delenv("NATIVE_AUTH_REDIRECT_URIS", raising=False)
        _, challenge = _pkce()
        response = _login(TestClient(app_for_login, follow_redirects=False), challenge=challenge)
        assert response.status_code == 404

    def test_redirect_not_on_the_allowlist_is_404(self, app_for_login, native_env):
        _, challenge = _pkce()
        client = TestClient(app_for_login, follow_redirects=False)
        for uri in (
            "edu.evil.app://auth/callback",
            APP_REDIRECT + "/",  # exact match: no normalisation
            APP_REDIRECT.upper(),
            "",
        ):
            assert _login(client, challenge=challenge, redirect_uri=uri).status_code == 404, uri

    def test_a_non_s256_challenge_is_400(self, app_for_login, native_env):
        client = TestClient(app_for_login, follow_redirects=False)
        for challenge in ("short", "x" * 44, "a" * 42 + "=", ""):
            params = {"client": "native", "redirect_uri": APP_REDIRECT, "code_challenge": challenge}
            response = client.get("/auth/login?" + urllib.parse.urlencode(params))
            assert response.status_code == 400, challenge

    def test_allowlisted_login_records_the_app_in_the_state(self, app_for_login, native_env):
        _, challenge = _pkce()
        response = _login(
            TestClient(app_for_login, follow_redirects=False),
            challenge=challenge,
            provider="ms-entra-id",
        )

        assert response.status_code == 302
        authorize = _query(response.headers["location"])
        # Cognito still sees our callback and our own PKCE, never the app's.
        assert authorize["redirect_uri"] == "http://localhost:8000/auth/callback"
        assert authorize["code_challenge"] != challenge
        assert authorize["identity_provider"] == "ms-entra-id"
        assert OAUTH_STATE_COOKIE_NAME in response.cookies

        ok, data = bff_routes._get_state_store().get_and_delete_state(authorize["state"])
        assert ok
        assert data.native_redirect_uri == APP_REDIRECT
        assert data.native_code_challenge == challenge
        assert data.return_to is None

    def test_the_browser_login_is_unchanged_by_the_allowlist(self, app_for_login, native_env):
        response = TestClient(app_for_login, follow_redirects=False).get(
            "/auth/login?return_to=/projects&redirect_uri=edu.evil.app://x"
        )
        assert response.status_code == 302
        state = _query(response.headers["location"])["state"]
        _, data = bff_routes._get_state_store().get_and_delete_state(state)
        assert data.native_redirect_uri is None
        assert data.return_to == "/projects"


# =====================================================================
# GET /auth/callback — the native exit
# =====================================================================


class TestNativeCallback:
    def test_hands_off_with_no_session_cookie(self, native_app, monkeypatch, repository):
        callback, _ = _sign_in(native_app, monkeypatch)

        assert callback.status_code == 302
        location = callback.headers["location"]
        assert location.startswith(APP_REDIRECT + "?")
        handoff = _query(location)["handoff"]
        assert len(handoff) >= 43

        # No session cookie set (or cleared) — only the binding cookie drops.
        assert _session_cookie_writes(callback) == []

        rows = _rows(repository)
        assert len(rows) == 1
        row = rows[0]
        assert row["client"] == "native"
        # Provisional until the exchange: minutes, not the 8 h browser window.
        assert int(row["ttl"]) - int(row["created_at"]) <= bff_routes._NATIVE_PROVISIONAL_TTL_SECONDS

    def test_the_handoff_works_on_a_loopback_redirect(self, native_app, monkeypatch):
        callback, _ = _sign_in(native_app, monkeypatch, redirect_uri=LOOPBACK_REDIRECT)
        assert callback.headers["location"].startswith(LOOPBACK_REDIRECT + "?handoff=")

    def test_a_failure_after_the_state_is_read_redirects_to_the_app(
        self, native_app, monkeypatch, repository
    ):
        verifier, challenge = _pkce()
        client = TestClient(native_app, follow_redirects=False)
        login = _login(client, challenge=challenge)
        state = _query(login.headers["location"])["state"]
        client.cookies.set(OAUTH_STATE_COOKIE_NAME, login.cookies.get(OAUTH_STATE_COOKIE_NAME))
        # A web session already in the shared jar must survive the failure.
        client.cookies.set(SESSION_COOKIE_NAME, "a-web-session")
        _tokens(monkeypatch, TokenExchangeError("boom"))

        callback = client.get(f"/auth/callback?code=c&state={state}")

        assert callback.status_code == 302
        assert callback.headers["location"] == f"{APP_REDIRECT}?error=exchange_failed"
        assert _session_cookie_writes(callback) == []
        assert _rows(repository) == []

    def test_an_unbound_native_state_redirects_to_the_app(self, native_app, monkeypatch):
        _, challenge = _pkce()
        client = TestClient(native_app, follow_redirects=False)
        login = _login(client, challenge=challenge)
        state = _query(login.headers["location"])["state"]
        client.cookies.set(OAUTH_STATE_COOKIE_NAME, "some-other-browser")

        callback = client.get(f"/auth/callback?code=c&state={state}")

        assert callback.headers["location"] == f"{APP_REDIRECT}?error=state_not_bound"
        assert _session_cookie_writes(callback) == []

    def test_an_allowlist_withdrawn_mid_login_redirects_nowhere(
        self, native_app, monkeypatch, repository
    ):
        _, challenge = _pkce()
        client = TestClient(native_app, follow_redirects=False)
        login = _login(client, challenge=challenge)
        state = _query(login.headers["location"])["state"]
        client.cookies.set(OAUTH_STATE_COOKIE_NAME, login.cookies.get(OAUTH_STATE_COOKIE_NAME))
        monkeypatch.setenv("NATIVE_AUTH_REDIRECT_URIS", "")
        _tokens(monkeypatch, nonce=_query(login.headers["location"])["nonce"])

        callback = client.get(f"/auth/callback?code=c&state={state}")

        assert callback.status_code == 404
        assert _session_cookie_writes(callback) == []
        assert _rows(repository) == []

    def test_a_failure_before_the_state_is_read_keeps_browser_behaviour(self, native_app):
        callback = TestClient(native_app, follow_redirects=False).get("/auth/callback?state=x")
        assert _query(callback.headers["location"])["auth_error"] == "missing_params"


# =====================================================================
# POST /auth/native/exchange
# =====================================================================


class TestNativeExchange:
    def test_happy_path_returns_the_session_the_app_expects(
        self, native_app, monkeypatch, repository, codec, validator
    ):
        callback, verifier = _sign_in(native_app, monkeypatch)
        handoff = _query(callback.headers["location"])["handoff"]

        response = _exchange(
            native_app,
            handoff,
            verifier,
            device_model="iPhone17,1" + "x" * 200,
            app_version="1.0 (42)\n",
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"session", "expires_at", "user"}
        assert body["user"]["user_id"] == "user-sub-001"
        assert set(body["user"]) == {"user_id", "email", "name", "roles", "picture", "csrf_token"}

        (row,) = _rows(repository)
        assert codec.unseal(body["session"]).session_id == row["session_id"]
        # Non-sliding lifetime from the moment of hand-over (D4).
        assert body["expires_at"] == int(row["created_at"]) + ABSOLUTE_LIFETIME
        assert int(row["ttl"]) == body["expires_at"]
        # Bounded metadata.
        assert row["device_model"] == ("iPhone17,1" + "x" * 200)[:64]
        assert row["app_version"] == "1.0 (42)"

    def test_a_spent_handoff_is_invalid_grant_and_keeps_the_session(
        self, native_app, monkeypatch, repository, validator
    ):
        callback, verifier = _sign_in(native_app, monkeypatch)
        handoff = _query(callback.headers["location"])["handoff"]
        assert _exchange(native_app, handoff, verifier).status_code == 200

        replay = _exchange(native_app, handoff, verifier)

        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"
        # The first exchange's session is the app's now; a replay must not
        # delete it.
        assert len(_rows(repository)) == 1

    def test_a_wrong_verifier_is_invalid_grant_and_deletes_the_row(
        self, native_app, monkeypatch, repository, validator
    ):
        callback, verifier = _sign_in(native_app, monkeypatch)
        handoff = _query(callback.headers["location"])["handoff"]
        other_verifier, _ = _pkce()

        response = _exchange(native_app, handoff, other_verifier)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert _rows(repository) == []
        # And the handoff is spent: the right verifier can't use it now.
        assert _exchange(native_app, handoff, verifier).status_code == 400

    def test_an_expired_handoff_is_invalid_grant_and_deletes_the_row(
        self, native_app, monkeypatch, repository, validator
    ):
        callback, verifier = _sign_in(native_app, monkeypatch)
        handoff = _query(callback.headers["location"])["handoff"]
        store = bff_routes._get_state_store()
        parked = store.take_handoff(handoff)
        store.store_handoff(
            handoff,
            NativeHandoff(
                session_id=parked.session_id,
                code_challenge=parked.code_challenge,
                expires_at=int(time.time()) - 1,
            ),
        )

        response = _exchange(native_app, handoff, verifier)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert _rows(repository) == []

    def test_an_unknown_handoff_is_invalid_grant(self, native_app, validator):
        verifier, _ = _pkce()
        response = _exchange(native_app, "never-issued", verifier)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    def test_empty_allowlist_is_404(self, app, monkeypatch, bff_env):
        monkeypatch.delenv("NATIVE_AUTH_REDIRECT_URIS", raising=False)
        app.include_router(native_router)
        verifier, _ = _pkce()
        response = TestClient(app).post(
            "/auth/native/exchange", json={"handoff": "h", "code_verifier": verifier}
        )
        assert response.status_code == 404

    def test_rate_limited_per_client_ip(self, native_app, validator):
        verifier, _ = _pkce()
        client = TestClient(native_app)
        statuses = [
            client.post(
                "/auth/native/exchange",
                json={"handoff": f"h{i}", "code_verifier": verifier},
                headers={"X-Forwarded-For": "203.0.113.9"},
            ).status_code
            for i in range(native_routes._EXCHANGE_MAX_REQUESTS + 1)
        ]
        assert statuses[:-1] == [400] * native_routes._EXCHANGE_MAX_REQUESTS
        assert statuses[-1] == 429
        # Another address has its own budget.
        other = client.post(
            "/auth/native/exchange",
            json={"handoff": "h", "code_verifier": verifier},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert other.status_code == 400


# =====================================================================
# POST /auth/logout — header sessions and token revocation
# =====================================================================


class TestLogout:
    @pytest.fixture
    def revoke(self, monkeypatch):
        client = MagicMock()
        client.enabled = True
        client.revoke = AsyncMock()
        monkeypatch.setattr(bff_routes, "_get_cognito_client", lambda: client)
        return client.revoke

    def _signed_in(self, native_app, monkeypatch, validator) -> str:
        callback, verifier = _sign_in(native_app, monkeypatch)
        handoff = _query(callback.headers["location"])["handoff"]
        return _exchange(native_app, handoff, verifier).json()["session"]

    def test_header_logout_deletes_the_row_and_writes_no_cookies(
        self, native_app, monkeypatch, repository, validator, revoke
    ):
        sealed = self._signed_in(native_app, monkeypatch, validator)

        response = TestClient(native_app).post(
            "/auth/logout", headers={"Authorization": f"BFF {sealed}"}
        )

        assert response.status_code == 200
        assert response.headers.get_list("set-cookie") == []
        assert _rows(repository) == []
        revoke.assert_awaited_once_with(refresh_token="refresh.tok")
        # Published, so every other task drops its cached copy in one poll.
        revoked = [i for i in repository._table.scan()["Items"] if i["PK"] == "REVOKED"]
        assert len(revoked) == 1

    def test_cookie_logout_also_revokes(self, native_app, repository, codec, revoke):
        from apis.shared.sessions_bff.models import CookiePayload

        from .test_logout import _seed_record

        record = _seed_record(repository)
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        response = TestClient(native_app).post(
            "/auth/logout", cookies={SESSION_COOKIE_NAME: sealed}
        )

        assert response.status_code == 200
        assert any(h.startswith(f"{SESSION_COOKIE_NAME}=") for h in response.headers.get_list("set-cookie"))
        revoke.assert_awaited_once_with(refresh_token=record.cognito_refresh_token)

    def test_a_revoke_failure_does_not_fail_logout(
        self, native_app, monkeypatch, repository, validator, revoke
    ):
        from apis.shared.sessions_bff.refresh import CognitoRefreshError

        sealed = self._signed_in(native_app, monkeypatch, validator)
        revoke.side_effect = CognitoRefreshError("cognito down")

        response = TestClient(native_app).post(
            "/auth/logout", headers={"Authorization": f"BFF {sealed}"}
        )

        assert response.status_code == 200
        assert _rows(repository) == []

    def test_an_unreadable_header_logs_out_quietly(self, native_app, revoke):
        response = TestClient(native_app).post(
            "/auth/logout", headers={"Authorization": "BFF not-a-sealed-value"}
        )
        assert response.status_code == 200
        assert response.headers.get_list("set-cookie") == []
        revoke.assert_not_awaited()
