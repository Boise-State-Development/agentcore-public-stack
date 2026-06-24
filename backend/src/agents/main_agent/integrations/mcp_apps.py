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

The entire surface is gated by `AGENTCORE_MCP_APPS_HOST_ENABLED` (default
true since PR #7; set it to `false` to opt an environment back out). When
the flag is off, no extension is advertised and no tool is filtered or
recorded — behavior is byte-for-byte unchanged.

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

import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

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


def mcp_apps_sandbox_origin() -> str:
    """Origin of the sandbox-proxy the SPA frames an MCP App in.

    Read on every call (not cached), same as the host flag. Empty string
    until the PR #1 mcp-sandbox stack is deployed and the deploy pipeline
    wires its SSM origin into the env — benign because the whole surface is
    inert behind the host flag. Surfaced to the SPA on the `ui_resource`
    event so the frontend needs no separate config fetch.
    """
    return os.environ.get(
        EnvVars.MCP_APPS_SANDBOX_ORIGIN, Defaults.MCP_APPS_SANDBOX_ORIGIN
    ).strip()


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

        # Pre-warm the served-manifest icon for this server (best-effort,
        # cached per origin) so the request-path identity resolver stays a
        # cache lookup. Only external clients carry a usable `server_url`;
        # Gateway-fronted ones don't (their origin won't serve a manifest).
        server_url = getattr(client, "server_url", None)
        if server_url:
            resolve_server_icon(server_url)

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
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve `csp` / `permissions` for the `ui_resource` event.

    The spec declares these on the resource's `_meta.ui` (per-content first,
    then the result-level `_meta`). We fall back to the tool-level `_meta.ui`
    PR #2 retained verbatim in `ToolUIMetadata.raw` so a server that declares
    them only on `tools/list` still works. PR #3 passes them through opaquely;
    building the actual CSP (deny-by-default) is the frontend's job (PR #4).

    Both are objects per SEP-1865: `csp` is `McpUiResourceCsp`
    (`{connectDomains, resourceDomains, frameDomains, baseUriDomains}`) and
    `permissions` is `{camera?, microphone?, geolocation?, clipboardWrite?}`
    (each an empty object when requested) — NOT a list. The sandbox proxy
    maps the permission keys onto the inner iframe's `allow` attribute.
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
    permissions: Dict[str, Any] = {}
    for block in sources:
        if not csp and isinstance(block.get("csp"), dict):
            csp = block["csp"]
        if not permissions and isinstance(block.get("permissions"), dict):
            permissions = block["permissions"]
    return csp, permissions


def _server_name_from_uri(resource_uri: str) -> str:
    """Title-case the `ui://<authority>/…` authority as a server-name fallback.

    `ui://excalidraw/canvas` -> "Excalidraw". Used only when the server did
    not advertise a `serverInfo.title`/`name` — a host-side approximation that
    needs no server cooperation (matches the label Claude shows). Returns "" if
    no authority can be parsed.
    """
    try:
        authority = urlsplit(resource_uri).netloc or ""
    except Exception:
        return ""
    # Split on common separators so "my-cool-server" reads as "My Cool Server".
    words = [w for w in re.split(r"[-_.\s]+", authority) if w]
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _pick_icon(icons: Any) -> str:
    """Pick a usable icon `src` from a `serverInfo.icons` / tool `icons` list.

    Each entry is an MCP `Icon` (`{src, mimeType, sizes}`) — a model or a dict.
    Returns the first non-empty `src` (an http(s) or `data:` URL the SPA header
    renders in an `<img>`, with a glyph fallback on error), or "" if none.
    """
    if not isinstance(icons, (list, tuple)):
        return ""
    for icon in icons:
        src = getattr(icon, "src", None)
        if src is None and isinstance(icon, dict):
            src = icon.get("src")
        if isinstance(src, str) and src.strip():
            return src.strip()
    return ""


# --- Served-manifest icon resolution (Claude-parity, automatic) -------------
# The MCP runtime protocol carries no icon (no `serverInfo.icons`/tool `icons`
# for e.g. Excalidraw). Claude shows the logo because it installs the server's
# MCPB *bundle*, which contains the icon file referenced by `manifest.json`
# (`"icon": "docs/logo.png"`), and inlines it as a `data:` URI. Many of these
# deployable servers ALSO serve that manifest + icon over HTTP at their origin
# (verified: `https://mcp.excalidraw.com/manifest.json` + `/docs/logo.png`), so
# we replicate it server-side: fetch the manifest, resolve its icon
# same-origin, and base64-inline it. Cached per origin (process lifetime), so
# it runs at most once per server. Entirely best-effort → "" → generic glyph.

