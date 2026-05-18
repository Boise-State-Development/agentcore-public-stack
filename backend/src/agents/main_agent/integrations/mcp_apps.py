"""MCP Apps host support — `initialize` extension advertisement + tool-visibility filter.

PR #2 of the MCP Apps host-renderer initiative
(`docs/kaizen/scoping/mcp-apps-host-renderer.md`). This module is the backend
surface for the MCP Apps extension (SEP-1865):

1. Advertise `capabilities.extensions["io.modelcontextprotocol/ui"]` on every
   outbound MCP `initialize` (Gateway + external clients), so servers know we
   can host their UIs. Unconditional per-server (servers that don't understand
   the capability ignore it).
2. Parse `_meta.ui` off `tools/list` responses and retain it in an in-process
   catalog (`UIToolCatalog`) keyed by agent-facing tool name. Later PRs read
   `resource_uri` from here to fetch the UI via `resources/read`.
3. Filter tools whose `_meta.ui.visibility` excludes `"model"` out of the
   Strands agent's tool list — the model must never see app-only tools — while
   the full metadata stays in the catalog.

The entire surface is inert unless `AGENTCORE_MCP_APPS_HOST_ENABLED=true`
(default false until PR #7 flips it on). When the flag is off, no extension is
advertised and no tool is filtered or recorded — behavior is byte-for-byte
unchanged.

Why a `ClientSession` symbol patch: Strands' `MCPClient` constructs the MCP
SDK `ClientSession` itself inside its background thread and exposes no hook to
customize the `initialize` capabilities. The SDK hard-codes
`ClientCapabilities(experimental=None, ...)` with no `extensions`. Subclassing
`ClientSession` and substituting the single symbol Strands resolves
(`strands.tools.mcp.mcp_client.ClientSession`) is the minimal, upgrade-robust
seam: it does not touch the SDK's own `ClientSession`, and the unit test that
asserts the capability appears on the wire fails loudly if a Strands upgrade
ever changes how the session is constructed.
"""

import logging
import os
from typing import Any, List, Optional

import mcp.types as mcp_types
from mcp.client.session import ClientSession
from strands.tools.mcp import MCPClient
from strands.types import PaginatedList

from agents.main_agent.config.constants import Defaults, EnvVars
from apis.shared.tools.models import ToolUIMetadata

logger = logging.getLogger(__name__)

# SEP-1865 wire constants.
MCP_APPS_UI_EXTENSION_KEY = "io.modelcontextprotocol/ui"
MCP_APPS_UI_MIME_TYPE = "text/html;profile=mcp-app"
MCP_APPS_UI_CAPABILITY: dict[str, Any] = {"mimeTypes": [MCP_APPS_UI_MIME_TYPE]}


def is_mcp_apps_host_enabled() -> bool:
    """True when the MCP Apps host surface is enabled via env flag.

    Read on every call (not cached) so the flag can be flipped without a
    process restart, matching the Gateway flag's pattern.
    """
    raw = os.environ.get(
        EnvVars.MCP_APPS_HOST_ENABLED, str(Defaults.MCP_APPS_HOST_ENABLED)
    )
    return raw.strip().lower() == "true"


# =============================================================================
# In-process UI tool catalog
# =============================================================================


class UIToolCatalog:
    """Process-global map of agent-facing tool name -> parsed `_meta.ui`.

    This is the "tool catalog for later PRs": PR #3 reads `resource_uri` from
    here to fetch the UI resource via `resources/read`. Kept in memory because
    `_meta.ui` is discovered live from the server on every `tools/list`, not
    admin-configured, and is re-derived each agent build.
    """

    def __init__(self) -> None:
        self._by_tool_name: dict[str, ToolUIMetadata] = {}

    def record(self, tool_name: str, ui_metadata: ToolUIMetadata) -> None:
        self._by_tool_name[tool_name] = ui_metadata

    def get(self, tool_name: str) -> Optional[ToolUIMetadata]:
        return self._by_tool_name.get(tool_name)

    def snapshot(self) -> dict[str, ToolUIMetadata]:
        return dict(self._by_tool_name)

    def clear(self) -> None:
        self._by_tool_name.clear()


_ui_tool_catalog: Optional[UIToolCatalog] = None


def get_ui_tool_catalog() -> UIToolCatalog:
    """Get or create the global UIToolCatalog instance."""
    global _ui_tool_catalog
    if _ui_tool_catalog is None:
        _ui_tool_catalog = UIToolCatalog()
    return _ui_tool_catalog


