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
