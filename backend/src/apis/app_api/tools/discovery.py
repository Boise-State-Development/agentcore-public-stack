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
from typing import List, Optional

from apis.shared.tools.models import DiscoveredMCPTool, ToolDefinition

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
