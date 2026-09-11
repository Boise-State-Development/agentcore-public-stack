"""Live MCP tool discovery for a *saved* catalog tool.

Drives per-tool selection in the admin skills picker and the user-facing model
settings: given a saved MCP/gateway catalog tool, return the individual tools it
exposes so a caller can enable a subset.

- external MCP (``mcp_external``): connect to the server and list its tools. For
  ``forward_auth_token`` servers, pass the caller's OIDC token so the live
  session authenticates as them. OAuth-provider (3LO) servers can't be
  discovered without an end-user consent token, so we fall back to any curated
  ``tools[]`` the admin recorded.
- gateway (``mcp``): the AgentCore Gateway enumerates a target's tools at
  registration, so return the curated ``tools[]`` (live gateway listing is not
  performed here).

NOTE: importing MCP-client construction from ``agents`` mirrors the existing
``/admin/tools/discover`` route. The import-boundary test permits
``app_api -> agents`` (it only forbids ``agents -> app_api``).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from apis.shared.tools.models import (
    MAX_CAPABILITY_ENTRIES,
    MAX_CAPABILITY_PAGES,
    DiscoveredMCPTool,
    MCPPromptEntry,
    MCPResourceEntry,
    ToolCapabilitySnapshot,
    ToolDefinition,
    _clip,
)

logger = logging.getLogger(__name__)


def _curated(tool: ToolDefinition) -> List[DiscoveredMCPTool]:
    """The tools an admin recorded on the catalog entry (may be empty)."""
    names = tool.curated_tool_names()
    if not names:
        return []
    cfg = tool.mcp_config or tool.mcp_gateway_config
    return [
        DiscoveredMCPTool(name=e.name, description=e.description)
        for e in (getattr(cfg, "tools", None) or [])
    ]


async def discover_tools_for_saved_tool(
    tool: ToolDefinition,
    oauth_token: Optional[str] = None,
) -> List[DiscoveredMCPTool]:
    """Return the individual tools a saved MCP/gateway catalog tool exposes.

    Args:
        tool: the saved catalog tool (must be protocol ``mcp`` or ``mcp_external``).
        oauth_token: the caller's OIDC token, forwarded to ``forward_auth_token``
            servers so discovery authenticates as the caller.

    Raises:
        RuntimeError: if a live external MCP server can't be reached. Routes
            translate this into a 502.
    """
    if tool.protocol == "mcp":
        # Gateway target — tools are enumerated by the gateway at registration.
        return _curated(tool)

    if tool.protocol != "mcp_external" or not tool.mcp_config:
        return []

    # OAuth (3LO) servers need an end-user consent token we don't hold here.
    if tool.requires_oauth_provider:
        return _curated(tool)

    from agents.main_agent.integrations.external_mcp_client import (
        create_external_mcp_client,
    )

    forward = bool(getattr(tool, "forward_auth_token", False))
    client = create_external_mcp_client(
        config=tool.mcp_config,
        tool_definition=tool,
        oauth_token=oauth_token if forward else None,
    )
    if client is None:
        return []

    def _list_tools():
        # MCPClient opens its session on context enter; list_tools_sync runs the
        # MCP tools/list call. Pushed to a thread so the event loop stays free.
        with client:
            return list(client.list_tools_sync())

    try:
        tools = await asyncio.to_thread(_list_tools)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 502 by the route
        logger.warning("Live MCP discovery failed for %s: %s", tool.tool_id, exc)
        raise RuntimeError(f"MCP server did not respond to tools/list: {exc}") from exc

    discovered: List[DiscoveredMCPTool] = []
    for t in tools:
        spec = getattr(t, "mcp_tool", None)
        name = getattr(spec, "name", None) or getattr(t, "tool_name", None)
        if not name:
            continue
        discovered.append(
            DiscoveredMCPTool(name=name, description=getattr(spec, "description", None))
        )
    return discovered


# =============================================================================
# Capability discovery (prompts + resources)
# =============================================================================


def _paginate(list_page, extract) -> tuple[list, bool]:
    """Walk an MCP listing's cursor, capped by entries and by pages.

    Returns ``(entries, truncated)``. Two caps, because they fail differently:
    a server with thousands of resources would blow the DynamoDB item, and a
    server with a broken cursor would loop forever.
    """
    entries: list = []
    cursor = None
    truncated = False
    for _ in range(MAX_CAPABILITY_PAGES):
        result = list_page(cursor)
        entries.extend(extract(result))
        if len(entries) >= MAX_CAPABILITY_ENTRIES:
            entries = entries[:MAX_CAPABILITY_ENTRIES]
            truncated = True
            break
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            break
    else:
        truncated = True
    return entries, truncated


def _prompt_entries(result) -> List[MCPPromptEntry]:
    out: List[MCPPromptEntry] = []
    for prompt in getattr(result, "prompts", None) or []:
        name = getattr(prompt, "name", None)
        if not name:
            continue
        out.append(
            MCPPromptEntry(
                name=name,
                title=_clip(getattr(prompt, "title", None)),
                description=_clip(getattr(prompt, "description", None)),
                arguments=[
                    getattr(arg, "name", "")
                    for arg in (getattr(prompt, "arguments", None) or [])
                    if getattr(arg, "name", None)
                ],
            )
        )
    return out


def _resource_entries(result, *, templates: bool) -> List[MCPResourceEntry]:
    out: List[MCPResourceEntry] = []
    source = (
        getattr(result, "resourceTemplates", None)
        if templates
        else getattr(result, "resources", None)
    ) or []
    for resource in source:
        # A template carries `uriTemplate`; a concrete resource carries `uri`.
        uri = getattr(resource, "uriTemplate", None) if templates else None
        uri = uri or getattr(resource, "uri", None)
        if not uri:
            continue
        out.append(
            MCPResourceEntry(
                uri=str(uri),
                name=_clip(getattr(resource, "name", None)),
                description=_clip(getattr(resource, "description", None)),
                mime_type=getattr(resource, "mimeType", None),
                uri_template=templates,
            )
        )
    return out


async def discover_capabilities_for_saved_tool(
    tool: ToolDefinition,
    oauth_token: Optional[str] = None,
    discovered_by: Optional[str] = None,
) -> ToolCapabilitySnapshot:
    """Ask a saved MCP tool what prompts and resources it exposes.

    Each listing is attempted independently and its failure is swallowed into
    ``supports_*=False``. A server that implements tools but not prompts answers
    ``prompts/list`` with a JSON-RPC "method not found", which is normal and must
    not cost us the resources listing — or the whole snapshot.

    A transport-level failure (server unreachable, auth rejected) is different:
    nothing was learned, so the snapshot records ``error`` and the caller can
    keep showing the previous one rather than replacing it with emptiness.

    Gateway (``mcp``) tools are not probed. The AgentCore Gateway enumerates a
    target's *tools* at registration and exposes no prompt or resource surface,
    so there is nothing on the other end to ask.
    """
    snapshot = ToolCapabilitySnapshot(
        tool_id=tool.tool_id,
        discovered_at=datetime.now(timezone.utc).isoformat(),
        discovered_by=discovered_by,
    )

    if tool.protocol != "mcp_external" or not tool.mcp_config:
        snapshot.error = "This tool is not an external MCP server."
        return snapshot

    from agents.main_agent.integrations.external_mcp_client import (
        create_external_mcp_client,
    )

    forward = bool(getattr(tool, "forward_auth_token", False))
    client = create_external_mcp_client(
        config=tool.mcp_config,
        tool_definition=tool,
        oauth_token=oauth_token if (forward or tool.requires_oauth_provider) else None,
    )
    if client is None:
        snapshot.error = "Could not build a client for this server."
        return snapshot

    def _probe() -> ToolCapabilitySnapshot:
        # One session for both listings — a second connect would double the
        # handshake cost and, for a 3LO server, the token round-trip with it.
        with client:
            try:
                prompts, prompts_truncated = _paginate(
                    lambda cursor: client.list_prompts_sync(pagination_token=cursor),
                    _prompt_entries,
                )
                snapshot.prompts = prompts
                snapshot.supports_prompts = True
                snapshot.truncated = snapshot.truncated or prompts_truncated
            except Exception as exc:  # noqa: BLE001 - unsupported is the common case
                logger.debug("prompts/list unavailable for %s: %s", tool.tool_id, exc)

            resources: List[MCPResourceEntry] = []
            supports_resources = False
            try:
                listed, listed_truncated = _paginate(
                    lambda cursor: client.list_resources_sync(pagination_token=cursor),
                    lambda result: _resource_entries(result, templates=False),
                )
                resources.extend(listed)
                supports_resources = True
                snapshot.truncated = snapshot.truncated or listed_truncated
            except Exception as exc:  # noqa: BLE001
                logger.debug("resources/list unavailable for %s: %s", tool.tool_id, exc)

            try:
                templated, templated_truncated = _paginate(
                    lambda cursor: client.list_resource_templates_sync(
                        pagination_token=cursor
                    ),
                    lambda result: _resource_entries(result, templates=True),
                )
                resources.extend(templated)
                supports_resources = True
                snapshot.truncated = snapshot.truncated or templated_truncated
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "resources/templates/list unavailable for %s: %s",
                    tool.tool_id,
                    exc,
                )

            snapshot.resources = resources[:MAX_CAPABILITY_ENTRIES]
            snapshot.supports_resources = supports_resources
            return snapshot

    try:
        return await asyncio.to_thread(_probe)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as `error`
        logger.warning("Capability discovery failed for %s: %s", tool.tool_id, exc)
        snapshot.error = f"Could not reach the MCP server: {exc}"
        return snapshot
