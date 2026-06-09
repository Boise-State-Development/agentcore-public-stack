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
from typing import List, Optional

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
