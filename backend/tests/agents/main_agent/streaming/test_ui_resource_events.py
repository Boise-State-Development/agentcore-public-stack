"""Tests for StreamCoordinator._extract_ui_resource_events.

PR #3 of the MCP Apps host-renderer initiative
(`docs/kaizen/scoping/mcp-apps-host-renderer.md`). Covers the per-tool-result
`ui_resource` SSE emit: it fires only for UI-bearing tools, fetches the
resource via the hosting client's `resources/read` and inlines the HTML,
correlates by toolUseId, dedupes, stays inert behind the host flag, and
never breaks the stream on failure.

Mirrors the helper-level style of `test_artifact_events.py` (drive the
coordinator method directly) and the mock-the-boundary catalog seeding from
`tests/agents/main_agent/integrations/test_mcp_apps.py`.
"""

from __future__ import annotations

import json

import mcp.types as mcp_types
import pytest

from agents.main_agent.integrations import mcp_apps
from agents.main_agent.integrations.mcp_apps import (
    MCP_APPS_UI_EXTENSION_KEY,
    MCP_APPS_UI_MIME_TYPE,
    get_ui_tool_catalog,
    record_and_filter_ui_tools,
)
from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

_ENV_FLAG = "AGENTCORE_MCP_APPS_HOST_ENABLED"
_ENV_SANDBOX_ORIGIN = "AGENTCORE_MCP_APPS_SANDBOX_ORIGIN"


@pytest.fixture
def coord() -> StreamCoordinator:
    return StreamCoordinator()


@pytest.fixture
def catalog_clean(monkeypatch):
    get_ui_tool_catalog().clear()
    monkeypatch.delenv(_ENV_FLAG, raising=False)
    monkeypatch.delenv(_ENV_SANDBOX_ORIGIN, raising=False)
    try:
        yield
    finally:
        get_ui_tool_catalog().clear()


class _FakeMCPClient:
    def __init__(self, result):
        self._result = result
        self.read_calls: list = []

    def read_resource_sync(self, uri):
        self.read_calls.append(uri)
        return self._result


def _fake_tool(tool_name, ui):
    from types import SimpleNamespace

    return SimpleNamespace(
        tool_name=tool_name,
        mcp_tool=SimpleNamespace(name=tool_name, meta={"ui": ui}),
    )


def _html_result(text="<h1>hi</h1>"):
    return mcp_types.ReadResourceResult(
        contents=[
            mcp_types.TextResourceContents(
                uri="ui://srv/widget",
                mimeType=MCP_APPS_UI_MIME_TYPE,
                text=text,
                _meta={
                    MCP_APPS_UI_EXTENSION_KEY: {
                        "csp": {"connectDomains": ["https://api.test"]},
                        "permissions": {"clipboardWrite": {}},
                    }
                },
            )
        ]
    )


def _seed(monkeypatch, client):
    monkeypatch.setenv(_ENV_FLAG, "true")
    record_and_filter_ui_tools(
        [_fake_tool("widget", {"resourceUri": "ui://srv/widget"})],
        client=client,
    )


def _tool_result_event(tool_use_id="tu-1"):
    return {
        "type": "tool_result",
        "data": {
            "tool_result": {
                "toolUseId": tool_use_id,
                "status": "success",
                "content": [{"text": "ok"}],
            }
        },
    }


def _parse(raw: str) -> dict:
    assert raw.startswith("event: ui_resource\ndata: ")
    assert raw.endswith("\n\n")
    return json.loads(raw[len("event: ui_resource\ndata: ") :].strip())


@pytest.mark.asyncio
async def test_emits_ui_resource_with_inline_html(
    coord, catalog_clean, monkeypatch
):
    client = _FakeMCPClient(_html_result("<main>app</main>"))
    _seed(monkeypatch, client)

    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, set()
    )

    assert client.read_calls == ["ui://srv/widget"]
    assert len(out) == 1
    payload = _parse(out[0])
    assert payload == {
        "type": "ui_resource",
        "toolUseId": "tu-1",
        "resourceUri": "ui://srv/widget",
        "html": "<main>app</main>",
        "mimeType": MCP_APPS_UI_MIME_TYPE,
        "csp": {"connectDomains": ["https://api.test"]},
        "permissions": {"clipboardWrite": {}},
        "sandboxOrigin": "",
    }


@pytest.mark.asyncio
async def test_dedupes_per_tool_use_id(coord, catalog_clean, monkeypatch):
    client = _FakeMCPClient(_html_result())
    _seed(monkeypatch, client)
    emitted: set = set()

    first = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, emitted
    )
    second = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, emitted
    )

    assert len(first) == 1
    assert second == []
    assert emitted == {"tu-1"}
    # The dedupe must short-circuit before a second resources/read.
    assert client.read_calls == ["ui://srv/widget"]


@pytest.mark.asyncio
async def test_inert_when_flag_disabled(coord, catalog_clean, monkeypatch):
    client = _FakeMCPClient(_html_result())
    _seed(monkeypatch, client)
    monkeypatch.setenv(_ENV_FLAG, "false")

    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, set()
    )
    assert out == []
    assert client.read_calls == []


@pytest.mark.asyncio
async def test_noop_for_untracked_tool_use_id(
    coord, catalog_clean, monkeypatch
):
    client = _FakeMCPClient(_html_result())
    _seed(monkeypatch, client)

    # No name learned for this toolUseId → cannot map to the catalog.
    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-unknown"), {}, set()
    )
    assert out == []
    assert client.read_calls == []


@pytest.mark.asyncio
async def test_noop_when_tool_result_has_no_tool_use_id(
    coord, catalog_clean, monkeypatch
):
    client = _FakeMCPClient(_html_result())
    _seed(monkeypatch, client)

    event = {"type": "tool_result", "data": {"tool_result": {"status": "ok"}}}
    out = await coord._extract_ui_resource_events(
        event, {"tu-1": "widget"}, set()
    )
    assert out == []


@pytest.mark.asyncio
async def test_noop_for_non_ui_tool(coord, catalog_clean, monkeypatch):
    # Flag on, but the tool has no `_meta.ui` in the catalog at all.
    monkeypatch.setenv(_ENV_FLAG, "true")
    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "plain_tool"}, set()
    )
    assert out == []


@pytest.mark.asyncio
async def test_failure_is_swallowed(coord, catalog_clean, monkeypatch):
    _seed(monkeypatch, _FakeMCPClient(_html_result()))

    def _boom(tool_name, tool_use_id):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(mcp_apps, "fetch_ui_resource", _boom)

    # A failure in the fetch path must not propagate into the live stream.
    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, set()
    )
    assert out == []
