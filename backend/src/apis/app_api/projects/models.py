"""Request/response models for the ``/projects`` surface (shared-projects §5).

camelCase on the wire via explicit aliases + ``populate_by_name``, matching the
memory-spaces and agents API models. Internal user ids never leave the API: a
project shows its owner by email, and a member row reports only whether that
person has signed in (``hasSignedIn``), which is what transfer depends on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.app_api.documents.models import (
    DocumentResponse,
    ImportDocumentsResponse,
    KbUsage,
    UploadUrlResponse,
)
from apis.app_api.web_sources.models import StartCrawlResponse
from apis.shared.assistants.models import MAX_AGENT_INSTRUCTIONS_CHARS, AgentModelConfig, VersionFieldChange
from apis.shared.projects.models import (
    MemberRole,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectStatus,
    SharedTask,
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



class SharedTaskResponse(BaseModel):
    """A task a member shared with the project. Opened at ``shareUrl``, forked via ``POST /shares/{shareId}/export``."""

    model_config = ConfigDict(populate_by_name=True)

    share_id: str = Field(..., alias="shareId")
    title: str
    shared_by_email: str = Field(..., alias="sharedByEmail")
    shared_at: str = Field(..., alias="sharedAt")
    share_url: str = Field(..., alias="shareUrl")
    is_mine: bool = Field(..., alias="isMine", description="Whether the caller shared it (and so may revoke it)")

    @classmethod
    def from_pointer(cls, pointer: SharedTask, caller_id: str) -> "SharedTaskResponse":
        return cls(
            share_id=pointer.share_id,
            title=pointer.title,
            shared_by_email=pointer.owner_email,
            shared_at=pointer.shared_at,
            share_url=f"/shared/{pointer.share_id}",
            is_mine=pointer.owner_id == caller_id,
        )


class SharedTasksResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tasks: List[SharedTaskResponse] = Field(..., description="Most recently shared first")


class DirectoryPersonResponse(BaseModel):
    """Someone the caller could invite. ``memberRole`` is set if they are already in the project."""

    model_config = ConfigDict(populate_by_name=True)

    email: str
    name: str
    has_signed_in: bool = Field(..., alias="hasSignedIn")
    member_role: Optional[ProjectRole] = Field(None, alias="memberRole")


class DirectoryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    people: List[DirectoryPersonResponse] = Field(
        ...,
        description="Best match first. A well-formed email nobody has signed in with is returned last, "
        "with hasSignedIn false, so it can always be invited.",
    )


# ---- settings (the harness) -------------------------------------------

INSTRUCTIONS_MAX_LENGTH = MAX_AGENT_INSTRUCTIONS_CHARS
MAX_BINDINGS_PER_KIND = 100


class BindingRef(BaseModel):
    """One tool or skill the project's agent may use. The kind is the route's."""

    ref: str = Field(..., min_length=1, max_length=512)
    config: Dict[str, Any] = Field(default_factory=dict)


class UpdateInstructionsRequest(BaseModel):
    instructions: str = Field(..., max_length=INSTRUCTIONS_MAX_LENGTH)


class UpdateModelRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model_settings: AgentModelConfig = Field(..., alias="modelConfig")


class UpdateBindingsRequest(BaseModel):
    """The complete list for the route's kind; bindings of other kinds are kept."""

    bindings: List[BindingRef] = Field(..., max_length=MAX_BINDINGS_PER_KIND)


class _SettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: Optional[int] = Field(None, description="Current version, or null before the first save")
    can_edit: bool = Field(..., alias="canEdit")


class InstructionsResponse(_SettingsResponse):
    instructions: str


class ModelResponse(_SettingsResponse):
    model_settings: Optional[AgentModelConfig] = Field(
        None, alias="modelConfig", description="Null: each member's own default model"
    )


class BindingsResponse(_SettingsResponse):
    bindings: List[BindingRef]


class SettingsVersionSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: int
    created_at: Optional[str] = Field(None, alias="createdAt")
    created_by_email: Optional[str] = Field(
        None, alias="createdByEmail", description="Null for the state the project was created with"
    )
    changes: List[str] = Field(..., description="Fields changed from the previous version")


class SettingsVersionsResponse(BaseModel):
    versions: List[SettingsVersionSummary] = Field(..., description="Newest first")


class SettingsVersionResponse(SettingsVersionSummary):
    """One version in full, with what changed from the version before it."""

    instructions: str
    model_settings: Optional[AgentModelConfig] = Field(None, alias="modelConfig")
    tools: List[BindingRef]
    skills: List[BindingRef]
    field_changes: List[VersionFieldChange] = Field(..., alias="fieldChanges")
    instructions_diff: List[str] = Field(..., alias="instructionsDiff")


# ---- knowledge (the harness's documents) ------------------------------


class ProjectDocumentResponse(DocumentResponse):
    """A document in the project's Files, with who added it."""

    added_by_email: Optional[str] = Field(
        None, alias="addedByEmail", description="Null if unknown: added before this was recorded, or by a former member"
    )


class ProjectDocumentsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    documents: List[ProjectDocumentResponse]
    next_token: Optional[str] = Field(None, alias="nextToken")
    kb_usage: Optional[KbUsage] = Field(None, alias="kbUsage")
    can_edit: bool = Field(..., alias="canEdit")


class SharingNotice(BaseModel):
    """What adding a file to a project means, said once, the same way to every client."""

    model_config = ConfigDict(populate_by_name=True)

    notice: str = Field(..., description="Shown when a member adds files: everyone in the project can read them")


class ProjectUploadUrlResponse(UploadUrlResponse, SharingNotice):
    pass


class ProjectImportResponse(ImportDocumentsResponse, SharingNotice):
    pass


class ProjectStartCrawlResponse(StartCrawlResponse, SharingNotice):
    pass
