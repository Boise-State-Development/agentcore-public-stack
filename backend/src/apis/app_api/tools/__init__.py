"""Tools API module for listing available tools and user permissions."""

from .models import (
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
from .repository import ToolCatalogRepository, get_tool_catalog_repository
from .service import ToolCatalogService, get_tool_catalog_service

__all__ = [
    # Enums
    "ToolCategory",
    "ToolProtocol",
    "ToolStatus",
    # Models
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
    # Repository
    "ToolCatalogRepository",
    "get_tool_catalog_repository",
    # Service
    "ToolCatalogService",
    "get_tool_catalog_service",
]
