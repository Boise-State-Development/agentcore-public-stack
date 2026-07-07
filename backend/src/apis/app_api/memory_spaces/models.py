"""Request/response models for the Memory Spaces user surface (A2).

The SPA consumes camelCase; FastAPI serializes by alias, so responses declare
camelCase aliases with ``populate_by_name`` for snake_case construction —
matching the assistants/schedules API models.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.memory.models import (
    EntryType,
    MemoryEntryRef,
    MemorySpace,
    Role,
    ShareRole,
    SpaceMember,
)
from apis.shared.memory.templates import TEMPLATES, SpaceTemplate


# ---- requests ----------------------------------------------------------


class CreateSpaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    template: str = Field("blank")


class UpsertEntryRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    body: str = Field(..., description="The entry's markdown content")
    entry_type: EntryType = Field("fact", alias="type")
    description: str = Field("")
    indexed: Dict[str, Any] = Field(default_factory=dict)


class UpdateIndexRequest(BaseModel):
    content: str = Field(..., description="The MEMORY.md index text")


class ShareRequest(BaseModel):
    email: str = Field(..., min_length=1, description="Grantee email")
    permission: ShareRole = Field("viewer", description="viewer | editor")


class UpdateShareRequest(BaseModel):
    permission: ShareRole = Field(..., description="viewer | editor")


# ---- responses ---------------------------------------------------------


class TemplateResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    template_id: str = Field(..., alias="templateId")
    name: str
    description: str

    @classmethod
    def from_template(cls, t: SpaceTemplate) -> "TemplateResponse":
        return cls(template_id=t.template_id, name=t.name, description=t.description)


class SpaceSummaryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    space_id: str = Field(..., alias="spaceId")
    name: str
    template: str
    role: Role
    owner_id: str = Field(..., alias="ownerId")
    created_at: str = Field("", alias="createdAt")
    updated_at: str = Field("", alias="updatedAt")

    @classmethod
    def from_space(cls, space: MemorySpace, role: Role) -> "SpaceSummaryResponse":
        return cls(
            space_id=space.space_id,
            name=space.name,
            template=space.template,
            role=role,
            owner_id=space.owner_id,
            created_at=space.created_at,
            updated_at=space.updated_at,
        )


class SpacesListResponse(BaseModel):
    spaces: List[SpaceSummaryResponse]
    templates: List[TemplateResponse] = Field(default_factory=list)


class EntryRefResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: str
    entry_type: EntryType = Field("fact", alias="type")
    description: str = ""
    size: int = 0
    updated: str = ""
    updated_by: str = Field("", alias="updatedBy")
    indexed: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_ref(cls, r: MemoryEntryRef) -> "EntryRefResponse":
        return cls(
            slug=r.slug,
            entry_type=r.entry_type,
            description=r.description,
            size=r.size,
            updated=r.updated,
            updated_by=r.updated_by,
            indexed=r.indexed,
        )


class SpaceDetailResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    space_id: str = Field(..., alias="spaceId")
    name: str
    template: str
    role: Role
    owner_id: str = Field(..., alias="ownerId")
    created_at: str = Field("", alias="createdAt")
    updated_at: str = Field("", alias="updatedAt")
    index: str = Field("", description="The MEMORY.md index text")
    entries: List[EntryRefResponse] = Field(default_factory=list)


class EntryContentResponse(BaseModel):
    slug: str
    content: str


class IndexContentResponse(BaseModel):
    content: str


class EntriesListResponse(BaseModel):
    entries: List[EntryRefResponse]


class MemberResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    email: str
    permission: ShareRole = "viewer"
    created_at: str = Field("", alias="createdAt")

    @classmethod
    def from_member(cls, m: SpaceMember) -> "MemberResponse":
        return cls(email=m.email, permission=m.permission, created_at=m.created_at)


class MembersListResponse(BaseModel):
    members: List[MemberResponse]


def all_templates() -> List[TemplateResponse]:
    return [TemplateResponse.from_template(t) for t in TEMPLATES.values()]
