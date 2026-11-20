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

from pydantic import BaseModel, Field, field_validator, model_validator

from apis.shared.skills.skill_md import (
    ALLOWED_TOOLS_MAX_NAMES,
    skill_metadata_problem,
    unsafe_character_problem,
)
from apis.shared.timestamps import from_iso, to_iso


# Regex for a skill_id — identical shape to tool_id (see ToolCreateRequest).
SKILL_ID_PATTERN = r"^[a-z][a-z0-9_]{2,49}$"

# Max length of a skill ``description``. This is the Agent Skills spec limit for
# the SKILL.md frontmatter ``description`` field, so a skill authored for Claude
# imports here unchanged. It is a real bound, not a formality: the description is
# the Level-1 catalog line injected into the cacheable system prompt for every
# enabled skill, on every turn.
SKILL_DESCRIPTION_MAX_LENGTH = 1024

# Max size of a skill's ``instructions`` (the SKILL.md body), in UTF-8 bytes, on
# every write path. The body shares a 400 KB DynamoDB item with the manifest.
SKILL_INSTRUCTIONS_MAX_BYTES = 200 * 1024

# ``owner_id`` of an admin-catalog skill. Anything else is a user-authored
# skill (Skills v2 PR-3): governed by ownership, not by RBAC ``granted_skills``.
SYSTEM_OWNER_ID = "system"


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
        description="Object key in the skill-resources bucket, in the standard "
        "agentskills.io layout (skills/{skill_id}/{references|scripts|assets}/{path})",
    )
    # Skills v2: which agentskills.io bundle directory the file belongs to.
    # ``script`` files are accept-and-inert (stored + listed, never executed).
    # Old manifest rows (v1, content-hash keyed) default to ``reference``.
    kind: str = Field(
        default="reference",
        description="Bundle directory: reference | script | asset",
    )
    # Where the file sits below its kind directory, '/'-separated, at most 4
    # segments (``resource_paths``): ``forms.md`` or ``legal/nda.md``. Rows
    # written before nested paths have none stored and read back with
    # ``path == filename``, so they need no migration; ``filename`` stays the
    # basename. Unique within a manifest across kinds.
    path: str = Field(
        default="",
        description="Path below the kind directory, e.g. 'legal/nda.md'",
    )

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _default_path(self) -> "SkillResourceRef":
        if not self.path:
            self.path = self.filename
        elif not self.filename:
            self.filename = self.path.rsplit("/", 1)[-1]
        return self

    def to_item(self) -> Dict:
        """The manifest entry as stored on a row (camelCase).

        ``path`` is written only when it differs from ``filename``, so an entry
        at the top of its directory is stored exactly as it was before nested
        paths existed.
        """
        item = {
            "filename": self.filename,
            "contentHash": self.content_hash,
            "size": self.size,
            "contentType": self.content_type,
            "s3Key": self.s3_key,
            "kind": self.kind,
        }
        if self.path != self.filename:
            item["path"] = self.path
        return item

    @classmethod
    def from_item(cls, item: Dict) -> "SkillResourceRef":
        """Inverse of :meth:`to_item`; a stored entry with no ``path`` is a top-level file."""
        return cls(
            filename=item.get("filename", ""),
            content_hash=item.get("contentHash", ""),
            # DynamoDB returns numbers as Decimal — coerce to int.
            size=int(item.get("size", 0)),
            content_type=item.get("contentType", ""),
            s3_key=item.get("s3Key", ""),
            # Old rows predate kind → default to reference.
            kind=item.get("kind", "reference"),
            path=item.get("path") or "",
        )


class SkillImportSource(BaseModel):
    """Where a user skill was last imported from (docs/specs/skill-bundle-import.md §3).

    Display and refresh metadata on the live row only. It is deliberately not a
    version snapshot field (``versions.SNAPSHOT_FIELDS``), so re-importing never
    changes what a pin froze or the content hash a pin compares (spec D5).
    """

    type: str = Field(..., description="zip | github")
    path: str = Field(default="", description="Bundle directory inside the archive")
    bundle_hash: str = Field(..., alias="bundleHash")
    imported_at: str = Field(..., alias="importedAt")
    url: Optional[str] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    ref: Optional[str] = None
    commit_sha: Optional[str] = Field(None, alias="commitSha")
    filename: Optional[str] = None
    # The frontmatter ``name`` as the bundle wrote it. The stored activation
    # name is derived from ``skill_id`` and can differ (a suffix on collision).
    original_name: Optional[str] = Field(None, alias="originalName")

    model_config = {"populate_by_name": True}

    def to_item(self) -> dict:
        return {k: v for k, v in self.model_dump(by_alias=True).items() if v is not None}

    def provenance(self) -> dict:
        """Where it came from, for display and audit: no URL, path or hash.

        ``{"type": "github", "owner", "repo", "ref", "commitSha"}`` or
        ``{"type": "zip"}``. The SPA renders "Imported from owner/repo @ sha7" or
        "Imported from a zip file" from it, as text.
        """
        fields = {
            "type": self.type,
            "owner": self.owner,
            "repo": self.repo,
            "ref": self.ref,
            "commitSha": self.commit_sha,
        }
        return {k: v for k, v in fields.items() if v}


