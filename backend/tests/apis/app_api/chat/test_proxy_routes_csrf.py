"""CSRF integration tests for the BFF chat proxy.

`/chat/stream` is a POST that rides the BFF session cookie, so it MUST
be guarded by CSRFMiddleware. This file confirms (a) the route is not
accidentally listed in the middleware's exempt set, and (b) a valid
double-submit token allows the request through.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.app_api.chat import proxy_routes
from apis.app_api.chat.proxy_routes import router as proxy_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.middleware.csrf import CSRFMiddleware
from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import SessionRecord


def _record() -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id="sess-csrf-001",
        user_id="user-sub",
        username="alice",
        cognito_access_token="access.token",
        cognito_refresh_token="refresh.token",
        id_token="id.token",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


def _user() -> User:
    user = User(
        email="alice@example.com",
        user_id="user-sub",
        name="Alice",
        roles=["user"],
    )
    user.raw_token = "access.token"
    return user


class _AttachSession(BaseHTTPMiddleware):
    """Stand-in for SessionRefreshMiddleware that just sets bff_session."""

    def __init__(self, app, record: Optional[SessionRecord]) -> None:
        super().__init__(app)
        self._record = record

    async def dispatch(self, request, call_next):
        if self._record is not None:
            request.state.bff_session = self._record
        return await call_next(request)


def _build_app(record: Optional[SessionRecord]) -> FastAPI:
    app = FastAPI()
    # Order matters: Starlette runs the LAST-added middleware first, so this
    # reflects the production stack in main.py — CSRFMiddleware sees the
    # session that AttachSession (a stand-in for SessionRefreshMiddleware)
    # populated.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(_AttachSession, record=record)
    app.include_router(proxy_router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return app


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=b"event: done\ndata: {}\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _patch_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(_ok_handler)
    monkeypatch.setattr(
        proxy_routes,
        "_build_upstream_client",
        lambda: httpx.AsyncClient(transport=transport),
    )


@pytest.fixture
def chat_path() -> str:
    return "/chat/stream"


def test_proxy_stream_without_csrf_returns_403(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    """Session attached but no CSRF cookie/header → CSRF middleware rejects."""
    _patch_upstream(monkeypatch)
    app = _build_app(record=_record())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_proxy_stream_with_valid_csrf_passes(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    record = _record()
    csrf_token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)

    _patch_upstream(monkeypatch)
    app = _build_app(record=record)

    response = TestClient(app).post(
        chat_path,
        json={"message": "hi"},
        cookies={CSRF_COOKIE_NAME: csrf_token},
        headers={CSRF_HEADER_NAME: csrf_token},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


def test_proxy_stream_csrf_header_mismatch_returns_403(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
) -> None:
    """Cookie present but header value differs — classic forged-form scenario."""
    record = _record()
    csrf_token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)

    _patch_upstream(monkeypatch)
    app = _build_app(record=record)

    response = TestClient(app).post(
        chat_path,
        json={"message": "hi"},
        cookies={CSRF_COOKIE_NAME: csrf_token},
        headers={CSRF_HEADER_NAME: "different-token"},
    )
    assert response.status_code == 403


def test_chat_proxy_path_not_in_csrf_exempt_set() -> None:
    """Future-proofing: surface a regression if someone adds the chat
    proxy path to the CSRF exempt list. The point of this proxy is to be
    guarded — exempting it would defeat the whole BFF-cookie security
    model."""
    from apis.shared.middleware.csrf import _EXEMPT_PATHS

    assert "/chat/stream" not in _EXEMPT_PATHS
