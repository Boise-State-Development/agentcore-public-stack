"""Shared Projects domain models (docs/specs/shared-projects.md §3.1).

A Project is a composition: its harness (instructions, model, tools, knowledge)
is a hidden Agent record in ``rag-assistants`` with ``kind="project"``, and its
memory is a Memory Space (Phase 2). These models cover only what the
``{prefix}-projects`` table itself holds: the project's META row and its
email-keyed ``MEMBER#`` rows.

Ownership lives on META (``owner_id``), never as a member row — the same shape
as agents and memory spaces — so ``ProjectRole`` has three values while a
stored member can only be an editor or a viewer.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ProjectRole = Literal["owner", "editor", "viewer"]
MemberRole = Literal["editor", "viewer"]
ProjectStatus = Literal["active", "archived"]
ProjectVisibility = Literal["private", "org"]

ROLE_RANK: dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}


def normalize_email(email: str) -> str:
    """The one spelling of an email this package stores or compares."""
    return email.strip().lower()


class ProjectSettings(BaseModel):
    """Per-project switches. Stored as a map on META."""

    model_config = ConfigDict(populate_by_name=True)

    editors_manage_members: bool = Field(True, alias="editorsManageMembers")


class Project(BaseModel):
    """The ``PROJECT#{id}`` / ``META`` row."""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., alias="projectId")
    name: str
    description: str = ""
    owner_id: str = Field(..., alias="ownerId")
    owner_email: str = Field(..., alias="ownerEmail")
    harness_agent_id: str = Field(..., alias="harnessAgentId")
    # Created by Phase 2.4, when space permissions become project-aware.
    shared_space_id: Optional[str] = Field(None, alias="sharedSpaceId")
    visibility: ProjectVisibility = "private"
    status: ProjectStatus = "active"
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    member_count: int = Field(0, alias="memberCount")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    # Bumped by every META mutation, including the member transactions that
    # maintain ``member_count``, so a stale read can never overwrite either.
    version: int = 1


class ProjectMember(BaseModel):
    """A ``PROJECT#{id}`` / ``MEMBER#{email}`` row."""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., alias="projectId")
    email: str
    role: MemberRole
    # Unknown until the invitee first resolves a permission on the project
    # (membership is email-keyed, so people who have never signed in can be
    # added). Back-filled then; required for transfer and cost attribution.
    user_id: Optional[str] = Field(None, alias="userId")
    invited_by: str = Field(..., alias="invitedBy")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