def skill_provenance(skill: object) -> Optional[dict]:
    """``SkillImportSource.provenance()`` for an imported skill, else None.

    Takes any object with an ``import_source`` (or none), so a response builder
    can call it on whatever record shape it holds.
    """
    source = getattr(skill, "import_source", None)
    return source.provenance() if source is not None else None


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

    compose: List[str] = Field(
        default_factory=list,
        description="skill_ids composed into this skill (composite skills)",
    )

    # Skills v2 (agentskills.io frontmatter passthrough). ``allowed_tools`` is
    # ADVISORY ONLY — Strands parses it and marks it "Experimental: not yet
    # enforced"; the platform never grants, mounts, or folds a tool because a
    # skill names it (spec D1/D4). ``skill_metadata`` carries the remaining
    # frontmatter (license, compatibility, arbitrary keys) round-trip-faithfully.
    allowed_tools: List[str] = Field(
        default_factory=list,
        description="Advisory tool names from SKILL.md frontmatter; never enforced (D4)",
    )
    skill_metadata: dict = Field(
        default_factory=dict,
        description="agentskills.io frontmatter passthrough (license, compatibility, ...)",
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

    # Last bundle import (user skills only). Not a snapshot field, so a re-import
    # never moves a pinned version (skill-bundle-import.md D5).
    import_source: Optional[SkillImportSource] = Field(default=None)

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
            "compose": list(self.compose),
            "allowedTools": list(self.allowed_tools),
            "skillMetadata": dict(self.skill_metadata),
            # Reference-file manifest (camelCase maps, mirroring the row's
            # convention). The bytes live in S3; this is just pointers.
            "resources": [r.to_item() for r in self.resources],
            "status": self.status
            if isinstance(self.status, str)
            else self.status.value,
            "category": self.category,
            "ownerId": self.owner_id,
            "visibility": self.visibility
            if isinstance(self.visibility, str)
            else self.visibility.value,
            "createdAt": to_iso(self.created_at) if self.created_at else None,
            "updatedAt": to_iso(self.updated_at) if self.updated_at else None,
            "createdBy": self.created_by,
            "updatedBy": self.updated_by,
            **(
                {"importSource": self.import_source.to_item()}
                if self.import_source
                else {}
            ),
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
            compose=list(item.get("compose") or []),
            allowed_tools=list(item.get("allowedTools") or []),
            skill_metadata=dict(item.get("skillMetadata") or {}),
            resources=[
                SkillResourceRef.from_item(r) for r in (item.get("resources") or [])
            ],
            status=item.get("status", SkillStatus.ACTIVE),
            category=item.get("category"),
            owner_id=item.get("ownerId", "system"),
            visibility=item.get("visibility", SkillVisibility.ADMIN),
            created_at=from_iso(created_at)
            if created_at
            else datetime.now(timezone.utc),
            updated_at=from_iso(updated_at)
            if updated_at
            else datetime.now(timezone.utc),
            created_by=item.get("createdBy"),
            updated_by=item.get("updatedBy"),
            import_source=SkillImportSource.model_validate(item["importSource"])
            if item.get("importSource")
            else None,
        )


# =============================================================================
# Field checks shared by every write path
# =============================================================================


def instructions_size_problem(instructions: Optional[str]) -> Optional[str]:
    """A sentence saying ``instructions`` is over the size cap, or None."""
    if instructions is None:
        return None
    size = len(instructions.encode("utf-8"))
    if size <= SKILL_INSTRUCTIONS_MAX_BYTES:
        return None
    return (
        f"the instructions are {size:,} bytes; the limit is "
        f"{SKILL_INSTRUCTIONS_MAX_BYTES // 1024} KiB. Move long detail into reference files"
    )


def check_text_field(label: str, value: Optional[str]) -> Optional[str]:
    """Pydantic-validator body: refuse unsafe characters in a display field."""
    problem = unsafe_character_problem(label, value)
    if problem:
        raise ValueError(problem)
    return value


def check_instructions(value: Optional[str]) -> Optional[str]:
    """Pydantic-validator body: refuse instructions over the size cap."""
    problem = instructions_size_problem(value)
    if problem:
        raise ValueError(problem[0].upper() + problem[1:])
    return value


def check_skill_metadata(value: Optional[dict]) -> Optional[dict]:
    """Pydantic-validator body: refuse ``skillMetadata`` the lenient validator rejects.

    Errors refuse (a non-string ``license``, a ``compatibility`` over 500
    characters, a non-string ``metadata`` value); warnings pass, because an
    unknown key is kept and written under ``metadata`` in the projection.
    """
    problem = skill_metadata_problem(value)
    if problem:
        raise ValueError(f"skillMetadata: {problem}")
    return value


