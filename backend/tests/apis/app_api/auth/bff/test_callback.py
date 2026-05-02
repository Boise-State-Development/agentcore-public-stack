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


def _seed_state(state: str = "valid-state", *, redirect_uri: str | None = None) -> str:
    """Push a state token through the same store the route reads from."""
    from .conftest import CALLBACK_URL

    store = bff_routes._get_state_store()
    from apis.shared.auth.state_store import OIDCStateData

    store.store_state(
        state,
        OIDCStateData(redirect_uri=redirect_uri or CALLBACK_URL, provider_id="cognito-bff"),
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
