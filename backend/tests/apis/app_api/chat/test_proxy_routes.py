"""Tests for the BFF chat proxy.

Covers the proxy mechanics in isolation — auth gate, body/header relay,
SSE streaming, and error mapping. The full SessionRefreshMiddleware ↔
CSRFMiddleware stack is exercised separately in `test_proxy_routes_csrf.py`.
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


@pytest.fixture
def chat_path() -> str:
    return "/chat/stream"


# ── Auth gate ─────────────────────────────────────────────────────────────


def test_returns_401_when_no_session_attached(chat_path: str) -> None:
    app = _build_app(record=None)
    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 401


# ── Happy path: SSE relay ─────────────────────────────────────────────────


def test_relays_sse_response_verbatim(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
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

    response = TestClient(app).post(chat_path, json={"message": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == sse_body


def test_forwards_authorization_bearer_from_session(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user(raw_token="the-stored-token"))

    TestClient(app).post(chat_path, json={"message": "hi"})
    assert captured["authorization"] == "Bearer the-stored-token"


def test_forwards_request_body_verbatim(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
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
        chat_path,
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert captured["body"] == payload
    assert captured["content_type"] == "application/json"


def test_forwards_oauth2_callback_url_header(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
) -> None:
    """The SPA sets OAuth2CallbackUrl on /chat/stream so inference-api's
    AgentCoreContextMiddleware can scope on-tool OAuth consent redirects
    back to the SPA's origin. The proxy must forward it verbatim — without
    it, `oauth_required` SSE events can't complete a consent flow."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["oauth_callback"] = request.headers.get("oauth2callbackurl")
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    TestClient(app).post(
        chat_path,
        json={"message": "hi"},
        headers={"OAuth2CallbackUrl": "https://app.example.com/oauth-complete"},
    )
    assert captured["oauth_callback"] == "https://app.example.com/oauth-complete"


def test_omits_oauth2_callback_url_header_when_caller_did_not_send_one(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
) -> None:
    """No SPA-supplied header → don't synthesize one. Inference-api falls
    back to its env-var default (set by CDK) when missing."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["oauth_callback"] = request.headers.get("oauth2callbackurl")
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    TestClient(app).post(chat_path, json={"message": "hi"})
    assert captured["oauth_callback"] is None


def test_targets_invocations_path_on_inference_api(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
) -> None:
    monkeypatch.setenv("INFERENCE_API_URL", "http://upstream:9999")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    TestClient(app).post(chat_path, json={"message": "hi"})
    assert captured["url"] == "http://upstream:9999/invocations"


def test_agentcore_runtime_url_encodes_arn_and_appends_qualifier(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
) -> None:
    """AgentCore Runtime data plane requires a URL-encoded ARN segment and
    a `qualifier` query string. SSM stores the ARN unencoded, so the proxy
    has to re-encode the path and append `qualifier=DEFAULT`. Without this,
    AWS returns 404 because the unencoded `/` in the ARN splits the path
    into too many segments.
    """
    monkeypatch.setenv(
        "INFERENCE_API_URL",
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/foo-AbCdEf",
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, content=b"event: done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    TestClient(app).post(chat_path, json={"message": "hi"})
    assert captured["url"] == (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3Aruntime"
        "%2Ffoo-AbCdEf/invocations?qualifier=DEFAULT"
    )


# ── Non-SSE relay (e.g. inference-api returns JSON validation error pre-stream) ──


def test_relays_non_sse_response_with_status_and_content_type(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
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

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == b'{"ok": true}'


# ── Upstream error propagation ────────────────────────────────────────────


def test_propagates_upstream_4xx(monkeypatch: pytest.MonkeyPatch, chat_path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"token rejected by inference-api")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 401
    assert "token rejected" in response.text


def test_propagates_upstream_5xx(monkeypatch: pytest.MonkeyPatch, chat_path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream overloaded")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 503


def test_returns_502_when_upstream_unreachable(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 502
    assert response.json()["detail"] == "Inference API is unreachable"


def test_returns_504_on_upstream_timeout(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 504


def test_returns_502_on_unexpected_upstream_error(
    monkeypatch: pytest.MonkeyPatch, chat_path: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("something is on fire")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 502


# ── AgentCore's 424 rewrite of the single-flight 409 ──────────────────────
#
# The Runtime data plane maps every non-2xx from the container to `424 Failed
# Dependency`, so the container's deliberate 409 arrives indistinguishable
# from a crash. The lease is the tiebreaker.


def _patch_lease_held(monkeypatch: pytest.MonkeyPatch, held: bool) -> None:
    import apis.shared.sessions.session_lease as session_lease

    async def _is_held(session_id: str, user_id: str) -> bool:
        assert session_id == "conv-1"
        assert user_id == "user-sub"
        return held

    monkeypatch.setattr(session_lease, "is_session_lease_held", _is_held)


def _upstream_424(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(424, content=b'{"message": "Failed Dependency"}')

    _patch_upstream(monkeypatch, handler)


def test_424_with_live_lease_relays_as_409(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    _upstream_424(monkeypatch)
    _patch_lease_held(monkeypatch, True)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(
        chat_path, json={"message": "hi", "session_id": "conv-1"}
    )

    # 409 is the status the SPA renders as a soft "Already responding" notice;
    # a relayed 424 surfaces as a fatal "Chat Request Failed" toast instead.
    assert response.status_code == 409
    # The Runtime's opaque body is replaced — it explains nothing to the user.
    assert "already streaming" in response.json()["detail"]


def test_424_without_a_lease_stays_424(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    _upstream_424(monkeypatch)
    _patch_lease_held(monkeypatch, False)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(
        chat_path, json={"message": "hi", "session_id": "conv-1"}
    )

    # No lease means no turn is streaming, so the 424 is a real upstream
    # failure and must not be disguised as a benign conflict.
    assert response.status_code == 424
    assert "Failed Dependency" in response.text


def test_424_without_a_session_id_stays_424(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    _upstream_424(monkeypatch)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(chat_path, json={"message": "hi"})
    assert response.status_code == 424


def test_424_keeps_its_status_when_the_lease_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    import apis.shared.sessions.session_lease as session_lease

    async def _boom(session_id: str, user_id: str) -> bool:
        raise RuntimeError("DynamoDB unavailable")

    _upstream_424(monkeypatch)
    monkeypatch.setattr(session_lease, "is_session_lease_held", _boom)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(
        chat_path, json={"message": "hi", "session_id": "conv-1"}
    )
    assert response.status_code == 424


def test_non_424_errors_skip_the_lease_lookup(
    monkeypatch: pytest.MonkeyPatch, chat_path: str
) -> None:
    import apis.shared.sessions.session_lease as session_lease

    async def _must_not_run(session_id: str, user_id: str) -> bool:
        raise AssertionError("lease lookup is 424-only")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    _patch_upstream(monkeypatch, handler)
    monkeypatch.setattr(session_lease, "is_session_lease_held", _must_not_run)
    app = _build_app(record=_record(), user_override=_user())

    response = TestClient(app).post(
        chat_path, json={"message": "hi", "session_id": "conv-1"}
    )
    assert response.status_code == 500