def check_allowed_tools(value: Optional[List[str]]) -> Optional[List[str]]:
    """Pydantic-validator body: refuse an ``allowedTools`` list over the specification's cap."""
    if value is None:
        return None
    names = [name for item in value for name in item.split()]
    if len(names) > ALLOWED_TOOLS_MAX_NAMES:
        raise ValueError(
            f"allowedTools names {len(names)} tools; the limit is {ALLOWED_TOOLS_MAX_NAMES}"
        )
    return names


def check_description_not_blank(value: Optional[str]) -> Optional[str]:
    """Pydantic-validator body: refuse a blank description when one is given."""
    if value is not None and not value.strip():
        raise ValueError(
            "The description cannot be blank; it is the line the agent reads when "
            "deciding whether to use the skill"
        )
    return value


# =============================================================================
# API Request Models (admin) — mirror apis/shared/tools/models.py
# =============================================================================


class SkillCreateRequest(BaseModel):
    """Request body for POST /admin/skills."""

    skill_id: str = Field(..., pattern=SKILL_ID_PATTERN, alias="skillId")
    display_name: str = Field(
        ..., min_length=1, max_length=100, alias="displayName"
    )
    description: str = Field(..., max_length=SKILL_DESCRIPTION_MAX_LENGTH)
    # SKILL.md body, capped at SKILL_INSTRUCTIONS_MAX_BYTES; empty is allowed so
    # an admin can save a draft and fill it in later.
    instructions: str = Field(default="")
    compose: List[str] = Field(default_factory=list)
    # Advisory only (D4); written to SKILL.md as one space-separated string.
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    # license, compatibility, metadata and any other frontmatter fields.
    skill_metadata: Dict = Field(default_factory=dict, alias="skillMetadata")
    status: SkillStatus = Field(default=SkillStatus.ACTIVE)
    category: Optional[str] = None

    model_config = {"populate_by_name": True}

    @field_validator("allowed_tools")
    @classmethod
    def _allowed_tools_valid(cls, value: List[str]) -> List[str]:
        return check_allowed_tools(value)

    @field_validator("skill_metadata")
    @classmethod
    def _skill_metadata_valid(cls, value: Dict) -> Dict:
        return check_skill_metadata(value)

    @field_validator("display_name")
    @classmethod
    def _display_name_safe(cls, value: str) -> str:
        return check_text_field("The name", value)

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str) -> str:
        check_description_not_blank(value)
        return check_text_field("The description", value)

    @field_validator("instructions")
    @classmethod
    def _instructions_size(cls, value: str) -> str:
        return check_instructions(value)


class SkillUpdateRequest(BaseModel):
    """Request body for PUT /admin/skills/{skill_id}."""

    display_name: Optional[str] = Field(
        None, min_length=1, max_length=100, alias="displayName"
    )
    description: Optional[str] = Field(
        None, max_length=SKILL_DESCRIPTION_MAX_LENGTH
    )
    instructions: Optional[str] = None
    compose: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = Field(None, alias="allowedTools")
    skill_metadata: Optional[Dict] = Field(None, alias="skillMetadata")
    status: Optional[SkillStatus] = None
    category: Optional[str] = None

    model_config = {"populate_by_name": True}

    @field_validator("allowed_tools")
    @classmethod
    def _allowed_tools_valid(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return check_allowed_tools(value)

    @field_validator("skill_metadata")
    @classmethod
    def _skill_metadata_valid(cls, value: Optional[Dict]) -> Optional[Dict]:
        return check_skill_metadata(value)

    @field_validator("display_name")
    @classmethod
    def _display_name_safe(cls, value: Optional[str]) -> Optional[str]:
        return check_text_field("The name", value)

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: Optional[str]) -> Optional[str]:
        check_description_not_blank(value)
        return check_text_field("The description", value)

    @field_validator("instructions")
    @classmethod
    def _instructions_size(cls, value: Optional[str]) -> Optional[str]:
        return check_instructions(value)


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
    compose: List[str] = Field(default_factory=list)
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    skill_metadata: Dict = Field(default_factory=dict, alias="skillMetadata")
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
            compose=list(skill.compose),
            allowed_tools=list(skill.allowed_tools),
            skill_metadata=dict(skill.skill_metadata),
            resources=list(skill.resources),
            status=skill.status,
            category=skill.category,
            owner_id=skill.owner_id,
            visibility=skill.visibility,
            allowed_app_roles=allowed_roles or skill.allowed_app_roles,
            created_at=to_iso(skill.created_at) if skill.created_at else "",
            updated_at=to_iso(skill.updated_at) if skill.updated_at else "",
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
                from_iso(updated_at)
                if updated_at
                else datetime.now(timezone.utc)
            ),
        )
