"""Tests for MCP Apps host support (PR #2 of the host-renderer initiative).

Covers the PR #2 acceptance criteria from
`docs/kaizen/scoping/mcp-apps-host-renderer.md`:

  (a) `io.modelcontextprotocol/ui` is advertised on the outbound MCP
      `initialize` when the host flag is on, and absent when it is off.
  (b) A tool whose `_meta.ui.visibility` excludes `"model"` is filtered out
      of the Strands tool list (external client + gateway filtered client).
  (c) `_meta.ui.resourceUri` survives the round-trip into our tool catalog.
  (d) Ordinary tools and default-visibility (`["model", "app"]`) tools are
      unaffected.

The fake-MCP-server surface is a `super().list_tools_sync()` stub returning
UI-bearing tools, mirroring the mock-the-boundary style already used in
`test_external_mcp_client.py`.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import mcp.types as mcp_types
import pytest
import strands.tools.mcp.mcp_client as strands_mcp_client_mod
from mcp.shared.session import BaseSession
from strands.types import PaginatedList

from agents.main_agent.integrations import mcp_apps
from agents.main_agent.integrations.mcp_apps import (
    MCP_APPS_UI_EXTENSION_KEY,
    MCP_APPS_UI_MIME_TYPE,
    UICapableMCPClient,
    _UIExtensionClientSession,
    ensure_ui_extension_session_patch,
    fetch_ui_resource,
    get_ui_tool_catalog,
    record_and_filter_ui_tools,
)
from agents.main_agent.integrations.gateway_mcp_client import FilteredMCPClient
from apis.shared.tools.models import DEFAULT_TOOL_VISIBILITY, ToolUIMetadata

_ENV_FLAG = "AGENTCORE_MCP_APPS_HOST_ENABLED"


@pytest.fixture
def mcp_apps_clean(monkeypatch):
    """Isolate the global catalog and the strands ClientSession symbol."""
    get_ui_tool_catalog().clear()
    original_session = strands_mcp_client_mod.ClientSession
    monkeypatch.delenv(_ENV_FLAG, raising=False)
    try:
        yield
    finally:
        strands_mcp_client_mod.ClientSession = original_session
        get_ui_tool_catalog().clear()


def _fake_tool(tool_name, ui=None, mcp_name=None):
    """An MCPAgentTool stand-in: it carries the raw mcp tool with `_meta`."""
    meta = {"ui": ui} if ui is not None else None
    return SimpleNamespace(
        tool_name=tool_name,
        mcp_tool=SimpleNamespace(name=mcp_name or tool_name, meta=meta),
    )


# ── ToolUIMetadata.from_meta ──────────────────────────────────────────────────


class TestToolUIMetadataParsing:
    def test_returns_none_for_non_ui_tool(self):
        assert ToolUIMetadata.from_meta(None) is None
        assert ToolUIMetadata.from_meta({}) is None
        assert ToolUIMetadata.from_meta({"other": 1}) is None

    def test_absent_visibility_defaults_to_spec_default(self):
        ui = ToolUIMetadata.from_meta({"ui": {"resourceUri": "ui://x/y"}})
        assert ui is not None
        assert ui.resource_uri == "ui://x/y"
        assert ui.visibility == DEFAULT_TOOL_VISIBILITY
        assert ui.visible_to_model() is True

    def test_app_only_visibility_hides_from_model(self):
        ui = ToolUIMetadata.from_meta(
            {"ui": {"resourceUri": "ui://x/y", "visibility": ["app"]}}
        )
        assert ui.visibility == ["app"]
        assert ui.visible_to_model() is False

    def test_raw_payload_is_retained_verbatim(self):
        raw = {
            "resourceUri": "ui://x/y",
            "visibility": ["model", "app"],
            "csp": {"connectDomains": ["https://example.com"]},
        }
        ui = ToolUIMetadata.from_meta({"ui": raw})
        assert ui.raw == raw


# ── (a) initialize advertises the UI extension ────────────────────────────────


async def _run_initialize(monkeypatch, *, enabled):
    """Drive _UIExtensionClientSession.initialize() with I/O stubbed out and
    return the ClientCapabilities that went onto the wire."""
    if enabled:
        monkeypatch.setenv(_ENV_FLAG, "true")
    else:
        monkeypatch.setenv(_ENV_FLAG, "false")

    captured: dict = {}

    async def fake_send_request(request, result_type, *a, **k):
        captured["request"] = request
        return mcp_types.InitializeResult(
            protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="fake-server", version="1"),
        )

    send_a, recv_a = anyio.create_memory_object_stream(1)
    send_b, recv_b = anyio.create_memory_object_stream(1)
    session = _UIExtensionClientSession(recv_a, send_b)

    with patch.object(
        BaseSession, "send_request", new=AsyncMock(side_effect=fake_send_request)
    ), patch.object(BaseSession, "send_notification", new=AsyncMock()):
        await session.initialize()

    request = captured["request"]
    caps = request.root.params.capabilities
    return caps.model_dump(by_alias=True, exclude_none=True)


@pytest.mark.asyncio
async def test_initialize_advertises_ui_extension_when_enabled(
    mcp_apps_clean, monkeypatch
):
    caps = await _run_initialize(monkeypatch, enabled=True)

    assert caps.get("extensions", {}).get(MCP_APPS_UI_EXTENSION_KEY) == {
        "mimeTypes": [MCP_APPS_UI_MIME_TYPE]
    }
    assert MCP_APPS_UI_MIME_TYPE == "text/html;profile=mcp-app"


@pytest.mark.asyncio
async def test_initialize_omits_ui_extension_when_disabled(
    mcp_apps_clean, monkeypatch
):
    caps = await _run_initialize(monkeypatch, enabled=False)

    assert MCP_APPS_UI_EXTENSION_KEY not in caps.get("extensions", {})


# ── ClientSession symbol patch ────────────────────────────────────────────────


class TestSessionPatch:
    def test_ensure_patch_substitutes_strands_client_session(self, mcp_apps_clean):
        ensure_ui_extension_session_patch()
        assert (
            strands_mcp_client_mod.ClientSession is _UIExtensionClientSession
        )

    def test_constructing_ui_capable_client_installs_patch(self, mcp_apps_clean):
        strands_mcp_client_mod.ClientSession = (
            mcp_apps._UIExtensionClientSession.__bases__[0]
        )
        UICapableMCPClient(lambda: None)
        assert (
            strands_mcp_client_mod.ClientSession is _UIExtensionClientSession
        )


# ── (b)(c)(d) record + visibility filter ─────────────────────────────────────


class TestRecordAndFilter:
    def test_passthrough_when_flag_disabled(self, mcp_apps_clean, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "false")
        tools = [
            _fake_tool("app_only", ui={"resourceUri": "ui://a", "visibility": ["app"]}),
            _fake_tool("plain"),
        ]

        result = record_and_filter_ui_tools(tools)

        # Inert: nothing filtered, nothing recorded.
        assert result == tools
        assert get_ui_tool_catalog().snapshot() == {}

    def test_filters_app_only_and_records_metadata(
        self, mcp_apps_clean, monkeypatch
    ):
        monkeypatch.setenv(_ENV_FLAG, "true")
        tools = [
            _fake_tool(
                "app_widget",
                ui={"resourceUri": "ui://app/widget", "visibility": ["app"]},
            ),
            _fake_tool(
                "panel",
                ui={"resourceUri": "ui://app/panel"},  # default visibility
            ),
            _fake_tool(
                "dual",
                ui={"resourceUri": "ui://app/dual", "visibility": ["model", "app"]},
            ),
            _fake_tool("plain"),  # ordinary, no _meta.ui
        ]

        result = record_and_filter_ui_tools(tools)

        kept = {t.tool_name for t in result}
        # (b) app-only hidden from the model; (d) the rest unaffected.
        assert kept == {"panel", "dual", "plain"}

        catalog = get_ui_tool_catalog()
        # (c) resourceUri survives the round-trip into our tool catalog,
        # including for the app-only tool we hide from the model.
        assert catalog.get("app_widget").resource_uri == "ui://app/widget"
        assert catalog.get("app_widget").visible_to_model() is False
        assert catalog.get("panel").resource_uri == "ui://app/panel"
        assert catalog.get("panel").visibility == DEFAULT_TOOL_VISIBILITY
        assert catalog.get("dual").resource_uri == "ui://app/dual"
        # Ordinary tools are never recorded.
        assert catalog.get("plain") is None


# ── external client: UICapableMCPClient.list_tools_sync ───────────────────────


class TestUICapableMCPClientListTools:
    @pytest.mark.asyncio
    async def test_list_tools_sync_filters_and_preserves_pagination(
        self, mcp_apps_clean, monkeypatch
    ):
        monkeypatch.setenv(_ENV_FLAG, "true")
        client = UICapableMCPClient(lambda: None)

        fake_page = PaginatedList(
            [
                _fake_tool(
                    "app_only",
                    ui={"resourceUri": "ui://srv/app", "visibility": ["app"]},
                ),
                _fake_tool("normal"),
            ],
            token="next-page",
        )

        with patch.object(
            strands_mcp_client_mod.MCPClient,
            "list_tools_sync",
            return_value=fake_page,
        ):
            result = client.list_tools_sync()

        assert [t.tool_name for t in result] == ["normal"]
        assert result.pagination_token == "next-page"
        assert (
            get_ui_tool_catalog().get("app_only").resource_uri == "ui://srv/app"
        )


# ── gateway client: FilteredMCPClient applies the same filter ─────────────────


class TestFilteredGatewayClientUIFilter:
    @pytest.mark.asyncio
    async def test_gateway_filtered_client_hides_app_only_tool(
        self, mcp_apps_clean, monkeypatch
    ):
        monkeypatch.setenv(_ENV_FLAG, "true")
        client = FilteredMCPClient(
            lambda: None,
            enabled_tool_ids=["app_only", "normal"],
        )

        fake_page = PaginatedList(
            [
                _fake_tool(
                    "app_only",
                    ui={"resourceUri": "ui://gw/app", "visibility": ["app"]},
                ),
                _fake_tool("normal"),
            ],
            token=None,
        )

        # Patch the grandparent MCPClient.list_tools_sync so FilteredMCPClient's
        # own override runs (enabled-id filter -> UI visibility filter).
        with patch.object(
            strands_mcp_client_mod.MCPClient,
            "list_tools_sync",
            return_value=fake_page,
        ):
            result = client.list_tools_sync()

        assert [t.tool_name for t in result] == ["normal"]
        assert get_ui_tool_catalog().get("app_only").resource_uri == "ui://gw/app"


# ── PR #3: resources/read fetch path + ui_resource payload ───────────────────


class _FakeMCPClient:
    """Stand-in for a Strands MCPClient at the `resources/read` boundary.

    Mirrors the mock-the-boundary style in `test_external_mcp_client.py`:
    the unit under test never starts a real session — it only calls
    `read_resource_sync`, which we record and stub.
    """

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.read_calls: list = []

    def read_resource_sync(self, uri):
        self.read_calls.append(uri)
        if self._raises is not None:
            raise self._raises
        return self._result


def _html_resource(
    *, text="<h1>widget</h1>", mime=MCP_APPS_UI_MIME_TYPE, ui_meta=None
):
    """A real `mcp.types.ReadResourceResult` — proves our extraction works
    against the actual MCP SDK shape, not just a duck-typed fake."""
    kwargs = {"uri": "ui://srv/widget", "mimeType": mime, "text": text}
    if ui_meta is not None:
        kwargs["_meta"] = {MCP_APPS_UI_EXTENSION_KEY: ui_meta}
    return mcp_types.ReadResourceResult(
        contents=[mcp_types.TextResourceContents(**kwargs)]
    )


def _seed_catalog(monkeypatch, *, ui, client):
    """Record a UI tool + its hosting client exactly the way a live
    `list_tools_sync` would (so the client-passing path is exercised too)."""
    monkeypatch.setenv(_ENV_FLAG, "true")
    record_and_filter_ui_tools([_fake_tool("widget", ui=ui)], client=client)


class TestFetchUIResource:
    def test_fetches_via_resources_read_and_inlines_html(
        self, mcp_apps_clean, monkeypatch
    ):
        client = _FakeMCPClient(
            result=_html_resource(
                ui_meta={
                    "csp": {"connectDomains": ["https://api.test"]},
                    "permissions": ["clipboard-write"],
                }
            )
        )
        _seed_catalog(
            monkeypatch,
            ui={"resourceUri": "ui://srv/widget"},
            client=client,
        )

        payload = fetch_ui_resource("widget", "tu-1")

        # Spec MUST: the resource is fetched via resources/read against the
        # hosting client, addressed by the `ui://` URI — never inlined by us.
        assert client.read_calls == ["ui://srv/widget"]
        assert payload == {
            "type": "ui_resource",
            "toolUseId": "tu-1",
            "resourceUri": "ui://srv/widget",
            "html": "<h1>widget</h1>",
            "mimeType": MCP_APPS_UI_MIME_TYPE,
            "csp": {"connectDomains": ["https://api.test"]},
            "permissions": ["clipboard-write"],
        }

    def test_inert_when_flag_disabled(self, mcp_apps_clean, monkeypatch):
        client = _FakeMCPClient(result=_html_resource())
        _seed_catalog(
            monkeypatch, ui={"resourceUri": "ui://srv/widget"}, client=client
        )
        # Flag flipped off *after* catalog seeding: the fetch path itself
        # must stay inert regardless of catalog contents.
        monkeypatch.setenv(_ENV_FLAG, "false")

        assert fetch_ui_resource("widget", "tu-1") is None
        assert client.read_calls == []

    def test_none_for_unknown_or_non_ui_tool(self, mcp_apps_clean, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "true")
        assert fetch_ui_resource("never-seen", "tu-1") is None

    def test_none_when_no_hosting_client_recorded(
        self, mcp_apps_clean, monkeypatch
    ):
        # Metadata recorded without a client (e.g. PR #2's catalog-only
        # path) → we cannot issue resources/read, so no event.
        _seed_catalog(
            monkeypatch, ui={"resourceUri": "ui://srv/widget"}, client=None
        )
        assert fetch_ui_resource("widget", "tu-1") is None

    def test_resources_read_failure_is_swallowed(
        self, mcp_apps_clean, monkeypatch
    ):
        client = _FakeMCPClient(raises=RuntimeError("session not running"))
        _seed_catalog(
            monkeypatch, ui={"resourceUri": "ui://srv/widget"}, client=client
        )
        assert fetch_ui_resource("widget", "tu-1") is None
        assert client.read_calls == ["ui://srv/widget"]

    def test_none_when_resource_has_no_inline_html(
        self, mcp_apps_clean, monkeypatch
    ):
        blob = mcp_types.ReadResourceResult(
            contents=[
                mcp_types.BlobResourceContents(
                    uri="ui://srv/widget",
                    mimeType="application/octet-stream",
                    blob="AAAA",
                )
            ]
        )
        client = _FakeMCPClient(result=blob)
        _seed_catalog(
            monkeypatch, ui={"resourceUri": "ui://srv/widget"}, client=client
        )
        assert fetch_ui_resource("widget", "tu-1") is None

    def test_csp_permissions_fall_back_to_tool_meta(
        self, mcp_apps_clean, monkeypatch
    ):
        # Resource carries no `_meta.ui`; the tool's `tools/list` `_meta.ui`
        # (retained verbatim by PR #2 in ToolUIMetadata.raw) supplies them.
        client = _FakeMCPClient(result=_html_resource(ui_meta=None))
        _seed_catalog(
            monkeypatch,
            ui={
                "resourceUri": "ui://srv/widget",
                "csp": {"frameDomains": ["https://embed.test"]},
                "permissions": ["geolocation"],
            },
            client=client,
        )

        payload = fetch_ui_resource("widget", "tu-9")
        assert payload is not None
        assert payload["csp"] == {"frameDomains": ["https://embed.test"]}
        assert payload["permissions"] == ["geolocation"]

    def test_prefers_mcp_app_mime_when_multiple_text_contents(
        self, mcp_apps_clean, monkeypatch
    ):
        result = mcp_types.ReadResourceResult(
            contents=[
                mcp_types.TextResourceContents(
                    uri="ui://srv/widget",
                    mimeType="text/plain",
                    text="ignored",
                ),
                mcp_types.TextResourceContents(
                    uri="ui://srv/widget",
                    mimeType=MCP_APPS_UI_MIME_TYPE,
                    text="<main>chosen</main>",
                ),
            ]
        )
        client = _FakeMCPClient(result=result)
        _seed_catalog(
            monkeypatch, ui={"resourceUri": "ui://srv/widget"}, client=client
        )

        payload = fetch_ui_resource("widget", "tu-2")
        assert payload["html"] == "<main>chosen</main>"
        assert payload["mimeType"] == MCP_APPS_UI_MIME_TYPE
