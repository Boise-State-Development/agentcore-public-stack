"""Tests for app-initiated tools/call dispatch (MCP Apps PR #5).

Mocks the boundary the way the PR #3 tests do: a fake MCP client +
`UIToolCatalog`, no live agent. Asserts the spec-MUST app-visibility gate
at the inference-api dispatch, and that a successful call publishes
synthesized tool_use/tool_result into the per-session broker.
"""

import pytest

from apis.inference_api.chat.app_tool_dispatch import (
    AppToolCallError,
    dispatch_app_tool_call,
)
from apis.shared.mcp_apps.broker import get_app_tool_event_broker
from apis.shared.tools.models import ToolUIMetadata
from agents.main_agent.integrations import mcp_apps as mcp_apps_mod


class _FakeContent:
    def __init__(self, text: str) -> None:
        self._text = text

    def model_dump(self, **_: object) -> dict:
        return {"type": "text", "text": self._text}


class _FakeResult:
    def __init__(self, text: str = "ok", is_error: bool = False) -> None:
        self.content = [_FakeContent(text)]
        self.isError = is_error


class _FakeClient:
    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self._result = result if result is not None else _FakeResult()
        self._raises = raises
        self.calls: list = []

    def call_tool_sync(self, tool_use_id, name, arguments=None):
        self.calls.append((tool_use_id, name, arguments))
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeCatalog:
    def __init__(self, meta=None, client=None) -> None:
        self._meta = meta
        self._client = client

    def get(self, _name):
        return self._meta

    def get_client(self, _name):
        return self._client


def _patch(monkeypatch, *, enabled=True, meta=None, client=None):
    monkeypatch.setattr(
        mcp_apps_mod, "is_mcp_apps_host_enabled", lambda: enabled
    )
    monkeypatch.setattr(
        mcp_apps_mod,
        "get_ui_tool_catalog",
        lambda: _FakeCatalog(meta=meta, client=client),
    )


def _ui(visibility):
    return ToolUIMetadata(resource_uri="ui://srv/w", visibility=visibility)


async def _call(session_id="disp-s1", tool_name="widget_tool"):
    return await dispatch_app_tool_call(
        agent=None,
        session_id=session_id,
        user_id="u1",
        tool_use_id="tu-1",
        tool_name=tool_name,
        arguments={"q": "x"},
    )


@pytest.mark.asyncio
async def test_rejects_when_host_flag_disabled(monkeypatch):
    _patch(monkeypatch, enabled=False)
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 403


@pytest.mark.asyncio
async def test_rejects_unknown_tool(monkeypatch):
    _patch(monkeypatch, enabled=True, meta=None, client=_FakeClient())
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 403


@pytest.mark.asyncio
async def test_rejects_tool_not_app_visible(monkeypatch):
    # visibility=["model"] → callable by the model, NOT by an app.
    _patch(
        monkeypatch,
        enabled=True,
        meta=_ui(["model"]),
        client=_FakeClient(),
    )
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 403


@pytest.mark.asyncio
async def test_rejects_when_no_live_client(monkeypatch):
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=None)
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 409


@pytest.mark.asyncio
async def test_dispatch_failure_maps_to_502(monkeypatch):
    _patch(
        monkeypatch,
        enabled=True,
        meta=_ui(["app"]),
        client=_FakeClient(raises=RuntimeError("boom")),
    )
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 502


@pytest.mark.asyncio
async def test_success_returns_result_and_publishes_thread_events(monkeypatch):
    client = _FakeClient(_FakeResult("hello"))
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    broker = get_app_tool_event_broker()
    q = broker.add_subscriber("disp-ok")
    try:
        payload = await dispatch_app_tool_call(
            agent=None,
            session_id="disp-ok",
            user_id="u1",
            tool_use_id="tu-9",
            tool_name="widget_tool",
            arguments={"q": "x"},
        )
    finally:
        events = broker.drain(q)
        broker.remove_subscriber("disp-ok", q)

    assert payload["toolUseId"] == "tu-9"
    assert payload["result"]["isError"] is False
    assert payload["result"]["content"] == [{"type": "text", "text": "hello"}]
    # The MCP client was called with a synthesized (distinct) id.
    assert client.calls[0][1] == "widget_tool"
    assert client.calls[0][0] != "tu-9"
    # Both thread events were published, tool_use before tool_result.
    types = [e["type"] for e in events]
    assert types == ["tool_use", "tool_result"]
    assert events[0]["data"]["tool_use"]["name"] == "widget_tool"
    assert events[0]["data"]["tool_use"]["origin"] == "mcp_app"
    assert events[1]["data"]["tool_result"]["status"] == "success"
