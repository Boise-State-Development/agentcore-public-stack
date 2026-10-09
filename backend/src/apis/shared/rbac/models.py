"""AppRole data models for RBAC system."""

from dataclasses import dataclass, field
from typing import List, Optional
from pydantic import BaseModel, Field


@dataclass
class EffectivePermissions:
    """Pre-computed permissions for fast authorization checks."""

    tools: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    quota_tier: Optional[str] = None
    # Delegated admin feature areas. Unlike the three axes above, this one does
    # NOT absorb grants from `inherits_from` — see `_compute_effective_permissions`.
    admin_scopes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "tools": self.tools,
            "models": self.models,
            "skills": self.skills,
            "quotaTier": self.quota_tier,
            "adminScopes": self.admin_scopes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EffectivePermissions":
        """Create from dictionary (DynamoDB item).

        `skills` and `admin_scopes` default to [] for roles persisted before
        those axes existed — they pick the new axis up on their next save/sync
        (mirror of how a new permission type rolls out for tools/models).
        """
        return cls(
            tools=data.get("tools", []),
            models=data.get("models", []),
            skills=data.get("skills", []),
            quota_tier=data.get("quotaTier"),
            admin_scopes=data.get("adminScopes", []),
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
    granted_skills: List[str] = field(default_factory=list)
    # Delegated admin feature areas (see `rbac/admin_scopes.py`). Stored as a
    # plain attribute on the DEFINITION item rather than as `*_GRANT#` items
    # with a GSI, because nothing needs the reverse lookup and every extra
    # mapping prefix is another thing `_delete_mapping_items` must know about.
    granted_admin_scopes: List[str] = field(default_factory=list)

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
            "grantedSkills": self.granted_skills,
            "grantedAdminScopes": self.granted_admin_scopes,
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
            granted_skills=data.get("grantedSkills", []),
            granted_admin_scopes=data.get("grantedAdminScopes", []),
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
    # Trailing defaults so existing positional/kwargs construction sites that
    # predate these axes keep working (they resolve to none of them).
    skills: List[str] = field(default_factory=list)
    admin_scopes: List[str] = field(default_factory=list)
    # The subset of ``tools`` / ``models`` / ``skills`` that came from the
    # user's own :class:`UserGrant` rather than a role. Already unioned into
    # the main lists (which every gate reads); these exist so a display
    # surface (the tool picker's ``grantedBy``) can say *why* without a
    # second lookup. Never hold ``"*"`` — a direct grant has no wildcard.
    direct_tools: List[str] = field(default_factory=list)
    direct_models: List[str] = field(default_factory=list)
    direct_skills: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "userId": self.user_id,
            "appRoles": self.app_roles,
            "tools": self.tools,
            "models": self.models,
            "skills": self.skills,
            "adminScopes": self.admin_scopes,
            "quotaTier": self.quota_tier,
            "resolvedAt": self.resolved_at,
            "directTools": self.direct_tools,
            "directModels": self.direct_models,
            "directSkills": self.direct_skills,
        }


@dataclass
class UserGrant:
    """Tools, models and skills granted to **one user** directly, beside their roles.

    A role is the normal way access is handed out, and stays so. A direct
    grant is for the cases a role fits badly: one person piloting a tool
    before their cohort gets it, a researcher who needs a model nobody else
    in their role does, a temporary exception with an end date. It is
    **additive only** — it can widen what the user's roles give, never
    narrow it. A deny lives on the resource (a catalog row's ``enabled``),
    not here.

    Resolution unions these lists into ``UserEffectivePermissions`` beside the
    role grants (``AppRoleService._merge_permissions``), so every gate that
    reads the resolved object honours them without knowing they exist. The
    ids are the same catalog ids a role grants: **base** tool ids (an MCP
    server as a whole, not a ``server::tool`` scoped ref), provider model
    ids, skill ids. There is no ``"*"``: a wildcard is a role concept, and a
    per-person superuser is a role assignment, not a grant.

    ``expires_at`` (ISO 8601, UTC) makes the grant lapse by itself — the
    resolver treats an expired grant as absent, and the per-user permission
    cache (5 min) bounds how late that lands. The row is left in place for
    the admin to see and clean up; nothing deletes it for them.
    """

    user_id: str
    granted_tools: List[str] = field(default_factory=list)
    granted_models: List[str] = field(default_factory=list)
    granted_skills: List[str] = field(default_factory=list)
    expires_at: Optional[str] = None
    note: str = ""

    # Audit fields
    granted_by: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def is_empty(self) -> bool:
        """True when the grant hands out nothing at all."""
        return not (self.granted_tools or self.granted_models or self.granted_skills)

    def is_active(self, now_iso: str) -> bool:
        """Whether the grant is in force at ``now_iso`` (ISO 8601 UTC).

        A missing ``expires_at`` never lapses. Both sides are the one
        canonical spelling ``apis.shared.timestamps`` produces, so a string
        comparison is a time comparison; an unparseable stored value is
        treated as expired rather than as open-ended, because the failure
        mode of "a typo grants forever" is the worse one.
        """
        if not self.expires_at:
            return True
        try:
            from datetime import datetime

            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            return False
        return expires > now

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "userId": self.user_id,
            "grantedTools": self.granted_tools,
            "grantedModels": self.granted_models,
            "grantedSkills": self.granted_skills,
            "expiresAt": self.expires_at,
            "note": self.note,
            "grantedBy": self.granted_by,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserGrant":
        """Create from dictionary (DynamoDB item)."""
        return cls(
            user_id=data.get("userId", ""),
            granted_tools=list(data.get("grantedTools", []) or []),
            granted_models=list(data.get("grantedModels", []) or []),
            granted_skills=list(data.get("grantedSkills", []) or []),
            expires_at=data.get("expiresAt") or None,
            note=data.get("note", "") or "",
            granted_by=data.get("grantedBy"),
            created_at=data.get("createdAt", ""),
            updated_at=data.get("updatedAt", ""),
        )


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
    granted_skills: List[str] = Field(default_factory=list, alias="grantedSkills")
    granted_admin_scopes: List[str] = Field(
        default_factory=list, alias="grantedAdminScopes"
    )
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
    granted_skills: Optional[List[str]] = Field(None, alias="grantedSkills")
    granted_admin_scopes: Optional[List[str]] = Field(
        None, alias="grantedAdminScopes"
    )
    priority: Optional[int] = Field(None, ge=0, le=999)
    enabled: Optional[bool] = None

    model_config = {"populate_by_name": True}


