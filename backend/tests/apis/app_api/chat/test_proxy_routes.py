"""Tests for `POST /chat/proxy-stream` (Phase 4 BFF chat proxy).

Covers the proxy mechanics in isolation — auth gate, body/header relay,
SSE streaming, and error mapping. The full SessionRefreshMiddleware ↔
CSRFMiddleware stack is exercised separately in
`test_proxy_routes_csrf.py`.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.app_api.chat import proxy_routes
from apis.app_api.chat.proxy_routes import router as proxy_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.models import SessionRecord


def _record() -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id="sess-001",
        user_id="user-sub",
        username="alice",
        cognito_access_token="access.token.value",
        cognito_refresh_token="refresh.token.value",
        id_token="id.token.value",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


def _user(*, raw_token: str = "access.token.value") -> User:
    user = User(
        email="alice@example.com",
        user_id="user-sub",
        name="Alice",
        roles=["user"],
    )
    user.raw_token = raw_token
    return user


class _AttachSession(BaseHTTPMiddleware):
    """Minimal stand-in for SessionRefreshMiddleware — sets bff_session."""

    def __init__(self, app, record: Optional[SessionRecord]) -> None:
        super().__init__(app)
        self._record = record

    async def dispatch(self, request, call_next):
        if self._record is not None:
            request.state.bff_session = self._record
        return await call_next(request)


def _build_app(
    *,
    record: Optional[SessionRecord] = None,
    user_override: Optional[User] = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_AttachSession, record=record)
    app.include_router(proxy_router)
    if user_override is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user_override
    return app


def _patch_upstream(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Replace the proxy's upstream-client builder with a MockTransport-
    backed one. The seam lives in `proxy_routes._build_upstream_client`
    so we don't have to mutate global `httpx.AsyncClient`."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        proxy_routes,
        "_build_upstream_client",
        lambda: httpx.AsyncClient(transport=transport),
    )


# ── Auth gate ─────────────────────────────────────────────────────────────


def test_returns_401_when_no_session_attached() -> None:
    app = _build_app(record=None)
    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 401


# ── Happy path: SSE relay ─────────────────────────────────────────────────


def test_relays_sse_response_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    sse_body = (
        b'event: message_start\ndata: {"role": "assistant"}\n\n'
        b'event: content_block_delta\ndata: {"text": "hello"}\n\n'
        b'event: done\ndata: {}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/invocations"
        assert request.method == "POST"
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == sse_body


def test_forwards_authorization_bearer_from_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user(raw_token="the-stored-token"))

    TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert captured["authorization"] == "Bearer the-stored-token"


def test_forwards_request_body_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    payload = b'{"session_id":"s1","message":"hello there","enabled_tools":["foo"]}'
    TestClient(app).post(
        "/chat/proxy-stream",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert captured["body"] == payload
    assert captured["content_type"] == "application/json"


def test_targets_invocations_path_on_inference_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_routes, "_INFERENCE_API_URL", "http://upstream:9999")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert captured["url"] == "http://upstream:9999/invocations"


# ── Non-SSE relay (e.g. inference-api returns JSON validation error pre-stream) ──


def test_relays_non_sse_response_with_status_and_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate inference-api returning a non-streaming success — a small
        # JSON body that should be passed through, not re-wrapped as SSE.
        return httpx.Response(
            200,
            content=b'{"ok": true}',
            headers={"content-type": "application/json"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == b'{"ok": true}'


# ── Upstream error propagation ────────────────────────────────────────────


def test_propagates_upstream_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"token rejected by inference-api")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 401
    assert "token rejected" in response.text


def test_propagates_upstream_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream overloaded")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 503


def test_returns_502_when_upstream_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 502
    assert response.json()["detail"] == "Inference API is unreachable"


def test_returns_504_on_upstream_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 504


def test_returns_502_on_unexpected_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("something is on fire")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post("/chat/proxy-stream", json={"message": "hi"})
    assert response.status_code == 502
