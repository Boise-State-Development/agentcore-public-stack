"""AppRole data models for RBAC system."""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


@dataclass
class EffectivePermissions:
    """Pre-computed permissions for fast authorization checks."""

    tools: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    quota_tier: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "tools": self.tools,
            "models": self.models,
            "quotaTier": self.quota_tier,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EffectivePermissions":
        """Create from dictionary (DynamoDB item)."""
        return cls(
            tools=data.get("tools", []),
            models=data.get("models", []),
            quota_tier=data.get("quotaTier"),
        )


@dataclass
class AppRole:
    """
    Application-level role that maps JWT roles to permissions.

    Permissions are denormalized (pre-computed) on save for fast runtime lookups.
    """

    # Primary identifiers
    role_id: str
    display_name: str
    description: str

    # JWT Mapping
    jwt_role_mappings: List[str] = field(default_factory=list)

    # Inheritance (single level only)
    inherits_from: List[str] = field(default_factory=list)

    # Denormalized permissions (computed on save)
    effective_permissions: EffectivePermissions = field(
        default_factory=EffectivePermissions
    )

    # Direct permission grants (before inheritance resolution)
    granted_tools: List[str] = field(default_factory=list)
    granted_models: List[str] = field(default_factory=list)

    # Metadata
    priority: int = 0
    is_system_role: bool = False
    enabled: bool = True

    # Audit fields
    created_at: str = ""
    updated_at: str = ""
    created_by: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "roleId": self.role_id,
            "displayName": self.display_name,
            "description": self.description,
            "jwtRoleMappings": self.jwt_role_mappings,
            "inheritsFrom": self.inherits_from,
            "effectivePermissions": self.effective_permissions.to_dict(),
            "grantedTools": self.granted_tools,
            "grantedModels": self.granted_models,
            "priority": self.priority,
            "isSystemRole": self.is_system_role,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "createdBy": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppRole":
        """Create from dictionary (DynamoDB item)."""
        effective_perms_data = data.get("effectivePermissions", {})
        return cls(
            role_id=data.get("roleId", ""),
            display_name=data.get("displayName", ""),
            description=data.get("description", ""),
            jwt_role_mappings=data.get("jwtRoleMappings", []),
            inherits_from=data.get("inheritsFrom", []),
            effective_permissions=EffectivePermissions.from_dict(effective_perms_data),
            granted_tools=data.get("grantedTools", []),
            granted_models=data.get("grantedModels", []),
            priority=data.get("priority", 0),
            is_system_role=data.get("isSystemRole", False),
            enabled=data.get("enabled", True),
            created_at=data.get("createdAt", ""),
            updated_at=data.get("updatedAt", ""),
            created_by=data.get("createdBy"),
        )


@dataclass
class UserEffectivePermissions:
    """
    Merged permissions for a specific user based on all their AppRoles.

    This is computed at runtime and cached per-user.
    """

    user_id: str
    app_roles: List[str]
    tools: List[str]
    models: List[str]
    quota_tier: Optional[str]
    resolved_at: str

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "userId": self.user_id,
            "appRoles": self.app_roles,
            "tools": self.tools,
            "models": self.models,
            "quotaTier": self.quota_tier,
            "resolvedAt": self.resolved_at,
        }


# =============================================================================
# Pydantic Models for API Request/Response
# =============================================================================


class AppRoleCreate(BaseModel):
    """Request body for creating a new AppRole."""

    role_id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{2,49}$", alias="roleId")
    display_name: str = Field(..., min_length=1, max_length=100, alias="displayName")
    description: str = Field("", max_length=500)
    jwt_role_mappings: List[str] = Field(
        default_factory=list, alias="jwtRoleMappings"
    )
    inherits_from: List[str] = Field(default_factory=list, alias="inheritsFrom")
    granted_tools: List[str] = Field(default_factory=list, alias="grantedTools")
    granted_models: List[str] = Field(default_factory=list, alias="grantedModels")
    priority: int = Field(0, ge=0, le=999)
    enabled: bool = True

    model_config = {"populate_by_name": True}


class AppRoleUpdate(BaseModel):
    """Request body for updating an AppRole (partial update)."""

    display_name: Optional[str] = Field(
        None, min_length=1, max_length=100, alias="displayName"
    )
    description: Optional[str] = Field(None, max_length=500)
    jwt_role_mappings: Optional[List[str]] = Field(None, alias="jwtRoleMappings")
    inherits_from: Optional[List[str]] = Field(None, alias="inheritsFrom")
    granted_tools: Optional[List[str]] = Field(None, alias="grantedTools")
    granted_models: Optional[List[str]] = Field(None, alias="grantedModels")
    priority: Optional[int] = Field(None, ge=0, le=999)
    enabled: Optional[bool] = None

    model_config = {"populate_by_name": True}


class EffectivePermissionsResponse(BaseModel):
    """Computed effective permissions response."""

    tools: List[str]
    models: List[str]
    quota_tier: Optional[str] = Field(None, alias="quotaTier")

    model_config = {"populate_by_name": True}


class AppRoleResponse(BaseModel):
    """Response model for an AppRole."""

    role_id: str = Field(..., alias="roleId")
    display_name: str = Field(..., alias="displayName")
    description: str
    jwt_role_mappings: List[str] = Field(..., alias="jwtRoleMappings")
    inherits_from: List[str] = Field(..., alias="inheritsFrom")
    granted_tools: List[str] = Field(..., alias="grantedTools")
    granted_models: List[str] = Field(..., alias="grantedModels")
    effective_permissions: EffectivePermissionsResponse = Field(
        ..., alias="effectivePermissions"
    )
    priority: int
    is_system_role: bool = Field(..., alias="isSystemRole")
    enabled: bool
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: Optional[str] = Field(None, alias="createdBy")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_app_role(cls, role: AppRole) -> "AppRoleResponse":
        """Create response from AppRole dataclass."""
        return cls(
            role_id=role.role_id,
            display_name=role.display_name,
            description=role.description,
            jwt_role_mappings=role.jwt_role_mappings,
            inherits_from=role.inherits_from,
            granted_tools=role.granted_tools,
            granted_models=role.granted_models,
            effective_permissions=EffectivePermissionsResponse(
                tools=role.effective_permissions.tools,
                models=role.effective_permissions.models,
                quota_tier=role.effective_permissions.quota_tier,
            ),
            priority=role.priority,
            is_system_role=role.is_system_role,
            enabled=role.enabled,
            created_at=role.created_at,
            updated_at=role.updated_at,
            created_by=role.created_by,
        )


class AppRoleListResponse(BaseModel):
    """Response model for listing roles."""

    roles: List[AppRoleResponse]
    total: int


class CacheStatsResponse(BaseModel):
    """Cache statistics response."""

    user_cache_size: int = Field(..., alias="userCacheSize")
    user_cache_expired: int = Field(..., alias="userCacheExpired")
    role_cache_size: int = Field(..., alias="roleCacheSize")
    role_cache_expired: int = Field(..., alias="roleCacheExpired")
    jwt_mapping_cache_size: int = Field(..., alias="jwtMappingCacheSize")
    jwt_mapping_cache_expired: int = Field(..., alias="jwtMappingCacheExpired")

    model_config = {"populate_by_name": True}
