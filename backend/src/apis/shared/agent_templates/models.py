"""Models for admin-managed agent templates.

Admins curate a small catalog of templates; any signed-in user reads the
enabled ones through the public ``GET /templates`` endpoint and starts a new
agent from one. The catalog is data, not code — the hardcoded client
``agent-templates.ts`` set is the seed source, not the source of truth.

Storage uses a single table with PK ``TEMPLATE#<template_id>``, SK
``METADATA``. No GSI: the catalog is small (tens of items), so a full Scan is
appropriate — this matches the ``system_prompts`` catalog it is modelled on.

Two wire shapes are deliberately different:

* **Admin** responses are snake_case and carry the whole record (including
  ``enabled``, ``sort_order`` and audit fields), matching the ``system_prompts``
  admin convention.
* **Public** responses are camelCase and project only the ``TemplateDraft``
  subset the client already consumes (``{ draft, pitch }[]``), so the existing
  picker / reconcile / form-population path is unchanged when it switches its
  data source from the hardcoded array to this endpoint.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from apis.shared.timestamps import utc_now_iso

# Bounds mirror the create-agent form's own field limits closely enough to keep
# a template Save-able without surprises.
MAX_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 1_024
MAX_INSTRUCTIONS_LENGTH = 100_000
MAX_PITCH_LENGTH = 200
MAX_STARTERS = 12
MAX_TAGS = 16
MAX_BINDINGS = 64

TemplateStatus = Literal["enabled", "disabled"]

# BindingKind mirrors the frontend union
# ('knowledge_base' | 'tool' | 'skill' | 'memory_space'). Stored as a free
# string so a fork can add binding kinds without a backend change; unknown
# kinds pass through and the client reconciles/ignores what it cannot bind.


def _utc_now() -> str:
    return utc_now_iso()


@dataclass
class TemplateModelConfig:
    """A model selection on a template. ``model_id=None`` ⇒ platform default."""

    model_id: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TemplateBinding:
    """One binding on a template — same shape as an ``AgentBinding``."""

    kind: str
    ref: str
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTemplate:
    """Admin-managed agent template stored in DynamoDB."""

    template_id: str
    name: str
    description: str
    emoji: str
    instructions: str
    tags: List[str]
    starters: List[str]
    model_config_: TemplateModelConfig
    bindings: List[TemplateBinding]
    # ── catalog management (never part of the public client draft) ──
    pitch: str
    status: TemplateStatus
    sort_order: int
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    updated_by: Optional[str] = None

    def to_dynamo_item(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "PK": f"TEMPLATE#{self.template_id}",
            "SK": "METADATA",
            "templateId": self.template_id,
            "name": self.name,
            "description": self.description,
            "emoji": self.emoji,
            "instructions": self.instructions,
            "tags": list(self.tags),
            "starters": list(self.starters),
            "modelConfig": {
                "modelId": self.model_config_.model_id,
                "params": self.model_config_.params or {},
            },
            "bindings": [
                {"kind": b.kind, "ref": b.ref, "config": b.config or {}}
                for b in self.bindings
            ],
            "pitch": self.pitch,
            "status": self.status,
            "sortOrder": self.sort_order,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.created_by:
            item["createdBy"] = self.created_by
        if self.updated_by:
            item["updatedBy"] = self.updated_by
        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "AgentTemplate":
        try:
            created_at = item["createdAt"]
            updated_at = item["updatedAt"]
        except KeyError as e:
            raise ValueError(
                f"Agent template item {item.get('PK', '?')} is missing required "
                f"timestamp field: {e.args[0]}"
            ) from e

        status = item.get("status", "enabled")
        if status not in ("enabled", "disabled"):
            # Defensive: an unknown status is hidden from users, never shown.
            status = "disabled"

        mc = item.get("modelConfig") or {}
        model_config = TemplateModelConfig(
            model_id=mc.get("modelId"),
            params=dict(mc.get("params") or {}),
        )
        bindings = [
            TemplateBinding(
                kind=b.get("kind", ""),
                ref=b.get("ref", ""),
                config=dict(b.get("config") or {}),
            )
            for b in (item.get("bindings") or [])
        ]

        # sortOrder may arrive as a Decimal from DynamoDB.
        raw_sort = item.get("sortOrder", 0)
        try:
            sort_order = int(raw_sort)
        except (TypeError, ValueError):
            sort_order = 0

        return cls(
            template_id=item["templateId"],
            name=item["name"],
            description=item.get("description", ""),
            emoji=item.get("emoji", ""),
            instructions=item.get("instructions", ""),
            tags=list(item.get("tags") or []),
            starters=list(item.get("starters") or []),
            model_config_=model_config,
            bindings=bindings,
            pitch=item.get("pitch", ""),
            status=status,
            sort_order=sort_order,
            created_at=created_at,
            updated_at=updated_at,
            created_by=item.get("createdBy"),
            updated_by=item.get("updatedBy"),
        )


# =============================================================================
# Shared request sub-models
# =============================================================================


class ModelConfigPayload(BaseModel):
    """Wire model config. ``model_id=None`` ⇒ platform default.

    Serializes camelCase (``modelId``) to match the client ``TemplateDraft``.
    ``protected_namespaces=()`` silences Pydantic's ``model_``-prefix warning.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        protected_namespaces=(),
    )

    model_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class BindingPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: str = Field(..., min_length=1, max_length=64)
    ref: str = Field(..., min_length=1, max_length=256)
    config: Dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# Admin request/response models (snake_case wire)
