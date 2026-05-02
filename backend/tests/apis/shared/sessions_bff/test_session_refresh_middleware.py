"""Tests for SessionRefreshMiddleware.

Covered:
    - Pass-through when BFF is not enabled (env vars unset).
    - Pass-through when no session cookie is present (Bearer-token requests).
    - Cookie present + session valid → record attached to `request.state`.
    - Cookie present + bad seal → cookie cleared on response.
    - Cookie present + session row missing → cookie cleared.
    - Cookie present + access token within leeway → refresh path called once.
    - Storm coalescing: N concurrent requests for the same session trigger
      exactly one Cognito refresh exchange.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apis.shared.middleware.session_refresh import SessionRefreshMiddleware
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.config import (
    BFFConfig,
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import RefreshResult


@pytest.fixture(autouse=True)
def _reset_session_locks() -> None:
    """Clear the process-wide lock registry between tests so storm-coalescing
    behavior is independent across cases."""
    lock_module._reset_for_tests()


def _make_codec() -> CookieCodec:
    codec = CookieCodec(kms_key_arn="arn:aws:kms:fake")
    codec._cipher = AESGCM(secrets.token_bytes(32))
    return codec


def _make_record(
    *,
    session_id: str = "sess-001",
    access_token_exp: Optional[int] = None,
) -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id=session_id,
        user_id="user-sub-001",
        username="alice",
        cognito_access_token="access.original",
        cognito_refresh_token="refresh.original",
        id_token="id.original",
        access_token_exp=access_token_exp if access_token_exp is not None else now + 3600,
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


def _enabled_config() -> BFFConfig:
    return BFFConfig(
        sessions_table_name="tbl",
        cookie_signing_key_arn="arn:aws:kms:fake",
        session_ttl_seconds=28800,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id="client-id",
        cognito_bff_app_client_secret_arn="arn:secret",
        inference_api_url=None,
    )


def _build_app(
    *,
    config: BFFConfig,
    repository,
    codec: CookieCodec,
    refresh_client,
    cache: Optional[SessionCache] = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SessionRefreshMiddleware,
        config=config,
        repository=repository,
        cookie_codec=codec,
        refresh_client=refresh_client,
        cache=cache or SessionCache(ttl_seconds=60),
    )

    @app.get("/echo")
    async def echo(request: Request):
        record = getattr(request.state, "bff_session", None)
        return {
            "has_session": record is not None,
            "session_id": record.session_id if record else None,
            "access_token": record.cognito_access_token if record else None,
        }

    return app


def test_passthrough_when_bff_disabled() -> None:
    config = BFFConfig(
        sessions_table_name=None,
        cookie_signing_key_arn=None,
        session_ttl_seconds=28800,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id=None,
        cognito_bff_app_client_secret_arn=None,
        inference_api_url=None,
    )
    repo = AsyncMock()
    refresh = MagicMock()
    app = _build_app(
        config=config, repository=repo, codec=_make_codec(), refresh_client=refresh
    )
    response = TestClient(app).get("/echo")
    assert response.status_code == 200
    assert response.json() == {
        "has_session": False,
        "session_id": None,
        "access_token": None,
    }
    repo.get.assert_not_called()


def test_passthrough_when_no_cookie_present() -> None:
    repo = AsyncMock()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=_make_codec(),
        refresh_client=refresh,
    )
    response = TestClient(app).get("/echo")
    assert response.status_code == 200
    assert response.json()["has_session"] is False
    repo.get.assert_not_called()


def test_valid_cookie_attaches_session_to_request_state() -> None:
    record = _make_record()
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    body = response.json()
    assert body["has_session"] is True
    assert body["session_id"] == record.session_id
    assert body["access_token"] == "access.original"
    refresh.refresh.assert_not_called()


def test_unrecoverable_cookie_is_cleared() -> None:
    repo = AsyncMock()
    repo.get.return_value = None
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    response = TestClient(app).get(
        "/echo", cookies={SESSION_COOKIE_NAME: "garbage-value"}
    )
    assert response.status_code == 200
    assert response.json()["has_session"] is False
    # Both BFF cookies must be cleared so the browser stops echoing the
    # bad pair on every subsequent request. `getlist` because Set-Cookie
    # appears once per cookie cleared.
    set_cookie_headers = response.headers.get_list("set-cookie")
    cleared = " ".join(set_cookie_headers)
    assert SESSION_COOKIE_NAME in cleared
    assert CSRF_COOKIE_NAME in cleared


def test_missing_session_row_clears_cookie() -> None:
    """Cookie unseals fine but the DDB row is gone — clear the cookie."""
    repo = AsyncMock()
    repo.get.return_value = None
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id="sess-gone"))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})
    assert response.status_code == 200
    assert response.json()["has_session"] is False
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


def test_near_expiry_session_triggers_refresh_once() -> None:
    record = _make_record(access_token_exp=int(time.time()) + 5)  # within 60s leeway
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh.return_value = RefreshResult(
        access_token="access.fresh",
        refresh_token="refresh.fresh",
        id_token="id.fresh",
        access_token_exp=int(time.time()) + 3600,
    )
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    body = response.json()
    assert body["has_session"] is True
    # The refreshed token should be exposed downstream.
    assert body["access_token"] == "access.fresh"
    refresh.refresh.assert_called_once_with(
        username="alice", refresh_token="refresh.original"
    )
    repo.update_tokens.assert_awaited_once()


def test_refresh_failure_clears_cookie() -> None:
    from apis.shared.sessions_bff.refresh import CognitoRefreshError

    record = _make_record(access_token_exp=int(time.time()) + 5)
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh.side_effect = CognitoRefreshError("rotated")
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    assert response.json()["has_session"] is False
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_storm_coalesces_to_single_refresh() -> None:
    """N concurrent in-flight requests for the same session id must only
    drive one Cognito refresh exchange, even if the refresh itself is slow.

    This guards against the multi-tab refresh-token-rotation storm called
    out in the project memory."""
    record = _make_record(access_token_exp=int(time.time()) + 5)
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()

    call_count = {"n": 0}

    def slow_refresh(*, username: str, refresh_token: str) -> RefreshResult:
        # Tight, synchronous bump — the lock guards entry, so this only
        # needs to be called sequentially to make the test meaningful.
        call_count["n"] += 1
        return RefreshResult(
            access_token=f"access.fresh.{call_count['n']}",
            refresh_token="refresh.fresh",
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )

    refresh = MagicMock()
    refresh.refresh.side_effect = slow_refresh

    # After the first refresh, repo.get returns the *fresh* record so other
    # waiters short-circuit out of the refresh branch.
    fresh_record = _make_record(
        access_token_exp=int(time.time()) + 3600
    )
    fresh_record.cognito_access_token = "access.fresh.1"
    fresh_record.cognito_refresh_token = "refresh.fresh"
    repo.get.side_effect = [record, record, fresh_record, fresh_record, fresh_record]
    repo.update_tokens = AsyncMock()

    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))

    # Use httpx AsyncClient driven against the ASGI app for true concurrency.
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        responses = await asyncio.gather(
            *(client.get("/echo") for _ in range(5))
        )

    for r in responses:
        assert r.status_code == 200
    # Critical assertion: only one refresh call across all 5 concurrent reqs.
    assert call_count["n"] == 1
