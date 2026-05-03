"""Tests for `GET /auth/callback`."""

from __future__ import annotations

import time
import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.token_exchange import ExchangeResult, TokenExchangeError
from apis.shared.sessions_bff.config import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)

from .conftest import POST_LOGIN_URL, make_id_token


def _seed_state(
    state: str = "valid-state",
    *,
    redirect_uri: str | None = None,
    return_to: str | None = None,
) -> str:
    """Push a state token through the same store the route reads from."""
    from .conftest import CALLBACK_URL

    store = bff_routes._get_state_store()
    from apis.shared.auth.state_store import OIDCStateData

    store.store_state(
        state,
        OIDCStateData(
            redirect_uri=redirect_uri or CALLBACK_URL,
            provider_id="cognito-bff",
            return_to=return_to,
        ),
        ttl_seconds=600,
    )
    return state


def _patch_token_exchange(monkeypatch, result: ExchangeResult | Exception) -> MagicMock:
    """Replace the async token-exchange helper with a mock."""
    if isinstance(result, Exception):
        mock = AsyncMock(side_effect=result)
    else:
        mock = AsyncMock(return_value=result)
    monkeypatch.setattr(bff_routes, "exchange_code_for_tokens", mock)
    return mock


def test_callback_happy_path_writes_row_and_cookies(app, monkeypatch, repository):
    state = _seed_state()
    id_token = make_id_token()
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="access.tok",
            refresh_token="refresh.tok",
            id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=auth-code-xyz&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == POST_LOGIN_URL

    # Both cookies set (TestClient surfaces them via Set-Cookie headers).
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob
    assert CSRF_COOKIE_NAME in set_cookie_blob
    assert "Secure" in set_cookie_blob
    assert "HttpOnly" in set_cookie_blob  # session cookie is httponly

    # And a session row got persisted under some session_id.
    scanned = repository._table.scan().get("Items", [])
    assert len(scanned) == 1
    item = scanned[0]
    assert item["user_id"] == "user-sub-001"
    assert item["username"] == "alice"
    assert item["cognito_access_token"] == "access.tok"
    assert item["cognito_refresh_token"] == "refresh.tok"


def test_callback_consumes_state_one_time(app, monkeypatch):
    state = _seed_state("once-only")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = TestClient(app, follow_redirects=False)

    first = client.get(f"/auth/callback?code=c&state={state}")
    assert first.status_code == 302
    assert "auth_error" not in first.headers["location"]

    # Replay with the same state must fail (state was deleted on first use).
    second = client.get(f"/auth/callback?code=c2&state={state}")
    assert second.status_code == 302
    parsed = urllib.parse.urlparse(second.headers["location"])
    assert dict(urllib.parse.parse_qsl(parsed.query)).get("auth_error") == "bad_state"


def test_callback_missing_code_redirects_with_error(app, monkeypatch):
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get("/auth/callback?state=whatever")
    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "missing_params"


def test_callback_oauth_error_param_redirects_with_error(app):
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        "/auth/callback?error=access_denied&error_description=user+cancelled"
    )
    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "oauth_error"
    # And cookies should be cleared on a failure path so a stale cookie
    # from a partial prior session is dropped.
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob


def test_callback_token_exchange_failure_redirects_with_error(app, monkeypatch):
    state = _seed_state("ex-fail")
    _patch_token_exchange(monkeypatch, TokenExchangeError("boom"))

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "exchange_failed"


def test_callback_missing_id_token_redirects_with_error(app, monkeypatch):
    state = _seed_state("no-id")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a",
            refresh_token="r",
            id_token=None,
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")
    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "no_id_token"


def test_callback_session_id_is_unique_across_logins(app, monkeypatch, repository):
    """Two callback successes write two distinct session_ids."""
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = TestClient(app, follow_redirects=False)

    s1 = _seed_state("first")
    s2 = _seed_state("second")
    client.get(f"/auth/callback?code=c1&state={s1}")
    client.get(f"/auth/callback?code=c2&state={s2}")

    items = repository._table.scan().get("Items", [])
    assert len({item["session_id"] for item in items}) == 2


