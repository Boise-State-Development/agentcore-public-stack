"""API routes for tool discovery and user permissions.

Admin tool management routes are in apis.app_api.admin.tools.routes.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user
from apis.shared.rbac.service import get_app_role_service

# Import legacy service for backward compatibility
from agents.main_agent.tools import (
    get_tool_catalog_service as get_legacy_catalog_service,
    ToolCategory as LegacyToolCategory,
)

# Import new service and models
from .service import get_tool_catalog_service
from .models import (
    UserToolsResponse,
    ToolPreferencesRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


# =============================================================================
# Legacy Response Models (for backward compatibility)
# =============================================================================


class LegacyToolResponse(BaseModel):
    """Response model for a single tool (legacy format)."""

    tool_id: str = Field(..., alias="toolId")
    name: str
    description: str
    category: str
    is_gateway_tool: bool = Field(..., alias="isGatewayTool")
    icon: Optional[str] = None

    model_config = {"populate_by_name": True}


class LegacyToolListResponse(BaseModel):
    """Response model for listing tools (legacy format)."""

    tools: List[LegacyToolResponse]
    total: int


class UserToolPermissionsResponse(BaseModel):
    """Response model for user's tool permissions."""

    user_id: str = Field(..., alias="userId")
    allowed_tools: List[str] = Field(..., alias="allowedTools")
    has_wildcard: bool = Field(..., alias="hasWildcard")
    app_roles: List[str] = Field(..., alias="appRoles")

    model_config = {"populate_by_name": True}


# =============================================================================
# Public User Endpoints
# =============================================================================


@router.get("/", response_model=UserToolsResponse)
async def get_user_tools(
    user: User = Depends(get_current_user),
):
    """
    Get tools available to the current user with preferences merged.

    This is the main endpoint for the frontend to fetch user's tools.
    Returns tools based on AppRole permissions and user preferences.

    Args:
        user: Authenticated user (injected)

    Returns:
        UserToolsResponse with user's accessible tools
    """
    logger.info(f"User {user.email} getting tools with preferences")

    service = get_tool_catalog_service()
    tools = await service.get_user_accessible_tools(user)
    categories = await service.get_categories(user)

    # Get AppRoles applied
    role_service = get_app_role_service()
    permissions = await role_service.resolve_user_permissions(user)

    return UserToolsResponse(
        tools=tools,
        categories=categories,
        app_roles_applied=permissions.app_roles,
    )


@router.put("/preferences")
async def update_tool_preferences(
    request: ToolPreferencesRequest,
    user: User = Depends(get_current_user),
):
    """
    Save user's tool enabled/disabled preferences.

    Only accepts preferences for tools the user has access to.

    Args:
        request: Tool preferences to save
        user: Authenticated user (injected)

    Returns:
        Success message
    """
    logger.info(f"User {user.email} updating tool preferences")

    service = get_tool_catalog_service()

    try:
        await service.save_user_preferences(user, request.preferences)
        return {"message": "Preferences saved successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Legacy Public Endpoints (backward compatibility)
# =============================================================================


@router.get("/catalog", response_model=LegacyToolListResponse)
async def list_all_tools(
    category: Optional[str] = Query(
        None, description="Filter by category (search, browser, data, utilities, code, gateway)"
    ),
    user: User = Depends(get_current_user),
):
    """
    List all tools available in the system (legacy endpoint).

    This returns the complete tool catalog with metadata.
    Use GET /tools for the new format with user preferences.

    Args:
        category: Optional category filter
        user: Authenticated user (injected)

    Returns:
        LegacyToolListResponse with list of all tools
    """
    logger.info(f"User {user.email} listing tool catalog (legacy)")

    catalog_service = get_legacy_catalog_service()

    if category:
        try:
            cat = LegacyToolCategory(category.lower())
            tools = catalog_service.get_tools_by_category(cat)
        except ValueError:
            tools = []
    else:
        tools = catalog_service.get_all_tools()

    return LegacyToolListResponse(
        tools=[
            LegacyToolResponse(
                tool_id=t.tool_id,
                name=t.name,
                description=t.description,
                category=t.category.value,
                is_gateway_tool=t.is_gateway_tool,
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


@router.get("/available", response_model=LegacyToolListResponse)
async def list_available_tools(
    category: Optional[str] = Query(
        None, description="Filter by category (search, browser, data, utilities, code, gateway)"
    ),
    user: User = Depends(get_current_user),
):
    """
    List tools available to the current user (legacy endpoint).

    This returns only tools the user is authorized to use based on their AppRoles.
    Use GET /tools for the new format with user preferences.

    Args:
        category: Optional category filter
        user: Authenticated user (injected)

    Returns:
        LegacyToolListResponse with user's available tools
    """
    logger.info(f"User {user.email} listing available tools (legacy)")

    catalog_service = get_legacy_catalog_service()
    role_service = get_app_role_service()

    permissions = await role_service.resolve_user_permissions(user)
    has_wildcard = "*" in permissions.tools
    allowed_tool_ids = set(permissions.tools)

    if category:
        try:
            cat = LegacyToolCategory(category.lower())
            all_tools = catalog_service.get_tools_by_category(cat)
        except ValueError:
            all_tools = []
    else:
        all_tools = catalog_service.get_all_tools()

    if has_wildcard:
        available_tools = all_tools
    else:
        available_tools = [t for t in all_tools if t.tool_id in allowed_tool_ids]

    return LegacyToolListResponse(
        tools=[
            LegacyToolResponse(
                tool_id=t.tool_id,
                name=t.name,
                description=t.description,
                category=t.category.value,
                is_gateway_tool=t.is_gateway_tool,
                icon=t.icon,
            )
            for t in available_tools
        ],
        total=len(available_tools),
    )
