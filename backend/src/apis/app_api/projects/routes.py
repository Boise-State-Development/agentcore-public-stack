"""``/projects`` — Shared Projects CRUD, members, transfer and leave (shared-projects §5, PR-1.2),
the people directory for inviting (PR-1.3), settings with version history (PR-1.5a), and the
caller's own and shared tasks (PR-1.6).

Every route authenticates by session cookie first and only then applies the
``PROJECTS_ENABLED`` kill switch, so an unauthenticated caller always sees 401
(the auth sweep requires it) and a signed-in caller sees 404 while the feature
is off. Authorization is entirely the service's: each handler is a thin
translation from ``ProjectError`` subclasses to status codes.

Deleting is two deliberate steps: ``PATCH {"status": "archived"}``, then
``DELETE``, which purges the project, its harness Agent and the harness's
documents. ``DELETE`` on an active project is a 409 naming the first step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from apis.app_api.agent_designer.services.binding_validation import BindingValidationError
from apis.shared.assistants.models import AgentBinding, VersionFieldChange
from apis.shared.assistants.version_diff import wire_field_name, wire_value
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.directory import display_names, get_directory
from apis.shared.feature_flags import projects_enabled
from apis.shared.projects.models import Project, ProjectRole, normalize_email
from apis.shared.projects.service import (
    ProjectConflictError,
    ProjectError,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectService,
    is_valid_email,
)
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.sessions.metadata import list_project_sessions
from apis.shared.sessions.models import SessionMetadataResponse, SessionsListResponse

from .harness_gateway import AppApiHarnessGateway
from .harness_settings import (
    SKILL,
    TOOL,
    HarnessSettingsService,
    HarnessView,
    created_by_email,
    settings_changes,
    skill_pins,
    version_instructions_diff,
)
from .models import (
    AddMembersRequest,
    AddMembersResponse,
    BindingRef,
    BindingsResponse,
    CreateProjectRequest,
    DirectoryPersonResponse,
    DirectoryResponse,
    InstructionsResponse,
    MemberResponse,
    MembersResponse,
    ModelResponse,
    PersonalSpaceResponse,
    MemoryLimitsResponse,
    ProjectMemoryResponse,
    ProjectListResponse,
    ProjectResponse,
    SettingsVersionResponse,
    SettingsVersionSummary,
    SettingsVersionsResponse,
    SharedTaskResponse,
    SharedTasksResponse,
    SkillBindingRef,
    SkillBindingsResponse,
    TransferOwnershipRequest,
    UpdateBindingsRequest,
    UpdateInstructionsRequest,
    UpdateMemberRequest,
    UpdateModelRequest,
    UpdateProjectRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])

_service: Optional[ProjectService] = None


def _svc() -> ProjectService:
    global _service
    if _service is None:
        _service = ProjectService(harness=AppApiHarnessGateway())
    return _service


def _settings() -> HarnessSettingsService:
    return HarnessSettingsService(_svc())


async def require_projects_user(user: User = Depends(get_current_user_from_session)) -> User:
    """Cookie auth, then the environment kill switch (404 while off)."""
    if not projects_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


def _translate(e: ProjectError) -> HTTPException:
    if isinstance(e, ProjectNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    if isinstance(e, ProjectPermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    if isinstance(e, ProjectConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


def _project_response(project: Project, role: ProjectRole) -> ProjectResponse:
    """Blocking (a directory lookup for the owner's name): from async code, run it in a thread."""
    return ProjectResponse.from_project(project, role, display_names([project.owner_email]))


def _emails_in(value: Any) -> Iterable[str]:
    """Every ``email``/``ownerEmail`` value in an audit record's detail."""
    if isinstance(value, dict):
        for key in ("email", "ownerEmail"):
            if isinstance(value.get(key), str):
                yield value[key]


# ---- projects ----------------------------------------------------------


@router.get("", response_model=ProjectListResponse, response_model_by_alias=True)
def list_projects(
    include_archived: bool = Query(False, alias="includeArchived"),
    user: User = Depends(require_projects_user),
) -> ProjectListResponse:
    """Projects the caller owns or is a member of, most recently updated first."""
    listed = _svc().list_projects(user, include_archived=include_archived)
    names = display_names({p.owner_email for p, _ in listed})
    return ProjectListResponse(projects=[ProjectResponse.from_project(p, role, names) for p, role in listed])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectResponse, response_model_by_alias=True)
