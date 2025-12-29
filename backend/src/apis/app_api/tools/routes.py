"""API routes for tool discovery and permissions."""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user, require_admin
from apis.shared.rbac import AppRoleService
from apis.shared.rbac.service import get_app_role_service
from agents.strands_agent.tools import get_tool_catalog_service, ToolCategory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


# =============================================================================
# Response Models
# =============================================================================


class ToolResponse(BaseModel):
    """Response model for a single tool."""

    tool_id: str = Field(..., alias="toolId")
    name: str
    description: str
    category: str
    is_gateway_tool: bool = Field(..., alias="isGatewayTool")
    requires_api_key: Optional[str] = Field(None, alias="requiresApiKey")
    icon: Optional[str] = None

    model_config = {"populate_by_name": True}


class ToolListResponse(BaseModel):
    """Response model for listing tools."""

    tools: List[ToolResponse]
    total: int


class UserToolPermissionsResponse(BaseModel):
    """Response model for user's tool permissions."""

    user_id: str = Field(..., alias="userId")
    allowed_tools: List[str] = Field(..., alias="allowedTools")
    has_wildcard: bool = Field(..., alias="hasWildcard")
    app_roles: List[str] = Field(..., alias="appRoles")

    model_config = {"populate_by_name": True}


# =============================================================================
# Public Endpoints
# =============================================================================


@router.get("/catalog", response_model=ToolListResponse)
async def list_all_tools(
    category: Optional[str] = Query(
        None, description="Filter by category (search, browser, data, utilities, code, gateway)"
    ),
    user: User = Depends(get_current_user),
):
    """
    List all tools available in the system.

    This returns the complete tool catalog with metadata.
    Use /tools/my-permissions to see which tools the current user can access.

    Args:
        category: Optional category filter
        user: Authenticated user (injected)

    Returns:
        ToolListResponse with list of all tools
    """
    logger.info(f"User {user.email} listing tool catalog")

    catalog_service = get_tool_catalog_service()

    if category:
        try:
            cat = ToolCategory(category.lower())
            tools = catalog_service.get_tools_by_category(cat)
        except ValueError:
            # Invalid category, return empty list
            tools = []
    else:
        tools = catalog_service.get_all_tools()

    return ToolListResponse(
        tools=[
            ToolResponse(
                tool_id=t.tool_id,
                name=t.name,
                description=t.description,
                category=t.category.value,
                is_gateway_tool=t.is_gateway_tool,
                requires_api_key=t.requires_api_key,
                icon=t.icon,
            )
            for t in tools
        ],
        total=len(tools),
    )


@router.get("/my-permissions", response_model=UserToolPermissionsResponse)
async def get_my_tool_permissions(
    user: User = Depends(get_current_user),
):
    """
    Get the current user's tool permissions.

    Returns the list of tool IDs the user is allowed to use based on their AppRoles.
    A wildcard (*) in allowed_tools means all tools are allowed.

    Args:
        user: Authenticated user (injected)

    Returns:
        UserToolPermissionsResponse with user's allowed tools
    """
    logger.info(f"User {user.email} checking tool permissions")

    role_service = get_app_role_service()
    permissions = await role_service.resolve_user_permissions(user)

    return UserToolPermissionsResponse(
        user_id=user.user_id,
        allowed_tools=permissions.tools,
        has_wildcard="*" in permissions.tools,
        app_roles=permissions.app_roles,
    )


@router.get("/available", response_model=ToolListResponse)
async def list_available_tools(
    category: Optional[str] = Query(
        None, description="Filter by category (search, browser, data, utilities, code, gateway)"
    ),
    user: User = Depends(get_current_user),
):
    """
    List tools available to the current user.

    This returns only tools the user is authorized to use based on their AppRoles.
    If the user has wildcard access (*), all tools are returned.

    Args:
        category: Optional category filter
        user: Authenticated user (injected)

    Returns:
        ToolListResponse with user's available tools
    """
    logger.info(f"User {user.email} listing available tools")

    catalog_service = get_tool_catalog_service()
    role_service = get_app_role_service()

    # Get user's tool permissions
    permissions = await role_service.resolve_user_permissions(user)
    has_wildcard = "*" in permissions.tools
    allowed_tool_ids = set(permissions.tools)

    # Get tools from catalog
    if category:
        try:
            cat = ToolCategory(category.lower())
            all_tools = catalog_service.get_tools_by_category(cat)
        except ValueError:
            all_tools = []
    else:
        all_tools = catalog_service.get_all_tools()

    # Filter to only allowed tools
    if has_wildcard:
        available_tools = all_tools
    else:
        available_tools = [t for t in all_tools if t.tool_id in allowed_tool_ids]

    return ToolListResponse(
        tools=[
            ToolResponse(
                tool_id=t.tool_id,
                name=t.name,
                description=t.description,
                category=t.category.value,
                is_gateway_tool=t.is_gateway_tool,
                requires_api_key=t.requires_api_key,
                icon=t.icon,
            )
            for t in available_tools
        ],
        total=len(available_tools),
    )


# =============================================================================
# Admin Endpoints
# =============================================================================


@router.get("/admin/catalog", response_model=ToolListResponse)
async def admin_list_all_tools(
    admin: User = Depends(require_admin),
):
    """
    Admin endpoint to list all tools in the catalog.

    Requires admin access.

    Args:
        admin: Authenticated admin user (injected)

    Returns:
        ToolListResponse with all tools
    """
    logger.info(f"Admin {admin.email} listing full tool catalog")

    catalog_service = get_tool_catalog_service()
    tools = catalog_service.get_all_tools()

    return ToolListResponse(
        tools=[
            ToolResponse(
                tool_id=t.tool_id,
                name=t.name,
                description=t.description,
                category=t.category.value,
                is_gateway_tool=t.is_gateway_tool,
                requires_api_key=t.requires_api_key,
                icon=t.icon,
            )
            for t in tools
        ],
        total=len(tools),
    )
