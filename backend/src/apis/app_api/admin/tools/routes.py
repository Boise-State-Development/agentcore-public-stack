"""Admin API routes for tool catalog management."""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apis.shared.auth import User, require_admin
from apis.app_api.tools.service import get_tool_catalog_service
from apis.app_api.tools.models import (
    ToolCreateRequest,
    ToolUpdateRequest,
    ToolRolesResponse,
    SetToolRolesRequest,
    AddRemoveRolesRequest,
    AdminToolResponse,
    AdminToolListResponse,
    ToolDefinition,
    MCPDiscoverRequest,
    MCPDiscoverResponse,
    DiscoveredMCPTool,
    MCPAuthType,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["admin-tools"])


@router.get("/", response_model=AdminToolListResponse)
async def admin_list_all_tools(
    status: Optional[str] = Query(None, description="Filter by status (active, deprecated, disabled)"),
    admin: User = Depends(require_admin),
):
    """
    List all tools in the catalog with their role assignments.

    Requires admin access.

    Args:
        status: Optional status filter
        admin: Authenticated admin user (injected)

    Returns:
        AdminToolListResponse with all tools
    """
    logger.info("Admin listing full tool catalog")

    service = get_tool_catalog_service()
    tools = await service.get_all_tools(status=status, include_roles=True)

    return AdminToolListResponse(
        tools=[AdminToolResponse.from_tool_definition(t) for t in tools],
        total=len(tools),
    )


@router.get("/{tool_id}", response_model=AdminToolResponse)
async def admin_get_tool(
    tool_id: str,
    admin: User = Depends(require_admin),
):
    """
    Get a specific tool by ID.

    Requires admin access.

    Args:
        tool_id: Tool identifier
        admin: Authenticated admin user (injected)

    Returns:
        AdminToolResponse for the tool
    """
    logger.info("Admin getting tool")

    service = get_tool_catalog_service()
    tool = await service.get_tool(tool_id)

    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    # Get roles for this tool
    roles = await service.get_roles_for_tool(tool_id)
    allowed_roles = [r.role_id for r in roles if r.grant_type == "direct"]

    return AdminToolResponse.from_tool_definition(tool, allowed_roles)