async def create_project(
    body: CreateProjectRequest, user: User = Depends(require_projects_user)
) -> ProjectResponse:
    """Create a project and its hidden harness Agent; the caller is the owner."""
    try:
        project = await _svc().create_project(user, body.name, body.description)
    except ProjectError as e:
        raise _translate(e)
    return await asyncio.to_thread(_project_response, project, "owner")


@router.get("/{project_id}", response_model=ProjectResponse, response_model_by_alias=True)
def get_project(project_id: str, user: User = Depends(require_projects_user)) -> ProjectResponse:
    try:
        project, role = _svc().get_project(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    return _project_response(project, role)


@router.patch("/{project_id}", response_model=ProjectResponse, response_model_by_alias=True)
async def update_project(
    project_id: str, body: UpdateProjectRequest, user: User = Depends(require_projects_user)
) -> ProjectResponse:
    """Editors: name, description. Owner: also ``editorsManageMembers`` and ``status``."""
    try:
        project, role = await _svc().update_project(
            project_id,
            user,
            name=body.name,
            description=body.description,
            editors_manage_members=body.editors_manage_members,
            status=body.status,
        )
    except ProjectError as e:
        raise _translate(e)
    return await asyncio.to_thread(_project_response, project, role)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, user: User = Depends(require_projects_user)) -> None:
    """Purge an archived project (owner only)."""
    try:
        await _svc().purge_project(project_id, user)
    except ProjectError as e:
        raise _translate(e)


@router.post("/{project_id}/transfer", response_model=ProjectResponse, response_model_by_alias=True)
def transfer_project(
    project_id: str, body: TransferOwnershipRequest, user: User = Depends(require_projects_user)
) -> ProjectResponse:
    """Make an existing editor the owner; the caller becomes an editor."""
    try:
        project = _svc().transfer_ownership(project_id, user, body.email)
    except ProjectError as e:
        raise _translate(e)
    return _project_response(project, "editor")


# ---- settings (instructions, model, tools, skills) ---------------------


def _refs(harness_bindings, kind: str) -> list:
    return [BindingRef(ref=b.ref, config=b.config) for b in (harness_bindings or []) if b.kind == kind]


async def _save(project_id: str, user: User, **changes) -> HarnessView:
    try:
        return await _settings().update(project_id, user, **changes)
    except ProjectError as e:
        raise _translate(e)
    except BindingValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


async def _view(project_id: str, user: User) -> HarnessView:
    try:
        return await _settings().get(project_id, user)
    except ProjectError as e:
        raise _translate(e)


def _instructions(view: HarnessView) -> InstructionsResponse:
    return InstructionsResponse(instructions=view.harness.instructions or "", version=view.version, can_edit=view.can_edit)


def _model(view: HarnessView) -> ModelResponse:
    return ModelResponse(model_settings=view.harness.model_settings, version=view.version, can_edit=view.can_edit)


def _bindings(view: HarnessView, kind: str) -> BindingsResponse:
    return BindingsResponse(bindings=_refs(view.harness.bindings, kind), version=view.version, can_edit=view.can_edit)


@router.get("/{project_id}/instructions", response_model=InstructionsResponse, response_model_by_alias=True)
async def get_instructions(project_id: str, user: User = Depends(require_projects_user)) -> InstructionsResponse:
    return _instructions(await _view(project_id, user))


@router.put("/{project_id}/instructions", response_model=InstructionsResponse, response_model_by_alias=True)
async def put_instructions(
    project_id: str, body: UpdateInstructionsRequest, user: User = Depends(require_projects_user)
) -> InstructionsResponse:
    """Replace the project's instructions (editor). Each change is a new version."""
    return _instructions(await _save(project_id, user, instructions=body.instructions))


@router.get("/{project_id}/model", response_model=ModelResponse, response_model_by_alias=True)
async def get_model(project_id: str, user: User = Depends(require_projects_user)) -> ModelResponse:
    return _model(await _view(project_id, user))