class EffectivePermissionsResponse(BaseModel):
    """Computed effective permissions response."""

    tools: List[str]
    models: List[str]
    skills: List[str] = Field(default_factory=list)
    admin_scopes: List[str] = Field(default_factory=list, alias="adminScopes")
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
    granted_skills: List[str] = Field(default_factory=list, alias="grantedSkills")
    granted_admin_scopes: List[str] = Field(
        default_factory=list, alias="grantedAdminScopes"
    )
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
            granted_skills=role.granted_skills,
            granted_admin_scopes=role.granted_admin_scopes,
            effective_permissions=EffectivePermissionsResponse(
                tools=role.effective_permissions.tools,
                models=role.effective_permissions.models,
                skills=role.effective_permissions.skills,
                admin_scopes=role.effective_permissions.admin_scopes,
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


class AdminScopeResponse(BaseModel):
    """One entry from the delegated admin scope registry."""

    id: str
    label: str
    group: str
    description: str
    delegable: bool

    model_config = {"populate_by_name": True}


class AdminScopeListResponse(BaseModel):
    """The admin scope registry, for building the role form's scope picker.

    Non-delegable scopes are included rather than filtered out: the picker
    should *show* that `admin.roles` and `admin.auth_providers` exist and
    explain that they cannot be granted, instead of leaving an admin wondering
    why those two areas are missing. The client renders them disabled; the
    server rejects them regardless (`validate_admin_scopes`).
    """

    scopes: List[AdminScopeResponse]
    total: int


class UserGrantUpdate(BaseModel):
    """Request body for ``PUT /admin/user-grants/{user_id}`` — a full replace.

    The form posts every list on every save, so a partial-update shape would
    only invite a client to drop a list by omission. Replace semantics make
    the stored grant exactly what the admin last saw on screen.
    """

    granted_tools: List[str] = Field(default_factory=list, alias="grantedTools")
    granted_models: List[str] = Field(default_factory=list, alias="grantedModels")
    granted_skills: List[str] = Field(default_factory=list, alias="grantedSkills")
    expires_at: Optional[str] = Field(None, alias="expiresAt")
    note: str = Field("", max_length=500)

    model_config = {"populate_by_name": True}


class UserGrantResponse(BaseModel):
    """One user's direct grant. An absent grant is served as the empty shape."""

    user_id: str = Field(..., alias="userId")
    granted_tools: List[str] = Field(default_factory=list, alias="grantedTools")
    granted_models: List[str] = Field(default_factory=list, alias="grantedModels")
    granted_skills: List[str] = Field(default_factory=list, alias="grantedSkills")
    expires_at: Optional[str] = Field(None, alias="expiresAt")
    note: str = ""
    granted_by: Optional[str] = Field(None, alias="grantedBy")
    created_at: str = Field("", alias="createdAt")
    updated_at: str = Field("", alias="updatedAt")
    # Derived on read so the SPA does not have to compare clocks.
    active: bool = True

    model_config = {"populate_by_name": True}

    @classmethod
    def from_user_grant(cls, grant: "UserGrant", now_iso: str) -> "UserGrantResponse":
        """Create response from UserGrant dataclass."""
        return cls(
            user_id=grant.user_id,
            granted_tools=grant.granted_tools,
            granted_models=grant.granted_models,
            granted_skills=grant.granted_skills,
            expires_at=grant.expires_at,
            note=grant.note,
            granted_by=grant.granted_by,
            created_at=grant.created_at,
            updated_at=grant.updated_at,
            active=grant.is_active(now_iso),
        )

    @classmethod
    def empty(cls, user_id: str) -> "UserGrantResponse":
        """The shape of "this user has no direct grant"."""
        return cls(user_id=user_id)


class UserGrantListResponse(BaseModel):
    """Every user holding a direct grant."""

    grants: List[UserGrantResponse]
    total: int


class UserGrantHoldersResponse(BaseModel):
    """The users directly granted one resource (reverse lookup)."""

    kind: str
    resource_id: str = Field(..., alias="resourceId")
    user_ids: List[str] = Field(default_factory=list, alias="userIds")

    model_config = {"populate_by_name": True}


class CacheStatsResponse(BaseModel):
    """Cache statistics response."""

    user_cache_size: int = Field(..., alias="userCacheSize")
    user_cache_expired: int = Field(..., alias="userCacheExpired")
    role_cache_size: int = Field(..., alias="roleCacheSize")
    role_cache_expired: int = Field(..., alias="roleCacheExpired")
    jwt_mapping_cache_size: int = Field(..., alias="jwtMappingCacheSize")
    jwt_mapping_cache_expired: int = Field(..., alias="jwtMappingCacheExpired")

    model_config = {"populate_by_name": True}