@router.post("/", response_model=AdminToolResponse)
async def admin_create_tool(
    request: ToolCreateRequest,
    admin: User = Depends(require_admin),
):
    """
    Create a new tool catalog entry.

    Requires admin access. This only creates the catalog entry.
    To grant access to AppRoles, use the role management endpoints.

    For MCP external tools, provide mcpConfig with server URL and auth settings.
    For A2A tools, provide a2aConfig with agent URL and capabilities.

    Args:
        request: Tool creation data
        admin: Authenticated admin user (injected)

    Returns:
        Created AdminToolResponse
    """
    logger.info("Admin creating tool")

    service = get_tool_catalog_service()

    # Convert MCP and A2A config requests to models if provided
    mcp_config = request.mcp_config.to_model() if request.mcp_config else None
    a2a_config = request.a2a_config.to_model() if request.a2a_config else None

    tool = ToolDefinition(
        tool_id=request.tool_id,
        display_name=request.display_name,
        description=request.description,
        category=request.category,
        protocol=request.protocol,
        status=request.status,
        requires_oauth_provider=request.requires_oauth_provider,
        forward_auth_token=request.forward_auth_token,
        is_public=request.is_public,
        enabled_by_default=request.enabled_by_default,
        mcp_config=mcp_config,
        a2a_config=a2a_config,
    )

    try:
        created = await service.create_tool(tool, admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Drop the all-tool-ids snapshot so the new tool is recognized by
    # ToolAccessService on the very next chat turn in this process.
    from apis.app_api.tools.freshness import invalidate as invalidate_freshness
    invalidate_freshness(created.tool_id)

    return AdminToolResponse.from_tool_definition(created)


@router.put("/{tool_id}", response_model=AdminToolResponse)
async def admin_update_tool(
    tool_id: str,
    request: ToolUpdateRequest,
    admin: User = Depends(require_admin),
):
    """
    Update tool metadata.

    Requires admin access.

    For MCP external tools, provide mcpConfig with server URL and auth settings.
    For A2A tools, provide a2aConfig with agent URL and capabilities.

    Args:
        tool_id: Tool identifier
        request: Fields to update
        admin: Authenticated admin user (injected)

    Returns:
        Updated AdminToolResponse
    """
    logger.info("Admin updating tool")

    service = get_tool_catalog_service()

    updates = request.model_dump(exclude_unset=True, by_alias=False)

    # Convert MCP and A2A config requests to models if provided
    if "mcp_config" in updates and updates["mcp_config"] is not None:
        updates["mcp_config"] = request.mcp_config.to_model()
    if "a2a_config" in updates and updates["a2a_config"] is not None:
        updates["a2a_config"] = request.a2a_config.to_model()

    try:
        updated = await service.update_tool(tool_id, updates, admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not updated:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    # Invalidate the freshness TTL entry so the next chat turn in this
    # process sees the new updated_at immediately (no wait for the TTL
    # to lapse). Other processes pick the change up within one TTL
    # window via their own freshness reads.
    from apis.app_api.tools.freshness import invalidate as invalidate_freshness
    invalidate_freshness(tool_id)

    return AdminToolResponse.from_tool_definition(updated)


@router.delete("/{tool_id}")
async def admin_delete_tool(
    tool_id: str,
    hard: bool = Query(False, description="If true, permanently delete instead of soft delete"),
    admin: User = Depends(require_admin),
):
    """
    Delete a tool from the catalog.

    By default, performs a soft delete (sets status to disabled).
    Use hard=true to permanently delete.

    Requires admin access.

    Args:
        tool_id: Tool identifier
        hard: If true, permanently delete
        admin: Authenticated admin user (injected)

    Returns:
        Success message
    """
    logger.info("Admin deleting tool")

    service = get_tool_catalog_service()
    deleted = await service.delete_tool(tool_id, admin, soft=not hard)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    from apis.app_api.tools.freshness import invalidate as invalidate_freshness
    invalidate_freshness(tool_id)

    action = "deleted" if hard else "disabled"
    return {"message": f"Tool '{tool_id}' {action} successfully"}


# =============================================================================
# MCP Server Discovery
# =============================================================================


@router.post("/discover", response_model=MCPDiscoverResponse)
async def admin_discover_mcp_tools(
    request: MCPDiscoverRequest,
    admin: User = Depends(require_admin),
):
    """Connect to an MCP server with the given config and return its tool list.

    Used by the admin tool form to populate the per-tool entries instead of
    asking admins to type each name. OAuth-gated servers are not supported —
    the admin's session can't supply an end-user token.

    Trust boundary: this endpoint deliberately accepts an arbitrary
    ``server_url`` from an authenticated admin and connects to it from the
    backend's network position. That's the same trust we already extend
    when the admin saves an MCP tool configuration (the agent loop will
    connect to whatever URL is in the catalog), so we don't add an
    SSRF allowlist here. Admins are expected to be able to reach internal
    MCP servers — that's the deployment shape. If a future change exposes
    this beyond admins, add an allowlist (scheme + host) before shipping.

    Args:
        request: MCP server connection details (URL, transport, auth)
        admin: Authenticated admin user (injected)

    Returns:
        MCPDiscoverResponse with the discovered tools and their descriptions
    """
    from agents.main_agent.integrations.external_mcp_client import (
        create_external_mcp_client,
    )

    if request.auth_type in (MCPAuthType.OAUTH2.value, MCPAuthType.OAUTH2):
        raise HTTPException(
            status_code=400,
            detail="OAuth-gated MCP servers can't be discovered server-side; "
            "list the tool names manually.",
        )

    client = create_external_mcp_client(config=request.to_config())
    if client is None:
        raise HTTPException(
            status_code=400,
            detail="Could not build an MCP client for the supplied config — "
            "check the server URL and transport.",
        )

    def _list_tools():
        # MCPClient is a sync context manager that opens a session on enter;
        # `list_tools_sync` performs the MCP `tools/list` call. We push it to
        # a thread so the async event loop stays responsive during connect.
        with client:
            return list(client.list_tools_sync())

    try:
        tools = await asyncio.to_thread(_list_tools)
    except Exception as exc:
        logger.exception("MCP discovery failed for %s", request.server_url)
        raise HTTPException(
            status_code=502,
            detail=f"MCP server did not respond to tools/list: {exc}",
        )

    discovered: list[DiscoveredMCPTool] = []
    for tool in tools:
        # Strands wraps each MCP tool as an MCPAgentTool; spec details live on
        # `mcp_tool` (mirrors the wire-format `Tool` from the MCP SDK).
        spec = getattr(tool, "mcp_tool", None)
        name = getattr(spec, "name", None) or getattr(tool, "tool_name", None)
        description = getattr(spec, "description", None)
        if not name:
            continue
        discovered.append(DiscoveredMCPTool(name=name, description=description))

    return MCPDiscoverResponse(tools=discovered)


# =============================================================================
# Role Assignment Endpoints
# =============================================================================


@router.get("/{tool_id}/roles", response_model=ToolRolesResponse)
async def get_tool_roles(
    tool_id: str,
    admin: User = Depends(require_admin),
):
    """
    Get AppRoles that grant access to this tool.

    Requires admin access.

    Args:
        tool_id: Tool identifier
        admin: Authenticated admin user (injected)

    Returns:
        ToolRolesResponse with role assignments
    """
    logger.info("Admin getting roles for tool")

    service = get_tool_catalog_service()

    # Verify tool exists
    tool = await service.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    roles = await service.get_roles_for_tool(tool_id)

    return ToolRolesResponse(tool_id=tool_id, roles=roles)


@router.put("/{tool_id}/roles")
async def set_tool_roles(
    tool_id: str,
    request: SetToolRolesRequest,
    admin: User = Depends(require_admin),
):
    """
    Set which AppRoles grant access to this tool.

    This replaces the current role assignments. Roles not in the list
    will have this tool removed from their grantedTools.

    Requires admin access.

    Args:
        tool_id: Tool identifier
        request: List of AppRole IDs
        admin: Authenticated admin user (injected)

    Returns:
        Success message
    """
    logger.info("Admin setting roles for tool")

    service = get_tool_catalog_service()

    try:
        await service.set_roles_for_tool(tool_id, request.app_role_ids, admin)
        return {"message": f"Roles updated for tool '{tool_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{tool_id}/roles/add")
async def add_roles_to_tool(
    tool_id: str,
    request: AddRemoveRolesRequest,
    admin: User = Depends(require_admin),
):
    """
    Add AppRoles to tool access (preserves existing).

    Requires admin access.

    Args:
        tool_id: Tool identifier
        request: List of AppRole IDs to add
        admin: Authenticated admin user (injected)

    Returns:
        Success message
    """
    logger.info("Admin adding roles to tool")

    service = get_tool_catalog_service()

    try:
        await service.add_roles_to_tool(tool_id, request.app_role_ids, admin)
        return {"message": f"Roles added to tool '{tool_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{tool_id}/roles/remove")
async def remove_roles_from_tool(
    tool_id: str,
    request: AddRemoveRolesRequest,
    admin: User = Depends(require_admin),
):
    """
    Remove AppRoles from tool access.

    Requires admin access.

    Args:
        tool_id: Tool identifier
        request: List of AppRole IDs to remove
        admin: Authenticated admin user (injected)

    Returns:
        Success message
    """
    logger.info("Admin removing roles from tool")

    service = get_tool_catalog_service()

    try:
        await service.remove_roles_from_tool(tool_id, request.app_role_ids, admin)
        return {"message": f"Roles removed from tool '{tool_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