@router.put("/{project_id}/model", response_model=ModelResponse, response_model_by_alias=True)
async def put_model(
    project_id: str, body: UpdateModelRequest, user: User = Depends(require_projects_user)
) -> ModelResponse:
    """Set the project's model (editor). The saver must be allowed to use it; a member who
    is not falls back to their own default when they run the project (§9.6)."""
    return _model(await _save(project_id, user, model_settings=body.model_settings))


@router.get("/{project_id}/tools", response_model=BindingsResponse, response_model_by_alias=True)
async def get_tools(project_id: str, user: User = Depends(require_projects_user)) -> BindingsResponse:
    return _bindings(await _view(project_id, user), TOOL)


@router.put("/{project_id}/tools", response_model=BindingsResponse, response_model_by_alias=True)
async def put_tools(
    project_id: str, body: UpdateBindingsRequest, user: User = Depends(require_projects_user)
) -> BindingsResponse:
    """Replace the project's tools (editor). Tools the saver adds must be ones they can use."""
    bindings = [AgentBinding(kind=TOOL, ref=b.ref, config=b.config) for b in body.bindings]
    return _bindings(await _save(project_id, user, kind=TOOL, bindings=bindings), TOOL)


async def _skills(view: HarnessView) -> SkillBindingsResponse:
    pins = await skill_pins(view.harness)
    return SkillBindingsResponse(
        bindings=[
            SkillBindingRef(
                ref=b.ref,
                config=b.config,
                version=pins[b.ref].version if b.ref in pins else None,
                pinned_at=pins[b.ref].pinned_at if b.ref in pins else None,
                update_available=pins[b.ref].update_available if b.ref in pins else False,
            )
            for b in (view.harness.bindings or [])
            if b.kind == SKILL
        ],
        version=view.version,
        can_edit=view.can_edit,
    )


@router.get("/{project_id}/skills", response_model=SkillBindingsResponse, response_model_by_alias=True)
async def get_skills(project_id: str, user: User = Depends(require_projects_user)) -> SkillBindingsResponse:
    return await _skills(await _view(project_id, user))


@router.put("/{project_id}/skills", response_model=SkillBindingsResponse, response_model_by_alias=True)
async def put_skills(
    project_id: str, body: UpdateBindingsRequest, user: User = Depends(require_projects_user)
) -> SkillBindingsResponse:
    """Replace the project's skills (editor). Skills the saver adds must be ones they can use.

    A skill added without a version is pinned to its content as of this save; a skill
    already bound keeps its pin unless the request names another version.
    """
    bindings = [AgentBinding(kind=SKILL, ref=b.ref, config=b.config) for b in body.bindings]
    return await _skills(await _save(project_id, user, kind=SKILL, bindings=bindings))


@router.post(
    "/{project_id}/skills/{skill_id}/pin", response_model=SkillBindingsResponse, response_model_by_alias=True
)
async def pin_skill(
    project_id: str, skill_id: str, user: User = Depends(require_projects_user)
) -> SkillBindingsResponse:
    """Pin a bound skill to its current content (editor), the "update" for a newer version."""
    try:
        view = await _settings().pin_skill(project_id, user, skill_id)
    except ProjectError as e:
        raise _translate(e)
    except BindingValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return await _skills(view)


@router.get(
    "/{project_id}/instructions/versions", response_model=SettingsVersionsResponse, response_model_by_alias=True
)
async def list_settings_versions(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_projects_user),
) -> SettingsVersionsResponse:
    """Every saved change to the project's settings, newest first."""
    try:
        versions = await _settings().list_versions(project_id, user, limit)
    except ProjectError as e:
        raise _translate(e)
    names = await asyncio.to_thread(display_names, filter(None, (created_by_email(v) for v, _ in versions)))
    return SettingsVersionsResponse(versions=[
        SettingsVersionSummary(
            version=v.version,
            created_at=v.created_at,
            created_by_email=created_by_email(v),
            created_by_name=names.get(created_by_email(v) or ""),
            changes=[wire_field_name(f) for f in fields],
        )
        for v, fields in versions
    ])


