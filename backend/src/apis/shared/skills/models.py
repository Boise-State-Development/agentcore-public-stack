"""
Skill Catalog Models

Pydantic models for the admin-managed Skill catalog. A Skill is an
instruction bundle (a SKILL.md body) that binds a curated set of existing
catalog tools and is exposed to user roles via RBAC — mirroring how tools
are gated today.

This module is the skills parallel of ``apis/shared/tools/models.py``
(``ToolDefinition``). It is consumed by ``app_api`` (admin authoring) and the
runtime (``agents``/``inference_api``); it never imports from either, to
respect the import boundary (``tests/architecture/test_import_boundaries.py``).

See ``docs/specs/admin-skills-rbac-tool-binding.md`` (§4 Data model, §5
Persistence).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# Regex for a skill_id — identical shape to tool_id (see ToolCreateRequest).
SKILL_ID_PATTERN = r"^[a-z][a-z0-9_]{2,49}$"


class SkillStatus(str, Enum):
    """Availability status of a skill (mirrors ToolStatus)."""

    ACTIVE = "active"
    DRAFT = "draft"
    DISABLED = "disabled"


class SkillVisibility(str, Enum):
    """Ownership visibility of a skill.

    Reserved for Phase 2 (user-authored & shared skills). v1 always writes
    ``ADMIN`` — admin-authored, RBAC-gated skills. See spec §10.
    """

    ADMIN = "admin"
    PRIVATE = "private"
    SHARED = "shared"


# =============================================================================
# Database Models (stored in DynamoDB)
# =============================================================================


class SkillResourceRef(BaseModel):
    """Manifest entry for one of a skill's supporting reference files.

    A skill's reference files (read-only markdown/resources for deep
    progressive disclosure) live as bytes in the ``skill-resources`` S3
    bucket — never inline in the DynamoDB item (400 KB limit). The
    ``SkillDefinition`` row carries only this lightweight manifest; the
    bytes are addressed by ``s3_key`` (content-hash keyed, so identical
    content within a skill dedupes to one object). See
    ``docs/specs/admin-skills-rbac-tool-binding.md`` (§0.2, §5, PR-4) and
    ``apis/shared/skills/resource_store.py``.

    camelCase aliases are declared so the same model round-trips both the
    admin API response (FastAPI serializes by alias) and is constructible
    from snake_case kwargs (``populate_by_name``). DynamoDB (de)serialization
    is handled explicitly in ``SkillDefinition.to_dynamo_item`` /
    ``from_dynamo_item``.
    """

    filename: str = Field(..., description="Display filename, e.g. 'forms.md'")
    content_hash: str = Field(
        ..., alias="contentHash", description="sha256 hex of the file bytes"
    )
    size: int = Field(..., description="Size of the file in bytes")
    content_type: str = Field(
        ..., alias="contentType", description="MIME type, e.g. 'text/markdown'"
    )
    s3_key: str = Field(
        ...,
        alias="s3Key",
        description="Object key in the skill-resources bucket "
        "(skills/{skill_id}/{content_hash})",
    )

    model_config = {"populate_by_name": True}


class SkillDefinition(BaseModel):
    """
    Catalog entry for a skill stored in DynamoDB.

    Mirrors ``ToolDefinition``: identity + display metadata + bound
    capabilities + audit, with snake_case→camelCase (de)serialization via
    ``to_dynamo_item`` / ``from_dynamo_item``.

    NOTE: Access control is managed via AppRoles (RBAC — PR-2 of this
    feature), not stored directly on the skill. ``allowed_app_roles`` is
    computed for display purposes only and is intentionally NOT persisted
    (same precedent as ``ToolDefinition.allowed_app_roles``).
    """

    # Identity
    skill_id: str = Field(
        ...,
        pattern=SKILL_ID_PATTERN,
        description="Unique identifier (e.g., 'pdf_workflows')",
    )

    # Display + instruction payload (progressive-disclosure levels)
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(
        ..., description="Level-1 catalog line, injected into the prompt (token-cheap)"
    )
    instructions: str = Field(
        ..., description="Level-2 SKILL.md body, loaded on dispatch"
    )

    # Bound capabilities
    bound_tool_ids: List[str] = Field(
        default_factory=list,
        description="Catalog tool_ids bound to this skill (span all protocols)",
    )
    compose: List[str] = Field(
        default_factory=list,
        description="skill_ids composed into this skill (composite skills)",
    )

    # Supporting reference files (rev 2026-06-09 §0.2; PR-4). Lightweight
    # manifest only — the file BYTES live in the skill-resources S3 bucket
    # (the 400 KB DynamoDB item limit rules out inlining reference docs).
    # Managed via the /admin/skills/{id}/resources endpoints, NOT the
    # create/update body. Old rows without this attribute deserialize to [].
    resources: List[SkillResourceRef] = Field(
        default_factory=list,
        description="Manifest of S3-backed supporting reference files",
    )

    # Lifecycle / grouping
    status: SkillStatus = Field(default=SkillStatus.ACTIVE)
    category: Optional[str] = Field(
        default=None, description="Optional grouping label"
    )

    # Forward-compat (reserved; enforced ADMIN-scope in v1) — see spec §10
    owner_id: str = Field(
        default="system",
        description="Author identity; reserved for Phase 2 user-authored skills",
    )
    visibility: SkillVisibility = Field(
        default=SkillVisibility.ADMIN,
        description="Ownership visibility; reserved for Phase 2",
    )

    # Computed field — which AppRoles grant this skill (for admin UI display).
    # Populated by the admin service from RBAC; not round-tripped to DynamoDB.
    allowed_app_roles: List[str] = Field(
        default_factory=list,
        description="AppRole IDs that grant this skill (computed from AppRoles)",
    )

    # Audit
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = Field(
        None, description="User ID of admin who created this entry"
    )
    updated_by: Optional[str] = Field(
        None, description="User ID of admin who last updated this"
    )

    model_config = {"use_enum_values": True}

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format (mirrors ToolDefinition)."""
        return {
            "PK": f"SKILL#{self.skill_id}",
            "SK": "METADATA",
            # SkillOwnerIndex (GSI4) — provisioned now so a Phase-2 "list my
            # skills" query needs no table migration. v1 admin lists scan by
            # PK begins_with("SKILL#") instead.
            "GSI4PK": f"OWNER#{self.owner_id}",
            "GSI4SK": f"SKILL#{self.skill_id}",
            "skillId": self.skill_id,
            "displayName": self.display_name,
            "description": self.description,
            "instructions": self.instructions,
            "boundToolIds": list(self.bound_tool_ids),
            "compose": list(self.compose),
            # Reference-file manifest (camelCase maps, mirroring the row's
            # convention). The bytes live in S3; this is just pointers.
            "resources": [
                {
                    "filename": r.filename,
                    "contentHash": r.content_hash,
                    "size": r.size,
                    "contentType": r.content_type,
                    "s3Key": r.s3_key,
                }
                for r in self.resources
            ],
            "status": self.status
            if isinstance(self.status, str)
            else self.status.value,
            "category": self.category,
            "ownerId": self.owner_id,
            "visibility": self.visibility
            if isinstance(self.visibility, str)
            else self.visibility.value,
            "createdAt": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "createdBy": self.created_by,
            "updatedBy": self.updated_by,
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "SkillDefinition":
        """Create from DynamoDB item (mirrors ToolDefinition)."""
        created_at = item.get("createdAt")
        updated_at = item.get("updatedAt")
        return cls(
            skill_id=item.get("skillId", ""),
            display_name=item.get("displayName", ""),
            description=item.get("description", ""),
            instructions=item.get("instructions", ""),
            bound_tool_ids=list(item.get("boundToolIds") or []),
            compose=list(item.get("compose") or []),
            resources=[
                SkillResourceRef(
                    filename=r.get("filename", ""),
                    content_hash=r.get("contentHash", ""),
                    # DynamoDB returns numbers as Decimal — coerce to int.
                    size=int(r.get("size", 0)),
                    content_type=r.get("contentType", ""),
                    s3_key=r.get("s3Key", ""),
                )
                for r in (item.get("resources") or [])
            ],
            status=item.get("status", SkillStatus.ACTIVE),
            category=item.get("category"),
            owner_id=item.get("ownerId", "system"),
            visibility=item.get("visibility", SkillVisibility.ADMIN),
            created_at=datetime.fromisoformat(created_at.rstrip("Z"))
            if created_at
            else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z"))
            if updated_at
            else datetime.now(timezone.utc),
            created_by=item.get("createdBy"),
            updated_by=item.get("updatedBy"),
        )


# =============================================================================
# API Request Models (admin) — mirror apis/shared/tools/models.py
# =============================================================================


class SkillCreateRequest(BaseModel):
    """Request body for POST /admin/skills."""

    skill_id: str = Field(..., pattern=SKILL_ID_PATTERN, alias="skillId")
    display_name: str = Field(
        ..., min_length=1, max_length=100, alias="displayName"
    )
    description: str = Field(..., max_length=500)
    # SKILL.md body — uncapped (instructions can be long); empty is allowed so
    # an admin can save a draft and fill it in later.
    instructions: str = Field(default="")
    bound_tool_ids: List[str] = Field(default_factory=list, alias="boundToolIds")
    compose: List[str] = Field(default_factory=list)
    status: SkillStatus = Field(default=SkillStatus.ACTIVE)
    category: Optional[str] = None

    model_config = {"populate_by_name": True}


class SkillUpdateRequest(BaseModel):
    """Request body for PUT /admin/skills/{skill_id}."""

    display_name: Optional[str] = Field(
        None, min_length=1, max_length=100, alias="displayName"
    )
    description: Optional[str] = Field(None, max_length=500)
    instructions: Optional[str] = None
    bound_tool_ids: Optional[List[str]] = Field(None, alias="boundToolIds")
    compose: Optional[List[str]] = None
    status: Optional[SkillStatus] = None
    category: Optional[str] = None

    model_config = {"populate_by_name": True}


class SkillRoleAssignment(BaseModel):
    """Role assignment info for a skill (mirror ToolRoleAssignment)."""

    role_id: str = Field(..., alias="roleId")
    display_name: str = Field(..., alias="displayName")
    grant_type: str = Field(
        ..., alias="grantType", description="'direct' or 'inherited'"
    )
    inherited_from: Optional[str] = Field(None, alias="inheritedFrom")
    enabled: bool

    model_config = {"populate_by_name": True}


class SkillRolesResponse(BaseModel):
    """Response for GET /admin/skills/{skill_id}/roles."""

    skill_id: str = Field(..., alias="skillId")
    roles: List[SkillRoleAssignment]

    model_config = {"populate_by_name": True}


class SetSkillRolesRequest(BaseModel):
    """Request body for PUT /admin/skills/{skill_id}/roles."""

    app_role_ids: List[str] = Field(..., alias="appRoleIds")

    model_config = {"populate_by_name": True}


class AddRemoveSkillRolesRequest(BaseModel):
    """Request body for POST /admin/skills/{skill_id}/roles/add or /remove."""

    app_role_ids: List[str] = Field(..., alias="appRoleIds")

    model_config = {"populate_by_name": True}


# =============================================================================
# API Response Models (admin)
# =============================================================================


class AdminSkillResponse(BaseModel):
    """Response model for admin skill listing (mirror AdminToolResponse)."""

    skill_id: str = Field(..., alias="skillId")
    display_name: str = Field(..., alias="displayName")
    description: str
    instructions: str
    bound_tool_ids: List[str] = Field(default_factory=list, alias="boundToolIds")
    compose: List[str] = Field(default_factory=list)
    resources: List[SkillResourceRef] = Field(default_factory=list)
    status: SkillStatus
    category: Optional[str] = None
    owner_id: str = Field("system", alias="ownerId")
    visibility: SkillVisibility = SkillVisibility.ADMIN
    allowed_app_roles: List[str] = Field(
        default_factory=list, alias="allowedAppRoles"
    )
    created_at: str = Field("", alias="createdAt")
    updated_at: str = Field("", alias="updatedAt")
    created_by: Optional[str] = Field(None, alias="createdBy")
    updated_by: Optional[str] = Field(None, alias="updatedBy")

    model_config = {"populate_by_name": True, "use_enum_values": True}

    @classmethod
    def from_skill_definition(
        cls, skill: "SkillDefinition", allowed_roles: Optional[List[str]] = None
    ) -> "AdminSkillResponse":
        """Create response from a SkillDefinition."""
        return cls(
            skill_id=skill.skill_id,
            display_name=skill.display_name,
            description=skill.description,
            instructions=skill.instructions,
            bound_tool_ids=list(skill.bound_tool_ids),
            compose=list(skill.compose),
            resources=list(skill.resources),
            status=skill.status,
            category=skill.category,
            owner_id=skill.owner_id,
            visibility=skill.visibility,
            allowed_app_roles=allowed_roles or skill.allowed_app_roles,
            created_at=skill.created_at.isoformat() + "Z" if skill.created_at else "",
            updated_at=skill.updated_at.isoformat() + "Z" if skill.updated_at else "",
            created_by=skill.created_by,
            updated_by=skill.updated_by,
        )


class AdminSkillListResponse(BaseModel):
    """Response for GET /admin/skills."""

    skills: List[AdminSkillResponse]
    total: int


class SkillResourcesResponse(BaseModel):
    """Response for the /admin/skills/{skill_id}/resources manifest endpoints.

    Returned by list, upload, and delete so the caller always sees the
    skill's current reference-file manifest after a write (the read-bytes
    endpoint returns the raw file body instead).
    """

    skill_id: str = Field(..., alias="skillId")
    resources: List[SkillResourceRef] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class UserSkillPreference(BaseModel):
    """
    User's explicit per-skill enabled/disabled preferences (mirrors
    ``UserToolPreference``). A skill absent from the map is enabled by
    default — RBAC granted means on unless the user toggled it off.

    Stored per-user in the skills/AppRoles table:
    PK=USER#{user_id}, SK=SKILL_PREFERENCES.
    """

    user_id: str
    skill_preferences: Dict[str, bool] = Field(
        default_factory=dict, description="Map of skill_id -> enabled state"
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format."""
        return {
            "PK": f"USER#{self.user_id}",
            "SK": "SKILL_PREFERENCES",
            "userId": self.user_id,
            "skillPreferences": self.skill_preferences,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "UserSkillPreference":
        """Create from a DynamoDB item."""
        updated_at = item.get("updatedAt")
        return cls(
            user_id=item.get("userId", ""),
            skill_preferences=dict(item.get("skillPreferences", {})),
            updated_at=(
                datetime.fromisoformat(updated_at.rstrip("Z"))
                if updated_at
                else datetime.now(timezone.utc)
            ),
        )