def record_and_filter_ui_tools(tools: List[Any]) -> List[Any]:
    """Record `_meta.ui` into the catalog and drop model-invisible tools.

    Given the `MCPAgentTool` list a Strands `MCPClient` produced from a
    `tools/list`, parse each tool's `_meta.ui`, store it in the catalog (keyed
    by the agent-facing tool name), and return only the tools the model is
    allowed to see. Tools with no `_meta.ui` are ordinary tools and pass
    through untouched.

    When the host flag is disabled this is a pure pass-through: nothing is
    recorded and nothing is filtered.
    """
    if not is_mcp_apps_host_enabled():
        return tools

    catalog = get_ui_tool_catalog()
    visible: List[Any] = []
    for tool in tools:
        mcp_tool = getattr(tool, "mcp_tool", None)
        meta = getattr(mcp_tool, "meta", None)
        ui_metadata = ToolUIMetadata.from_meta(meta)

        if ui_metadata is None:
            visible.append(tool)
            continue

        tool_name = getattr(tool, "tool_name", None) or getattr(
            mcp_tool, "name", "<unknown>"
        )
        catalog.record(tool_name, ui_metadata)

        if ui_metadata.visible_to_model():
            visible.append(tool)
        else:
            logger.debug(
                "filtered app-only MCP tool from model tool list: %s "
                "(visibility=%s)",
                tool_name,
                ui_metadata.visibility,
            )

    return visible


# =============================================================================
# initialize() extension advertisement
# =============================================================================


class _UIExtensionClientSession(ClientSession):
    """`ClientSession` that advertises the MCP Apps UI extension on `initialize`.

    Drop-in for the SDK `ClientSession` — identical constructor, identical
    behavior, except that the outbound `InitializeRequest` gets
    `capabilities.extensions["io.modelcontextprotocol/ui"]` added when the
    host flag is enabled. We augment in `send_request` rather than reimplement
    `initialize()` so we inherit whatever capabilities the SDK computes
    (sampling/elicitation/roots/tasks) and stay robust to SDK changes.
    """

    async def send_request(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        if is_mcp_apps_host_enabled():
            try:
                root = getattr(request, "root", None)
                if isinstance(root, mcp_types.InitializeRequest):
                    caps = root.params.capabilities
                    caps_data = caps.model_dump(by_alias=True, exclude_none=True)
                    extensions = dict(caps_data.get("extensions") or {})
                    extensions.setdefault(
                        MCP_APPS_UI_EXTENSION_KEY, dict(MCP_APPS_UI_CAPABILITY)
                    )
                    caps_data["extensions"] = extensions
                    # `ClientCapabilities` is `extra="allow"`, so the extra
                    # `extensions` key round-trips through model_dump and onto
                    # the JSON-RPC wire in BaseSession.send_request.
                    root.params.capabilities = mcp_types.ClientCapabilities(
                        **caps_data
                    )
            except Exception:
                # Advertising the extension must never break a connection;
                # a server that never sees it simply won't return MCP Apps.
                logger.warning(
                    "failed to advertise MCP Apps UI extension on initialize; "
                    "continuing without it",
                    exc_info=True,
                )

        return await super().send_request(request, *args, **kwargs)


def ensure_ui_extension_session_patch() -> None:
    """Idempotently make Strands' MCP client construct `_UIExtensionClientSession`.

    Substitutes the single `ClientSession` symbol that
    `strands.tools.mcp.mcp_client` resolves when it builds a session. The MCP
    SDK's own `mcp.ClientSession` is left untouched. Safe to leave installed
    permanently: the subclass only augments `initialize` when the host flag is
    on, so with the flag off it is behaviorally identical to the SDK class.
    """
    import strands.tools.mcp.mcp_client as strands_mcp_client_mod

    if strands_mcp_client_mod.ClientSession is _UIExtensionClientSession:
        return

    strands_mcp_client_mod.ClientSession = _UIExtensionClientSession
    logger.info(
        "MCP Apps: patched strands MCP client to advertise the "
        "'%s' extension on initialize",
        MCP_APPS_UI_EXTENSION_KEY,
    )


# =============================================================================
# UI-capable MCP client
# =============================================================================


class UICapableMCPClient(MCPClient):
    """`MCPClient` that records `_meta.ui` and hides app-only tools.

    Used for external MCP servers. Construction installs the `initialize`
    extension patch so this client's session advertises the UI capability.
    `list_tools_sync` is the seam Strands calls to build the model's tool
    list, so filtering here guarantees the model never sees app-only tools
    while the full metadata is retained in the catalog.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ensure_ui_extension_session_patch()
        super().__init__(*args, **kwargs)

    def list_tools_sync(self, *args: Any, **kwargs: Any) -> PaginatedList:
        result = super().list_tools_sync(*args, **kwargs)
        filtered = record_and_filter_ui_tools(list(result))
        return PaginatedList(filtered, token=result.pagination_token)