_ICON_FETCH_TIMEOUT_S = 5
_MAX_ICON_BYTES = 256 * 1024  # decoded image size cap (excalidraw logo ~87KB)
_MAX_MANIFEST_BYTES = 64 * 1024
#: origin -> resolved icon `data:` URI, or "" (resolved-but-none / failed).
_server_icon_by_origin: Dict[str, str] = {}


def _url_origin(url: Any) -> str:
    """`scheme://netloc` for an http(s) URL, else "" (guards file:// etc.)."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _http_get(url: str, max_bytes: int) -> Tuple[bytes, str]:
    """GET with a timeout; return (body, content-type). Caps the body size and
    refuses non-http(s) schemes (the URL is an admin-trusted MCP origin)."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("refusing non-http(s) URL")
    req = Request(url, headers={"User-Agent": "agentcore-mcp-apps"})
    with urlopen(req, timeout=_ICON_FETCH_TIMEOUT_S) as resp:  # noqa: S310 - trusted MCP origin
        ctype = resp.headers.get("Content-Type", "") or ""
        body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("response exceeds size cap")
    return body, ctype


def _fetch_manifest_icon(origin: str) -> str:
    """Fetch `<origin>/manifest.json`, resolve its `icon` same-origin, and
    return a base64 `data:` URI (or "")."""
    body, _ = _http_get(f"{origin}/manifest.json", _MAX_MANIFEST_BYTES)
    manifest = json.loads(body.decode("utf-8"))
    icon_ref = manifest.get("icon") if isinstance(manifest, dict) else None
    if not isinstance(icon_ref, str) or not icon_ref.strip():
        return ""
    icon_ref = icon_ref.strip()
    if icon_ref.startswith("data:"):
        return icon_ref  # already inline
    icon_url = urljoin(origin + "/", icon_ref)
    # Same-origin only: never follow an absolute icon URL to a foreign host
    # (bounds the fetch to the already-trusted, admin-configured MCP origin).
    if _url_origin(icon_url) != origin:
        return ""
    img, ctype = _http_get(icon_url, _MAX_ICON_BYTES)
    mime = ctype.split(";")[0].strip()
    if not mime.startswith("image/"):
        return ""
    return f"data:{mime};base64,{base64.b64encode(img).decode('ascii')}"


def resolve_server_icon(server_url: Any) -> str:
    """Resolve + cache a server origin's served-manifest icon (best-effort).

    Pre-warmed from `record_and_filter_ui_tools` at `tools/list` so the
    request-path resolvers stay a cache lookup. Caches "" on miss/failure too,
    so a server that serves no manifest is probed at most once. Never raises.
    """
    if not is_mcp_apps_host_enabled():
        return ""
    origin = _url_origin(server_url)
    if not origin:
        return ""
    if origin in _server_icon_by_origin:
        return _server_icon_by_origin[origin]
    icon = ""
    try:
        icon = _fetch_manifest_icon(origin)
    except Exception:
        logger.debug(
            "MCP Apps: served-manifest icon resolution failed for %s",
            origin,
            exc_info=True,
        )
        icon = ""
    _server_icon_by_origin[origin] = icon
    return icon


def get_cached_server_icon(server_url: Any) -> str:
    """Cache-only lookup (no network) of a server origin's resolved icon."""
    return _server_icon_by_origin.get(_url_origin(server_url), "")


def _resolve_server_identity(
    client: Any, resource_uri: str
) -> Tuple[str, str]:
    """Resolve `(serverName, icon)` for a tool's `ui_resource` event.

    Name: server-advertised `serverInfo` (`title` > `name`, captured off
    `initialize`) → `ui://` authority. Icon: `serverInfo.icons` (spec) → the
    server's served-manifest icon (Claude parity, pre-warmed cache keyed by the
    client's `server_url`) → "" (frontend glyph). Wholly best-effort.
    """
    info = getattr(
        getattr(client, "_background_thread_session", None),
        "_mcp_apps_server_info",
        None,
    )
    name = getattr(info, "title", None) or getattr(info, "name", None) or ""
    icon = _pick_icon(getattr(info, "icons", None))
    if not icon:
        server_url = getattr(client, "server_url", None)
        if server_url:
            icon = get_cached_server_icon(server_url)
    if not name:
        name = _server_name_from_uri(resource_uri)
    return name, icon


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
    server_name, icon = _resolve_server_identity(client, ui_metadata.resource_uri)
    return {
        "type": "ui_resource",
        "toolUseId": tool_use_id,
        "resourceUri": ui_metadata.resource_uri,
        "html": html,
        "mimeType": mime_type or MCP_APPS_UI_MIME_TYPE,
        "csp": csp,
        "permissions": permissions,
        # Server identity for the App header (SEP-1865 Claude parity): the
        # server's display name + optional icon. `serverName` falls back to the
        # `ui://` authority; `icon` is "" when the server advertised none (the
        # frontend then renders a generic glyph).
        "serverName": server_name,
        "icon": icon,
        # Agent-facing tool name, carried on the event so the App frame's header
        # shows it (with the running shimmer) the instant the frame promotes —
        # independent of when the streamed message content lands.
        "toolName": tool_name,
        # Origin the SPA frames the sandbox-proxy at (PR #1's proxy.html).
        # Carried on the event so the frontend needs no separate config
        # fetch. Empty until the mcp-sandbox stack is deployed + wired.
        "sandboxOrigin": mcp_apps_sandbox_origin(),
    }


