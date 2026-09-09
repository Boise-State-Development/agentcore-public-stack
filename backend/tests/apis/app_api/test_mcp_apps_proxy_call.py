"""Tests for the cookie-authenticated MCP App tools/call proxy (PR #5).

Mirrors `test_proxy_routes.py`: the upstream client seam
(`proxy_routes._build_upstream_client`) is swapped for a MockTransport so
the relay to inference-api `/invocations` is asserted without a network.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.chat import proxy_routes
from apis.app_api.mcp_apps.routes import router as mcp_apps_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User


def _user(raw_token: str = "access.token.value") -> User:
    user = User(
        email="alice@example.com",
        user_id="user-sub",
        name="Alice",
        roles=["user"],
    )
    user.raw_token = raw_token
    return user


def _build_app(*, user_override: Optional[User] = None) -> FastAPI:
    app = FastAPI()
    app.include_router(mcp_apps_router)
    if user_override is not None:
        app.dependency_overrides[get_current_user_from_session] = (
            lambda: user_override
        )
    return app


def _patch_upstream(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        proxy_routes,
        "_build_upstream_client",
        lambda: httpx.AsyncClient(transport=transport),
    )


_BODY = {
    "sessionId": "sess-1",
    "toolUseId": "tu-1",
    "toolName": "widget_tool",
    "arguments": {"q": "x"},
    "enabledTools": ["gateway_widget"],
    "modelId": "m1",
}


def test_requires_session() -> None:
    # No auth override → get_current_user_from_session rejects.
    resp = TestClient(_build_app()).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 401


def test_relays_directive_and_bearer_then_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "toolUseId": "tu-1",
                "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
            },
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user("tok-abc"))

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)

    assert resp.status_code == 200
    assert resp.json()["result"]["content"][0]["text"] == "ok"
    assert seen["url"].endswith("/invocations")
    assert seen["auth"] == "Bearer tok-abc"
    # The conversation binding + directive are forwarded verbatim.
    assert seen["body"]["session_id"] == "sess-1"
    assert seen["body"]["enabled_tools"] == ["gateway_widget"]
    assert seen["body"]["app_tool_call"] == {
        "tool_use_id": "tu-1",
        "tool_name": "widget_tool",
        "arguments": {"q": "x"},
    }


def test_relays_inference_error_status_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # inference-api rejected the tool as not app-visible (spec MUST gate).
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "not app-visible"})

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user())

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 403
    assert resp.json()["error"] == "not app-visible"


def test_maps_unreachable_inference_to_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user())

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 502


# --- AgentCore envelope translation ----------------------------------------
#
# inference-api can't use HTTP status to report an app-tool error: AgentCore
# Runtime rewrites any non-2xx to a generic 424 and drops the message. It
# answers 200 + `appToolError` instead, and app-api restores the real status
# here. See `apis/shared/mcp_apps/error_envelope.py`.


def test_restores_status_and_message_from_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consent case that reached users as "check your CloudWatch logs"."""
    consent = (
        "Authorization required for 'google_tasks'. Connect the account, "
        "then try again."
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"appToolError": {"code": 409, "message": consent}}
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user())

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 409
    assert resp.json()["error"] == consent


def test_enveloped_error_never_relays_a_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 would trip the SPA interceptor and sign the user out."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"appToolError": {"code": 401, "message": "nope"}}
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user())

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 502


def test_enveloped_error_persists_no_provenance_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enveloped error is a failed call — it must not look like a success.

    The card write is gated on the upstream's 200, and an envelope now
    arrives *with* a 200, so this is the regression the ordering guards
    against.
    """
    stored: list[dict] = []

    class _Store:
        def store(self, **kwargs: object) -> None:
            stored.append(dict(kwargs))

    monkeypatch.setattr(
        "apis.app_api.mcp_apps.routes.get_app_card_store", lambda: _Store()
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"appToolError": {"code": 409, "message": "connect"}}
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user())

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 409
    assert stored == []


def test_successful_call_still_persists_a_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the test above: the envelope check must not swallow success."""
    stored: list[dict] = []

    class _Store:
        def store(self, **kwargs: object) -> None:
            stored.append(dict(kwargs))

    monkeypatch.setattr(
        "apis.app_api.mcp_apps.routes.get_app_card_store", lambda: _Store()
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "toolUseId": "tu-1",
                "result": {"content": [{"text": "ok"}], "isError": False},
            },
        )

    _patch_upstream(monkeypatch, handler)
    app = _build_app(user_override=_user())

    resp = TestClient(app).post("/mcp-apps/proxy-call", json=_BODY)
    assert resp.status_code == 200
    assert len(stored) == 1
    assert stored[0]["tool_name"] == "widget_tool"