# ─── return_to deep-link round-trip (Phase 7) ─────────────────────────


def test_callback_redirects_to_return_to_path_when_set(app, monkeypatch):
    """Successful callback honours the same-origin path the SPA stashed
    at /auth/login, grafted onto the SPA origin from
    BFF_POST_LOGIN_REDIRECT_URL so cross-origin dev (BFF on :8000, SPA on
    :4200) lands on the SPA host instead of the BFF host."""
    state = _seed_state("with-return-to", return_to="/files/abc?tab=details")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    # POST_LOGIN_URL = "http://localhost:4200/" — origin spliced onto path.
    assert (
        response.headers["location"]
        == "http://localhost:4200/files/abc?tab=details"
    )


def test_callback_falls_back_to_post_login_when_no_return_to(app, monkeypatch):
    state = _seed_state("no-return-to")  # return_to omitted → None
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == POST_LOGIN_URL


# ─── Users-table upsert from ID-token claims (Phase 7 follow-up) ──────


def _patch_user_sync(monkeypatch) -> AsyncMock:
    """Replace the lazy `_get_user_sync_service` with a stub that captures
    the kwargs the callback passes to `sync_from_user`.

    The real service skips when the Users table env var isn't configured;
    we stub it so the test can assert the BFF callback actually calls
    sync with the email/name/roles parsed from the ID token — that's the
    fix for the "first-login user gets email=None and Cognito provider
    group instead of IdP roles" regression."""
    sync_mock = MagicMock()
    sync_mock.enabled = True
    sync_mock.sync_from_user = AsyncMock(return_value=(None, True))
    monkeypatch.setattr(bff_routes, "_get_user_sync_service", lambda: sync_mock)
    return sync_mock.sync_from_user


def test_callback_upserts_user_with_id_token_claims(app, monkeypatch):
    """The Users row must be seeded from the *ID token* — the access token
    has no email/name/picture and only carries Cognito's internal provider
    group in `cognito:groups`, never the IdP-mapped role list."""
    state = _seed_state("sync-claims")
    id_token = make_id_token(
        sub="user-sub-001",
        username="alice",
        email="Alice@Example.com",
        name="Alice Example",
        picture="https://example.com/a.png",
        custom_roles='["Admin","Editor"]',
    )
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    sync_call = _patch_user_sync(monkeypatch)

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    sync_call.assert_awaited_once()
    kwargs = sync_call.await_args.kwargs
    assert kwargs["user_id"] == "user-sub-001"
    # email is normalized to lowercase by `decode_id_token_claims`
    assert kwargs["email"] == "alice@example.com"
    assert kwargs["name"] == "Alice Example"
    assert kwargs["picture"] == "https://example.com/a.png"
    # `custom:roles` is preferred over `cognito:groups`; JSON-array form
    # parses out cleanly.
    assert kwargs["roles"] == ["Admin", "Editor"]


def test_callback_falls_back_to_cognito_groups_when_custom_roles_absent(
    app, monkeypatch
):
    """No `custom:roles` claim → use `cognito:groups`. This matches the
    access-token validator's behavior so RBAC is consistent between the
    Bearer (legacy) and cookie (BFF) paths."""
    state = _seed_state("sync-groups")
    id_token = make_id_token(
        custom_roles=None,
        cognito_groups=["Admin", "Beta"],
    )
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    sync_call = _patch_user_sync(monkeypatch)

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert sync_call.await_args.kwargs["roles"] == ["Admin", "Beta"]


def test_callback_user_sync_failure_does_not_break_login(app, monkeypatch):
    """A DDB hiccup on the Users-table upsert must not prevent the user
    from logging in — they get a valid session, the Users row just lags."""
    state = _seed_state("sync-failure")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    failing_sync = MagicMock()
    failing_sync.enabled = True
    failing_sync.sync_from_user = AsyncMock(side_effect=RuntimeError("ddb down"))
    monkeypatch.setattr(bff_routes, "_get_user_sync_service", lambda: failing_sync)

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == POST_LOGIN_URL
