"""Admin API routes for tool catalog management."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apis.shared.auth import User, require_admin
from apis.app_api.tools.service import ToolCatalogService, get_tool_catalog_service
from apis.app_api.tools.models import (
    ToolCreateRequest,
    ToolUpdateRequest,
    ToolRolesResponse,
    SetToolRolesRequest,
    AddRemoveRolesRequest,
    AdminToolResponse,
    AdminToolListResponse,
    SyncResult,
    ToolDefinition,
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
    logger.info(f"Admin {admin.email} listing full tool catalog")

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
    logger.info(f"Admin {admin.email} getting tool: {tool_id}")

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
    logger.info(f"Admin {admin.email} creating tool: {request.tool_id}")

    service = get_tool_catalog_service()

    # Convert MCP and A2A config requests to models if provided
    mcp_config = request.mcp_config.to_model() if request.mcp_config else None
    a2a_config = request.a2a_config.to_model() if request.a2a_config else None

    tool = ToolDefinition(
        tool_id=request.tool_id,
        display_name=request.display_name,
        description=request.description,
        category=request.category,
        icon=request.icon,
        protocol=request.protocol,
        status=request.status,
        requires_oauth_provider=request.requires_oauth_provider,
        is_public=request.is_public,
        enabled_by_default=request.enabled_by_default,
        mcp_config=mcp_config,
        a2a_config=a2a_config,
    )

    try:
        created = await service.create_tool(tool, admin)
        return AdminToolResponse.from_tool_definition(created)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    logger.info(f"Admin {admin.email} updating tool: {tool_id}")

    service = get_tool_catalog_service()

    updates = request.model_dump(exclude_unset=True, by_alias=False)

    # Convert MCP and A2A config requests to models if provided
    if "mcp_config" in updates and updates["mcp_config"] is not None:
        updates["mcp_config"] = request.mcp_config.to_model()
    if "a2a_config" in updates and updates["a2a_config"] is not None:
        updates["a2a_config"] = request.a2a_config.to_model()

    updated = await service.update_tool(tool_id, updates, admin)

    if not updated:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

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
    logger.info(f"Admin {admin.email} deleting tool: {tool_id} (hard={hard})")

    service = get_tool_catalog_service()
    deleted = await service.delete_tool(tool_id, admin, soft=not hard)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    action = "deleted" if hard else "disabled"
    return {"message": f"Tool '{tool_id}' {action} successfully"}


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
    logger.info(f"Admin {admin.email} getting roles for tool: {tool_id}")

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
    logger.info(f"Admin {admin.email} setting roles for tool: {tool_id}")

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
    logger.info(f"Admin {admin.email} adding roles to tool: {tool_id}")

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
    logger.info(f"Admin {admin.email} removing roles from tool: {tool_id}")

    service = get_tool_catalog_service()

    try:
        await service.remove_roles_from_tool(tool_id, request.app_role_ids, admin)
        return {"message": f"Roles removed from tool '{tool_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Sync Endpoints
# =============================================================================


@router.post("/sync", response_model=SyncResult)
async def sync_from_registry(
    dry_run: bool = Query(True, description="If true, only report what would happen"),
    admin: User = Depends(require_admin),
):
    """
    Sync catalog from code registry.

    Discovers tools from the backend tool registry and updates the catalog:
    - Creates entries for new tools
    - Marks orphaned tools as deprecated

    Requires admin access.

    Args:
        dry_run: If true, only report changes without applying
        admin: Authenticated admin user (injected)

    Returns:
        SyncResult with discovered, orphaned, and unchanged tools
    """
    logger.info(f"Admin {admin.email} syncing tool catalog (dry_run={dry_run})")

    service = get_tool_catalog_service()
    result = await service.sync_catalog_from_registry(admin, dry_run=dry_run)

    return result
