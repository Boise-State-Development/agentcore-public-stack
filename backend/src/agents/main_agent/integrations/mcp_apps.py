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
from typing import Any, Dict, List, Optional, Tuple

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

    PR #3 also records the MCP client that surfaced each UI tool, alongside
    its metadata. `record_and_filter_ui_tools` is invoked from within a
    client's own `list_tools_sync`, so "the server hosting the tool" is just
    that client — `read_resource_sync` against it is the spec-mandated
    `resources/read`. The client's session stays alive for the agent's
    lifetime (Strands holds MCP clients as tool providers), so it is still
    active when a tool result arrives mid-stream.
    """

    def __init__(self) -> None:
        self._by_tool_name: dict[str, ToolUIMetadata] = {}
        self._client_by_tool_name: dict[str, Any] = {}

    def record(
        self,
        tool_name: str,
        ui_metadata: ToolUIMetadata,
        client: Optional[Any] = None,
    ) -> None:
        self._by_tool_name[tool_name] = ui_metadata
        if client is not None:
            self._client_by_tool_name[tool_name] = client

    def get(self, tool_name: str) -> Optional[ToolUIMetadata]:
        return self._by_tool_name.get(tool_name)

    def get_client(self, tool_name: str) -> Optional[Any]:
        """The MCP client that surfaced `tool_name`, or None.

        Used by `fetch_ui_resource` to issue `resources/read` against the
        same server the tool came from (spec MUST: never inline).
        """
        return self._client_by_tool_name.get(tool_name)

    def snapshot(self) -> dict[str, ToolUIMetadata]:
        return dict(self._by_tool_name)

    def clear(self) -> None:
        self._by_tool_name.clear()
        self._client_by_tool_name.clear()


_ui_tool_catalog: Optional[UIToolCatalog] = None


def get_ui_tool_catalog() -> UIToolCatalog:
    """Get or create the global UIToolCatalog instance."""
    global _ui_tool_catalog
    if _ui_tool_catalog is None:
        _ui_tool_catalog = UIToolCatalog()
    return _ui_tool_catalog


def record_and_filter_ui_tools(
    tools: List[Any], client: Optional[Any] = None
) -> List[Any]:
    """Record `_meta.ui` into the catalog and drop model-invisible tools.

    Given the `MCPAgentTool` list a Strands `MCPClient` produced from a
    `tools/list`, parse each tool's `_meta.ui`, store it in the catalog (keyed
    by the agent-facing tool name), and return only the tools the model is
    allowed to see. Tools with no `_meta.ui` are ordinary tools and pass
    through untouched.

    `client` is the MCP client whose `list_tools_sync` produced `tools`. It
    is recorded alongside each UI tool's metadata so PR #3 can issue
    `resources/read` against the same server the tool came from. It is
    optional purely so PR #2's catalog tests can call this without a client.

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
        catalog.record(tool_name, ui_metadata, client=client)

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
# resources/read fetch path (PR #3)
# =============================================================================

# Keys an MCP App resource may carry its `_meta.ui` block under. SEP-1865
# namespaces it as `io.modelcontextprotocol/ui`; PR #2 also accepts the short
# `ui` alias on tool `_meta`, so honor both on the resource side too.
_UI_META_KEYS = (MCP_APPS_UI_EXTENSION_KEY, "ui")


def _coerce_meta(meta: Any) -> Dict[str, Any]:
    """Best-effort `_meta` -> dict. Accepts a dict or a pydantic model."""
    if isinstance(meta, dict):
        return meta
    if meta is not None and hasattr(meta, "model_dump"):
        try:
            return meta.model_dump(by_alias=True, exclude_none=True)
        except Exception:
            return {}
    return {}


def _ui_block(meta: Any) -> Dict[str, Any]:
    """Extract the MCP Apps `ui` block from a `_meta` dict, or {}."""
    data = _coerce_meta(meta)
    for key in _UI_META_KEYS:
        block = data.get(key)
        if isinstance(block, dict):
            return block
    return {}