@router.get(
    "/{project_id}/instructions/versions/{number}",
    response_model=SettingsVersionResponse,
    response_model_by_alias=True,
)
async def get_settings_version(
    project_id: str, number: int = Path(..., ge=1), user: User = Depends(require_projects_user)
) -> SettingsVersionResponse:
    """One version in full, with its diff against the version before it."""
    try:
        detail = await _settings().get_version(project_id, user, number)
    except ProjectError as e:
        raise _translate(e)
    v = detail.version
    changes = settings_changes(detail.previous, v)
    author = created_by_email(v)
    names = await asyncio.to_thread(display_names, [author] if author else [])
    return SettingsVersionResponse(
        version=v.version,
        created_at=v.created_at,
        created_by_email=author,
        created_by_name=names.get(author or ""),
        changes=[wire_field_name(f) for f, _, _ in changes],
        instructions=v.instructions or "",
        model_settings=v.model_settings,
        tools=_refs(v.bindings, TOOL),
        skills=_refs(v.bindings, SKILL),
        field_changes=[
            VersionFieldChange(
                field=wire_field_name(f),
                before=wire_value(before),
                after=wire_value(after),
                behavior=f in ("instructions", "bindings", "model_settings"),
            )
            for f, before, after in changes
        ],
        instructions_diff=version_instructions_diff(detail),
    )


# ---- audit -------------------------------------------------------------

# What a project's editors see of its trail: who did what, by email. Admins get
# the full record (``/admin/projects/{id}/audit``), user ids included. ``people``
# names the emails the page mentions (actors, and the members a record is about).
_MEMBER_AUDIT_FIELDS = ("auditId", "timestamp", "action", "actorEmail", "changes", "before", "after", "reason")


@router.get("/{project_id}/audit")
def project_audit(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, max_length=128),
    user: User = Depends(require_projects_user),
) -> Dict[str, Any]:
    """The project's audit trail, newest first (editor)."""
    try:
        records, next_cursor = _svc().list_audit(project_id, user, limit=limit, after=cursor)
    except ProjectError as e:
        raise _translate(e)
    except Exception:
        logger.exception("Failed to read the audit trail for project %s", scrub_log(project_id))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Audit log is unavailable.")
    page = [{k: v for k, v in r.to_response().items() if k in _MEMBER_AUDIT_FIELDS} for r in records]
    emails = set()
    for record in page:
        if isinstance(record.get("actorEmail"), str):
            emails.add(record["actorEmail"])
        for detail in (record.get("before"), record.get("after")):
            emails.update(_emails_in(detail))
    return {"records": page, "people": display_names({normalize_email(e) for e in emails}), "nextCursor": next_cursor}


# ---- tasks -------------------------------------------------------------


