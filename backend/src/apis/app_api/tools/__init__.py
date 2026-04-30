"""Tools API module for listing available tools and user permissions.

Models and repository live in apis.shared.tools.
Service and routes remain here as they are app_api-specific.
"""

from apis.shared.tools.models import (
    ToolCategory,
    ToolProtocol,
    ToolStatus,
    ToolDefinition,
    UserToolPreference,
    UserToolAccess,
    UserToolsResponse,
    ToolPreferencesRequest,
    ToolCreateRequest,
    ToolUpdateRequest,
    ToolRoleAssignment,
    ToolRolesResponse,
    SetToolRolesRequest,
    AddRemoveRolesRequest,
    AdminToolResponse,
    AdminToolListResponse,
)
from apis.shared.tools.repository import ToolCatalogRepository, get_tool_catalog_repository
from .service import ToolCatalogService, get_tool_catalog_service

__all__ = [
    "ToolCategory",
    "ToolProtocol",
    "ToolStatus",
    "ToolDefinition",
    "UserToolPreference",
    "UserToolAccess",
    "UserToolsResponse",
    "ToolPreferencesRequest",
    "ToolCreateRequest",
    "ToolUpdateRequest",
    "ToolRoleAssignment",
    "ToolRolesResponse",
    "SetToolRolesRequest",
    "AddRemoveRolesRequest",
    "AdminToolResponse",
    "AdminToolListResponse",
    "ToolCatalogRepository",
    "get_tool_catalog_repository",
    "ToolCatalogService",
    "get_tool_catalog_service",
]
