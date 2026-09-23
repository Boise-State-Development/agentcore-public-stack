"""Request/response models for the ``/projects`` surface (shared-projects §5).

camelCase on the wire via explicit aliases + ``populate_by_name``, matching the
memory-spaces and agents API models. Internal user ids never leave the API: a
project shows its owner by email, and a member row reports only whether that
person has signed in (``hasSignedIn``), which is what transfer depends on.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.projects.models import (
    MemberRole,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectStatus,
)
from apis.shared.projects.service import (
    DESCRIPTION_MAX_LENGTH,
    MAX_EMAILS_PER_REQUEST,
    NAME_MAX_LENGTH,
    AddMembersResult,
)

# ---- requests ----------------------------------------------------------


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=NAME_MAX_LENGTH)
    description: Optional[str] = Field(None, max_length=DESCRIPTION_MAX_LENGTH)


class UpdateProjectRequest(BaseModel):
    """Absent fields are untouched. ``status`` and ``editorsManageMembers`` are owner-only."""

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, min_length=1, max_length=NAME_MAX_LENGTH)
    description: Optional[str] = Field(None, max_length=DESCRIPTION_MAX_LENGTH)
    editors_manage_members: Optional[bool] = Field(None, alias="editorsManageMembers")
    status: Optional[ProjectStatus] = None


class AddMembersRequest(BaseModel):
    """Bulk invite: paste a list of emails, pick one role."""

    emails: List[str] = Field(..., min_length=1, max_length=MAX_EMAILS_PER_REQUEST)
    role: MemberRole = "viewer"


class UpdateMemberRequest(BaseModel):
    role: MemberRole


class TransferOwnershipRequest(BaseModel):
    email: str = Field(..., min_length=3, description="An existing editor of the project")


# ---- responses ---------------------------------------------------------


class ProjectResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., alias="projectId")
    name: str
    description: str
    owner_email: str = Field(..., alias="ownerEmail")
    role: ProjectRole = Field(..., description="The caller's role on this project")
    status: ProjectStatus
    editors_manage_members: bool = Field(..., alias="editorsManageMembers")
    member_count: int = Field(..., alias="memberCount", description="Members besides the owner")
    harness_agent_id: str = Field(..., alias="harnessAgentId")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    @classmethod
    def from_project(cls, project: Project, role: ProjectRole) -> "ProjectResponse":
        return cls(
            project_id=project.project_id,
            name=project.name,
            description=project.description,
            owner_email=project.owner_email,
            role=role,
            status=project.status,
            editors_manage_members=project.settings.editors_manage_members,
            member_count=project.member_count,
            harness_agent_id=project.harness_agent_id,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


class ProjectListResponse(BaseModel):
    projects: List[ProjectResponse]


class MemberResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    email: str
    role: ProjectRole
    has_signed_in: bool = Field(..., alias="hasSignedIn")
    created_at: Optional[str] = Field(None, alias="createdAt")

    @classmethod
    def from_member(cls, member: ProjectMember) -> "MemberResponse":
        return cls(
            email=member.email,
            role=member.role,
            has_signed_in=member.user_id is not None,
            created_at=member.created_at,
        )


class MembersResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    members: List[MemberResponse] = Field(..., description="Owner first, then members by email")
    can_manage: bool = Field(..., alias="canManage", description="Whether the caller may add or remove people")

    @classmethod
    def build(cls, project: Project, role: ProjectRole, members: List[ProjectMember]) -> "MembersResponse":
        owner = MemberResponse(
            email=project.owner_email, role="owner", has_signed_in=True, created_at=project.created_at
        )
        can_manage = project.status == "active" and (
            role == "owner" or (role == "editor" and project.settings.editors_manage_members)
        )
        return cls(members=[owner, *(MemberResponse.from_member(m) for m in members)], can_manage=can_manage)


class AddMembersResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    added: List[MemberResponse]
    already_members: List[str] = Field(..., alias="alreadyMembers")
    invalid: List[str]
    over_capacity: List[str] = Field(
        ..., alias="overCapacity", description="Not added because the project reached its member limit"
    )

    @classmethod
    def from_result(cls, result: AddMembersResult) -> "AddMembersResponse":
        return cls(
            added=[MemberResponse.from_member(m) for m in result.added],
            already_members=result.already_members,
            invalid=result.invalid,
            over_capacity=result.over_capacity,
        )