def build_ui_app_header(
    tool_name: str, tool_use_id: str
) -> Optional[Dict[str, Any]]:
    """Build a metadata-only `ui_resource` (empty `html`) WITHOUT `resources/read`.

    The full `ui_resource` requires a `resources/read` of the App HTML, which
    can be large/slow; that latency is the window where a UI tool would briefly
    show in the plain tool rail before its frame mounts. This builds the same
    event shape with everything that's known *instantly* at the tool's
    `content_block_start` — `resourceUri` + `serverName` + `icon` (from the
    catalog + captured `serverInfo`) + tool-level `csp`/`permissions` — and an
    EMPTY `html`. Emitting it first lets the host promote the App frame's
    HEADER (icon + server + tool + shimmer) immediately; the full
    `fetch_ui_resource` payload follows and, last-write-wins on the frontend,
    fills the iframe (which stays unmounted until `html` is non-empty).

    Returns None on flag-off or a non-UI tool — same inert contract as
    `fetch_ui_resource`. csp/permissions come only from the tool's `tools/list`
    `_meta.ui` here (the resource-level overrides arrive with the full fetch);
    they're advisory until the iframe mounts on the full event anyway.
    """
    if not is_mcp_apps_host_enabled():
        return None
    catalog = get_ui_tool_catalog()
    ui_metadata = catalog.get(tool_name)
    if ui_metadata is None or not ui_metadata.resource_uri:
        return None

    raw = ui_metadata.raw or {}
    csp = raw["csp"] if isinstance(raw.get("csp"), dict) else {}
    permissions = (
        raw["permissions"] if isinstance(raw.get("permissions"), dict) else {}
    )
    server_name, icon = _resolve_server_identity(
        catalog.get_client(tool_name), ui_metadata.resource_uri
    )
    return {
        "type": "ui_resource",
        "toolUseId": tool_use_id,
        "resourceUri": ui_metadata.resource_uri,
        # Empty until the full fetch lands; the frontend gates the iframe mount
        # on a non-empty html, so this shell only drives the header.
        "html": "",
        "mimeType": MCP_APPS_UI_MIME_TYPE,
        "csp": csp,
        "permissions": permissions,
        "serverName": server_name,
        "icon": icon,
        "toolName": tool_name,
        "sandboxOrigin": mcp_apps_sandbox_origin(),
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

    #: `serverInfo` (`Implementation`) captured off this session's
    #: `initialize` result — the App header's source of truth for the server's
    #: display name + icon (SEP-1865 header parity with Claude). Neither the
    #: MCP SDK `ClientSession` nor Strands' `MCPClient` retains it (Strands
    #: keeps only `instructions`), so we stash it here and `fetch_ui_resource`
    #: reads it back via the client's `_background_thread_session`. None until
    #: `initialize` returns.
    _mcp_apps_server_info: Optional[Any] = None

    async def send_request(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        is_initialize = isinstance(
            getattr(request, "root", None), mcp_types.InitializeRequest
        )
        if is_mcp_apps_host_enabled() and is_initialize:
            try:
                root = request.root
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

        result = await super().send_request(request, *args, **kwargs)

        # Capture the server's `Implementation` (name/title/icons) off the
        # `initialize` response so the App header can show the server's
        # identity. Best-effort: a missing/odd `serverInfo` just leaves the
        # header to fall back to the `ui://` authority + a generic glyph.
        if is_initialize:
            self._mcp_apps_server_info = getattr(result, "serverInfo", None)

        return result


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

# Transport-level error type names worth retrying on a fresh connection — a TLS
# handshake blip (`SSLV3_ALERT_HANDSHAKE_FAILURE` from a middlebox), a reset, or
# a connect timeout. Matched by class name so we don't hard-import
# httpx/httpcore/ssl just to classify an exception.
_TRANSIENT_CONNECT_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectionResetError",
        "ReadTimeout",
        "PoolTimeout",
        "SSLError",
        "SSLEOFError",
    }
)