# =============================================================================


class AgentTemplateCreate(BaseModel):
    """Create payload. ``template_id`` optional — slugified from name if absent."""

    template_id: Optional[str] = Field(None, min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH)
    description: str = Field("", max_length=MAX_DESCRIPTION_LENGTH)
    emoji: str = Field("", max_length=16)
    instructions: str = Field("", max_length=MAX_INSTRUCTIONS_LENGTH)
    tags: List[str] = Field(default_factory=list, max_length=MAX_TAGS)
    starters: List[str] = Field(default_factory=list, max_length=MAX_STARTERS)
    model_cfg: ModelConfigPayload = Field(
        default_factory=ModelConfigPayload, alias="modelConfig"
    )
    bindings: List[BindingPayload] = Field(default_factory=list, max_length=MAX_BINDINGS)
    pitch: str = Field("", max_length=MAX_PITCH_LENGTH)
    status: TemplateStatus = "enabled"
    sort_order: int = 0

    # Accept both the alias (``modelConfig``) and the field name on input;
    # ``protected_namespaces=()`` avoids Pydantic's ``model_``-prefix warning.
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())


class AgentTemplateUpdate(BaseModel):
    """Partial update — every field optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: Optional[str] = Field(None, max_length=MAX_DESCRIPTION_LENGTH)
    emoji: Optional[str] = Field(None, max_length=16)
    instructions: Optional[str] = Field(None, max_length=MAX_INSTRUCTIONS_LENGTH)
    tags: Optional[List[str]] = Field(None, max_length=MAX_TAGS)
    starters: Optional[List[str]] = Field(None, max_length=MAX_STARTERS)
    model_cfg: Optional[ModelConfigPayload] = Field(None, alias="modelConfig")
    bindings: Optional[List[BindingPayload]] = Field(None, max_length=MAX_BINDINGS)
    pitch: Optional[str] = Field(None, max_length=MAX_PITCH_LENGTH)
    status: Optional[TemplateStatus] = None
    sort_order: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())


class AgentTemplateAdminResponse(BaseModel):
    """Full admin response (snake_case)."""

    template_id: str
    name: str
    description: str
    emoji: str
    instructions: str
    tags: List[str]
    starters: List[str]
    model_cfg: ModelConfigPayload = Field(serialization_alias="modelConfig")
    bindings: List[BindingPayload]
    pitch: str
    status: TemplateStatus
    sort_order: int
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    updated_by: Optional[str] = None

    model_config = ConfigDict(protected_namespaces=())

    @classmethod
    def from_template(cls, t: AgentTemplate) -> "AgentTemplateAdminResponse":
        return cls(
            template_id=t.template_id,
            name=t.name,
            description=t.description,
            emoji=t.emoji,
            instructions=t.instructions,
            tags=t.tags,
            starters=t.starters,
            model_cfg=ModelConfigPayload(
                model_id=t.model_config_.model_id, params=t.model_config_.params
            ),
            bindings=[
                BindingPayload(kind=b.kind, ref=b.ref, config=b.config)
                for b in t.bindings
            ],
            pitch=t.pitch,
            status=t.status,
            sort_order=t.sort_order,
            created_at=t.created_at,
            updated_at=t.updated_at,
            created_by=t.created_by,
            updated_by=t.updated_by,
        )


class AgentTemplateAdminListResponse(BaseModel):
    templates: List[AgentTemplateAdminResponse]
    total: int


# =============================================================================
# Public response models (camelCase — matches the client TemplateDraft)
# =============================================================================


class TemplateDraftResponse(BaseModel):
    """The client ``TemplateDraft`` shape, camelCase, consumed by the picker.

    Only the subset the create-agent form reads — no catalog/audit fields.
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    template_id: str
    name: str
    description: str
    emoji: str
    instructions: str
    tags: List[str]
    starters: List[str]
    # Serialized as "modelConfig" via the camel alias generator.
    model_cfg: ModelConfigPayload = Field(serialization_alias="modelConfig")
    bindings: List[BindingPayload]


class TemplateCatalogEntryResponse(BaseModel):
    """One picker entry: ``{ draft, pitch }`` — matches ``TemplateCatalogEntry``."""

    draft: TemplateDraftResponse
    pitch: str

    @classmethod
    def from_template(cls, t: AgentTemplate) -> "TemplateCatalogEntryResponse":
        return cls(
            draft=TemplateDraftResponse(
                template_id=t.template_id,
                name=t.name,
                description=t.description,
                emoji=t.emoji,
                instructions=t.instructions,
                tags=t.tags,
                starters=t.starters,
                model_cfg=ModelConfigPayload(
                    model_id=t.model_config_.model_id, params=t.model_config_.params
                ),
                bindings=[
                    BindingPayload(kind=b.kind, ref=b.ref, config=b.config)
                    for b in t.bindings
                ],
            ),
            pitch=t.pitch,
        )


class PublicTemplateListResponse(BaseModel):
    templates: List[TemplateCatalogEntryResponse]
    total: int
