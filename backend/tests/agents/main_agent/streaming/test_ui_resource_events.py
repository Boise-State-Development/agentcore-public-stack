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
from apis.shared.mcp_apps import ui_resource_store

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
        # No serverInfo on the fake client → authority fallback ("srv" → "Srv").
        "serverName": "Srv",
        "icon": "",
        "toolName": "widget",
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


class _FakeUiResourceStore:
    def __init__(self) -> None:
        self.calls: list = []

    def store(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_persists_resource_when_session_and_user_provided(
    coord, catalog_clean, monkeypatch
):
    client = _FakeMCPClient(_html_result("<main>app</main>"))
    _seed(monkeypatch, client)
    fake = _FakeUiResourceStore()
    monkeypatch.setattr(ui_resource_store, "get_ui_resource_store", lambda: fake)

    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"),
        {"tu-1": "widget"},
        set(),
        session_id="sess-1",
        user_id="user-1",
    )

    # The live event still streams unchanged...
    assert len(out) == 1
    # ...and the resource is persisted for reload survival with the same
    # fields the SPA re-seeds McpAppStateService from.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["session_id"] == "sess-1"
    assert call["user_id"] == "user-1"
    assert call["tool_use_id"] == "tu-1"
    assert call["resource_uri"] == "ui://srv/widget"
    assert call["html"] == "<main>app</main>"
    assert call["mime_type"] == MCP_APPS_UI_MIME_TYPE
    assert call["csp"] == {"connectDomains": ["https://api.test"]}
    assert call["permissions"] == {"clipboardWrite": {}}


@pytest.mark.asyncio
async def test_does_not_persist_without_session_or_user(
    coord, catalog_clean, monkeypatch
):
    client = _FakeMCPClient(_html_result())
    _seed(monkeypatch, client)
    fake = _FakeUiResourceStore()
    monkeypatch.setattr(ui_resource_store, "get_ui_resource_store", lambda: fake)

    # No session/user (e.g. a context without a persisted conversation) →
    # emit live but skip persistence.
    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, set()
    )
    assert len(out) == 1
    assert fake.calls == []


@pytest.mark.asyncio
async def test_persistence_failure_does_not_break_stream(
    coord, catalog_clean, monkeypatch
):
    _seed(monkeypatch, _FakeMCPClient(_html_result()))

    class _Boom:
        def store(self, **kwargs):
            raise RuntimeError("dynamo exploded")

    monkeypatch.setattr(
        ui_resource_store, "get_ui_resource_store", lambda: _Boom()
    )

    # A persistence failure is best-effort — the live event still streams.
    out = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"),
        {"tu-1": "widget"},
        set(),
        session_id="sess-1",
        user_id="user-1",
    )
    assert len(out) == 1


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


# ---------------------------------------------------------------------------
# Early frame mount + streamed partial tool input (SEP-1865 tool-input-partial)
# ---------------------------------------------------------------------------


def _parse_partial(raw: str) -> dict:
    prefix = "event: ui_tool_input_partial\ndata: "
    assert raw.startswith(prefix)
    assert raw.endswith("\n\n")
    return json.loads(raw[len(prefix) :].strip())


@pytest.mark.asyncio
async def test_early_mount_emits_and_dedupes_against_tool_result(
    coord, catalog_clean, monkeypatch
):
    """The frame mounts at content_block_start; the later tool_result no-ops."""
    client = _FakeMCPClient(_html_result("<main>app</main>"))
    _seed(monkeypatch, client)
    emitted: set = set()

    mount = await coord._emit_ui_resource_for_tool(
        "widget", "tu-1", emitted
    )
    assert len(mount) == 1
    assert _parse(mount[0])["toolUseId"] == "tu-1"
    assert emitted == {"tu-1"}

    # The post-tool_result fallback path must not emit a second frame.
    again = await coord._extract_ui_resource_events(
        _tool_result_event("tu-1"), {"tu-1": "widget"}, emitted
    )
    assert again == []
    assert client.read_calls == ["ui://srv/widget"]  # only fetched once