#: MCP client start() retry policy (transient transport failures only).
_MCP_START_MAX_ATTEMPTS = 3
_MCP_START_BACKOFF_BASE_S = 0.5


def _is_transient_connect_error(exc: BaseException) -> bool:
    """True if `exc` — or anything in its cause/context/ExceptionGroup chain —
    is a transport-level connection error worth retrying on a new connection.

    Strands wraps the real failure as `MCPClientInitializationError` whose
    `__cause__` is an anyio `ExceptionGroup` containing the `httpx.ConnectError`
    (often itself caused by an `ssl.SSLError`), so we walk the whole tree.
    """
    seen: set[int] = set()
    stack: List[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if type(e).__name__ in _TRANSIENT_CONNECT_ERROR_NAMES:
            return True
        for nxt in (e.__cause__, e.__context__):
            if nxt is not None:
                stack.append(nxt)
        stack.extend(getattr(e, "exceptions", ()) or ())  # ExceptionGroup
    return False


class UICapableMCPClient(MCPClient):
    """`MCPClient` that records `_meta.ui` and hides app-only tools.

    Used for external MCP servers. Construction installs the `initialize`
    extension patch so this client's session advertises the UI capability.
    `list_tools_sync` is the seam Strands calls to build the model's tool
    list, so filtering here guarantees the model never sees app-only tools
    while the full metadata is retained in the catalog.

    `server_url` is the configured MCP endpoint (`MCPServerConfig.server_url`);
    retained so the App-header icon resolver can derive the server origin and
    fetch its served bundle manifest's icon. Strands' `MCPClient` only gets a
    transport callable, so it can't expose the URL itself.
    """

    def __init__(
        self, *args: Any, server_url: Optional[str] = None, **kwargs: Any
    ) -> None:
        self.server_url = server_url
        ensure_ui_extension_session_patch()
        super().__init__(*args, **kwargs)

    def start(self, *args: Any, **kwargs: Any) -> "UICapableMCPClient":
        """Start the MCP session, retrying transient transport failures.

        A single TLS handshake blip or connection reset at startup otherwise
        fails the whole agent build: Strands' `start()` raises
        `MCPClientInitializationError`, the tool fails to load, and agent
        creation errors out for the user. External MCP endpoints — Lambda
        Function URLs, third-party servers behind TLS-inspecting middleboxes —
        hit these intermittently, so retry a few times with exponential backoff
        before giving up. Non-transient failures (bad URL, auth, protocol
        mismatch) are re-raised on the first attempt. Strands resets its init
        future + background thread on failure (via `stop()`), so re-invoking
        `start()` is safe.
        """
        last_exc: Optional[BaseException] = None
        delay = _MCP_START_BACKOFF_BASE_S
        for attempt in range(1, _MCP_START_MAX_ATTEMPTS + 1):
            try:
                super().start(*args, **kwargs)
                return self
            except Exception as exc:  # noqa: BLE001 - classify, then retry/raise
                last_exc = exc
                if attempt >= _MCP_START_MAX_ATTEMPTS or not _is_transient_connect_error(
                    exc
                ):
                    raise
                logger.warning(
                    "MCP client start failed (attempt %d/%d) with a transient "
                    "transport error; retrying in %.1fs: %s",
                    attempt,
                    _MCP_START_MAX_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)
                delay *= 2
        # Unreachable: the loop returns on success or raises on the last attempt.
        assert last_exc is not None
        raise last_exc

    def list_tools_sync(self, *args: Any, **kwargs: Any) -> PaginatedList:
        result = super().list_tools_sync(*args, **kwargs)
        filtered = record_and_filter_ui_tools(list(result), client=self)
        # Drop tools folded behind a skill's meta-tools (PR-6b). No-op unless a
        # SkillAgent registered a fold set for this client; imported lazily to
        # avoid a module import cycle (mcp_tool_folding is integration-neutral).
        from agents.main_agent.integrations.mcp_tool_folding import (
            drop_folded_tools,
        )

        filtered = drop_folded_tools(self, filtered)
        return PaginatedList(filtered, token=result.pagination_token)