@router.get("/{project_id}/tasks", response_model=SessionsListResponse, response_model_exclude_none=True)
async def list_tasks(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    next_token: Optional[str] = Query(None, alias="nextToken"),
    user: User = Depends(require_projects_user),
) -> SessionsListResponse:
    """The caller's own tasks (sessions) in the project, most recent first.

    Only ever the caller's: other members' tasks reach the project only by being
    shared to it (``/shared-tasks``).
    """
    try:
        _svc().get_project(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    sessions, token = await list_project_sessions(user.user_id, project_id, limit=limit, next_token=next_token)
    return SessionsListResponse(
        sessions=[SessionMetadataResponse.model_validate(s.model_dump(by_alias=True)) for s in sessions],
        next_token=token,
    )


@router.get("/{project_id}/shared-tasks", response_model=SharedTasksResponse, response_model_by_alias=True)
def list_shared_tasks(project_id: str, user: User = Depends(require_projects_user)) -> SharedTasksResponse:
    """Tasks members have shared with the project (``accessLevel: "project"``)."""
    try:
        pointers = _svc().list_shared_tasks(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    names = display_names({p.owner_email for p in pointers})
    return SharedTasksResponse(tasks=[SharedTaskResponse.from_pointer(p, user.user_id, names) for p in pointers])


# ---- memory (Phase 2.4) ------------------------------------------------


@router.get("/{project_id}/memory", response_model=ProjectMemoryResponse, response_model_by_alias=True)
def get_project_memory(project_id: str, user: User = Depends(require_projects_user)) -> ProjectMemoryResponse:
    """The project's shared space and the caller's own. Creates the shared one if it is missing."""
    try:
        spaces = _svc().get_memory_spaces(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    return ProjectMemoryResponse(
        shared_space_id=spaces.shared_space_id,
        personal_space_id=spaces.personal_space_id,
        role=spaces.role,
        limits=_memory_limits(),
    )


def _memory_limits() -> MemoryLimitsResponse:
    from apis.shared.memory.hydration import MINE_MEMORY_MAX_TOKENS, PROJECT_MEMORY_MAX_TOKENS
    from apis.shared.memory.service import file_hard_cap_tokens, file_soft_threshold_tokens

    return MemoryLimitsResponse(
        file_hard_cap_tokens=file_hard_cap_tokens(),
        file_soft_threshold_tokens=file_soft_threshold_tokens(),
        project_index_budget_tokens=PROJECT_MEMORY_MAX_TOKENS,
        personal_index_budget_tokens=MINE_MEMORY_MAX_TOKENS,
    )


@router.post("/{project_id}/memory/mine", response_model=PersonalSpaceResponse, response_model_by_alias=True)
def create_personal_memory(project_id: str, user: User = Depends(require_projects_user)) -> PersonalSpaceResponse:
    """The caller's own memory in this project, created on first call (idempotent)."""
    try:
        space_id = _svc().get_or_create_personal_space(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    return PersonalSpaceResponse(space_id=space_id)


# ---- members -----------------------------------------------------------


@router.get("/{project_id}/members", response_model=MembersResponse, response_model_by_alias=True)
def list_members(project_id: str, user: User = Depends(require_projects_user)) -> MembersResponse:
    try:
        project, role, members = _svc().list_members(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    people = _svc().known_people([project.owner_email, *(m.email for m in members)])
    return MembersResponse.build(project, role, members, people)


@router.post("/{project_id}/members", response_model=AddMembersResponse, response_model_by_alias=True)
def add_members(
    project_id: str, body: AddMembersRequest, user: User = Depends(require_projects_user)
) -> AddMembersResponse:
    """Bulk invite by email. Invitees gain access immediately, signed in before or not."""
    try:
        result = _svc().add_members(project_id, user, body.emails, body.role)
    except ProjectError as e:
        raise _translate(e)
    return AddMembersResponse.from_result(result, _svc().known_people([m.email for m in result.added]))


@router.get("/{project_id}/directory", response_model=DirectoryResponse, response_model_by_alias=True)
async def search_directory(
    project_id: str,
    q: str = Query(..., min_length=1, max_length=254),
    limit: int = Query(10, ge=1, le=25),
    user: User = Depends(require_projects_user),
) -> DirectoryResponse:
    """People to invite, matched by email prefix or name, each marked with any role they already hold.

    The directory only knows people who have signed in, so a well-formed email it
    does not know is returned too: an invite is keyed by email and works either way.
    """
    try:
        project, _, members = _svc().list_members(project_id, user)
    except ProjectError as e:
        raise _translate(e)

    roles = {m.email: m.role for m in members}
    roles[project.owner_email] = "owner"
    people = [
        DirectoryPersonResponse(email=p.email, name=p.name, has_signed_in=p.known, member_role=roles.get(p.email))
        for p in await get_directory().search(q, limit)
    ]

    typed = normalize_email(q)
    if is_valid_email(typed) and all(p.email != typed for p in people):
        people = people[: limit - 1]
        people.append(DirectoryPersonResponse(
            email=typed, name="", has_signed_in=False, member_role=roles.get(typed),
        ))
    return DirectoryResponse(people=people)


# Declared before ``/members/{email}`` so "me" is never read as an address.
@router.delete("/{project_id}/members/me", status_code=status.HTTP_204_NO_CONTENT)
def leave_project(project_id: str, user: User = Depends(require_projects_user)) -> None:
    """Leave a project. The owner must transfer ownership first."""
    try:
        _svc().leave(project_id, user)
    except ProjectError as e:
        raise _translate(e)


@router.patch("/{project_id}/members/{email}", response_model=MemberResponse, response_model_by_alias=True)
def update_member(
    project_id: str, email: str, body: UpdateMemberRequest, user: User = Depends(require_projects_user)
) -> MemberResponse:
    try:
        member = _svc().update_member_role(project_id, user, email, body.role)
    except ProjectError as e:
        raise _translate(e)
    return MemberResponse.from_member(member, _svc().known_people([member.email]))


@router.delete("/{project_id}/members/{email}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(project_id: str, email: str, user: User = Depends(require_projects_user)) -> None:
    try:
        _svc().remove_member(project_id, user, email)
    except ProjectError as e:
        raise _translate(e)