@pytest.mark.asyncio
async def test_early_mount_noop_for_non_ui_tool(coord, catalog_clean, monkeypatch):
    monkeypatch.setenv(_ENV_FLAG, "true")
    out = await coord._emit_ui_resource_for_tool("plain_tool", "tu-1", set())
    assert out == []


@pytest.mark.asyncio
async def test_early_mount_inert_when_flag_disabled(
    coord, catalog_clean, monkeypatch
):
    _seed(monkeypatch, _FakeMCPClient(_html_result()))
    monkeypatch.setenv(_ENV_FLAG, "false")
    out = await coord._emit_ui_resource_for_tool("widget", "tu-1", set())
    assert out == []


@pytest.mark.asyncio
async def test_header_shell_emits_instantly_without_read(
    coord, catalog_clean, monkeypatch
):
    """The header-only shell ships an empty-html `ui_resource` with NO
    resources/read, so the App frame's header replaces the tool rail at once;
    its own dedupe set is independent of the full-emit set."""
    client = _FakeMCPClient(_html_result("<main>app</main>"))
    _seed(monkeypatch, client)
    header_emitted: set = set()

    header = coord._emit_ui_app_header_for_tool(
        "widget", "tu-1", header_emitted
    )
    assert len(header) == 1
    payload = _parse(header[0])
    assert payload["toolUseId"] == "tu-1"
    assert payload["html"] == ""  # shell — iframe waits for the full emit
    assert payload["resourceUri"] == "ui://srv/widget"
    assert client.read_calls == []  # the whole point: no slow read
    assert header_emitted == {"tu-1"}

    # Deduped on its own set...
    assert coord._emit_ui_app_header_for_tool("widget", "tu-1", header_emitted) == []

    # ...but the full html-bearing emit (separate set) is NOT blocked by it.
    full = await coord._emit_ui_resource_for_tool("widget", "tu-1", set())
    assert len(full) == 1
    assert _parse(full[0])["html"] == "<main>app</main>"


def test_header_shell_inert_when_flag_disabled(
    coord, catalog_clean, monkeypatch
):
    _seed(monkeypatch, _FakeMCPClient(_html_result()))
    monkeypatch.setenv(_ENV_FLAG, "false")
    assert coord._emit_ui_app_header_for_tool("widget", "tu-1", set()) == []


def test_header_shell_noop_for_non_ui_tool(coord, catalog_clean, monkeypatch):
    monkeypatch.setenv(_ENV_FLAG, "true")
    assert coord._emit_ui_app_header_for_tool("plain_tool", "tu-1", set()) == []


def test_partial_input_emits_healed_arguments(coord):
    # An incomplete streamed prefix heals to a valid object and ships as the
    # tool-input-partial payload.
    out = coord._emit_tool_input_partial(
        "tu-1", '{"elements": [{"type": "rectangle"}, {"type": "came'
    )
    assert len(out) == 1
    payload = _parse_partial(out[0])
    assert payload["type"] == "ui_tool_input_partial"
    assert payload["toolUseId"] == "tu-1"
    assert isinstance(payload["arguments"], dict)
    assert payload["arguments"]["elements"][0]["type"] == "rectangle"


def test_partial_input_skips_until_object_heals(coord):
    # Empty / whitespace → nothing to emit.
    assert coord._emit_tool_input_partial("tu-1", "") == []
    assert coord._emit_tool_input_partial("tu-1", "   ") == []
    # A prefix that only heals to an empty object carries nothing useful yet,
    # so we wait for more fragments rather than emitting a bare `{}`.
    assert coord._emit_tool_input_partial("tu-1", '{"ele') == []


def test_partial_input_failure_is_swallowed(coord, monkeypatch):
    import agents.main_agent.streaming.stream_coordinator as sc

    # Even if healing blows up, the stream is never broken.
    monkeypatch.setattr(
        "apis.shared.mcp_apps.partial_json.heal_partial_json",
        lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert coord._emit_tool_input_partial("tu-1", '{"a":1}') == []