def _extract_html_content(result: Any) -> Tuple[Optional[str], str]:
    """Pick the HTML body + MIME type out of a `resources/read` result.

    Prefers the spec MIME type (`text/html;profile=mcp-app`), then any
    `text/html*` text content, then untyped text (the tool already declared
    a `ui://` resource, so an inline body with no MIME is treated as the
    app). An explicit non-HTML MIME (`text/plain`, `application/json`, …) is
    rejected — we never pass a non-app body off as the app. Returns
    `(None, "")` when nothing usable is present (e.g. a blob-only resource);
    the caller then emits nothing.
    """
    contents = getattr(result, "contents", None) or []
    html_fallback: Optional[Tuple[str, str]] = None
    untyped_fallback: Optional[Tuple[str, str]] = None

    for item in contents:
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        mime = getattr(item, "mimeType", None) or ""
        if mime == MCP_APPS_UI_MIME_TYPE:
            return text, mime
        if html_fallback is None and mime.startswith("text/html"):
            html_fallback = (text, mime)
        elif untyped_fallback is None and not mime:
            untyped_fallback = (text, MCP_APPS_UI_MIME_TYPE)

    chosen = html_fallback or untyped_fallback
    if chosen is None:
        return None, ""
    return chosen[0], chosen[1]


def _extract_csp_permissions(
    result: Any, ui_metadata: ToolUIMetadata
) -> Tuple[Dict[str, Any], List[Any]]:
    """Resolve `csp` / `permissions` for the `ui_resource` event.

    The spec declares these on the resource's `_meta.ui` (per-content first,
    then the result-level `_meta`). We fall back to the tool-level `_meta.ui`
    PR #2 retained verbatim in `ToolUIMetadata.raw` so a server that declares
    them only on `tools/list` still works. PR #3 passes them through opaquely;
    building the actual CSP (deny-by-default) is the frontend's job (PR #4).
    """
    sources: List[Dict[str, Any]] = []
    for item in getattr(result, "contents", None) or []:
        block = _ui_block(getattr(item, "meta", None))
        if block:
            sources.append(block)
    result_block = _ui_block(getattr(result, "meta", None))
    if result_block:
        sources.append(result_block)
    sources.append(ui_metadata.raw or {})

    csp: Dict[str, Any] = {}
    permissions: List[Any] = []
    for block in sources:
        if not csp and isinstance(block.get("csp"), dict):
            csp = block["csp"]
        if not permissions and isinstance(block.get("permissions"), list):
            permissions = block["permissions"]
    return csp, permissions


def fetch_ui_resource(
    tool_name: str, tool_use_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch a tool's MCP App UI resource and build the `ui_resource` payload.

    Looks up `tool_name` in the catalog PR #2 populates; if it carries a
    `ui://` `resourceUri`, issues `resources/read` against the same MCP
    client that surfaced the tool (spec MUST: fetch via `resources/read`,
    never inline from the server's perspective) and returns the SSE payload
    `{type, toolUseId, resourceUri, html, mimeType, csp, permissions}` with
    the HTML inlined so the frontend needs no MCP client of its own.

    Best-effort and fully inert when `AGENTCORE_MCP_APPS_HOST_ENABLED` is
    false: returns None on flag-off, non-UI tool, unknown hosting client,
    inactive session, fetch error, or a body with no inline HTML. Never
    raises into the stream.
    """
    if not is_mcp_apps_host_enabled():
        return None

    catalog = get_ui_tool_catalog()
    ui_metadata = catalog.get(tool_name)
    if ui_metadata is None or not ui_metadata.resource_uri:
        return None

    client = catalog.get_client(tool_name)
    if client is None:
        logger.warning(
            "MCP Apps: tool %s has resourceUri %s but no hosting client "
            "was recorded; cannot issue resources/read",
            tool_name,
            ui_metadata.resource_uri,
        )
        return None

    try:
        result = client.read_resource_sync(ui_metadata.resource_uri)
    except Exception:
        logger.warning(
            "MCP Apps: resources/read failed for %s (%s); emitting no "
            "ui_resource event",
            tool_name,
            ui_metadata.resource_uri,
            exc_info=True,
        )
        return None

    html, mime_type = _extract_html_content(result)
    if html is None:
        logger.warning(
            "MCP Apps: resources/read for %s (%s) returned no inline HTML; "
            "emitting no ui_resource event",
            tool_name,
            ui_metadata.resource_uri,
        )
        return None

    csp, permissions = _extract_csp_permissions(result, ui_metadata)
    return {
        "type": "ui_resource",
        "toolUseId": tool_use_id,
        "resourceUri": ui_metadata.resource_uri,
        "html": html,
        "mimeType": mime_type or MCP_APPS_UI_MIME_TYPE,
        "csp": csp,
        "permissions": permissions,
    }


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
        filtered = record_and_filter_ui_tools(list(result), client=self)
        return PaginatedList(filtered, token=result.pagination_token)
