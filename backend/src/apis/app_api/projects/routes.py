"""``/projects`` — Shared Projects CRUD, members, transfer and leave (shared-projects §5, PR-1.2),
and the caller's own and shared tasks (PR-1.6).

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

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import projects_enabled
from apis.shared.projects.service import (
    ProjectConflictError,
    ProjectError,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectService,
)
from apis.shared.sessions.metadata import list_project_sessions
from apis.shared.sessions.models import SessionMetadataResponse, SessionsListResponse

from .harness_gateway import AppApiHarnessGateway
from .models import (
    AddMembersRequest,
    AddMembersResponse,
    CreateProjectRequest,
    MemberResponse,
    MembersResponse,
    ProjectListResponse,
    ProjectResponse,
    SharedTaskResponse,
    SharedTasksResponse,
    TransferOwnershipRequest,
    UpdateMemberRequest,
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


# ---- projects ----------------------------------------------------------


@router.get("", response_model=ProjectListResponse, response_model_by_alias=True)
def list_projects(
    include_archived: bool = Query(False, alias="includeArchived"),
    user: User = Depends(require_projects_user),
) -> ProjectListResponse:
    """Projects the caller owns or is a member of, most recently updated first."""
    listed = _svc().list_projects(user, include_archived=include_archived)
    return ProjectListResponse(projects=[ProjectResponse.from_project(p, role) for p, role in listed])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectResponse, response_model_by_alias=True)
async def create_project(
    body: CreateProjectRequest, user: User = Depends(require_projects_user)
) -> ProjectResponse:
    """Create a project and its hidden harness Agent; the caller is the owner."""
    try:
        project = await _svc().create_project(user, body.name, body.description)
    except ProjectError as e:
        raise _translate(e)
    return ProjectResponse.from_project(project, "owner")


@router.get("/{project_id}", response_model=ProjectResponse, response_model_by_alias=True)
def get_project(project_id: str, user: User = Depends(require_projects_user)) -> ProjectResponse:
    try:
        project, role = _svc().get_project(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    return ProjectResponse.from_project(project, role)


@router.patch("/{project_id}", response_model=ProjectResponse, response_model_by_alias=True)
def update_project(
    project_id: str, body: UpdateProjectRequest, user: User = Depends(require_projects_user)
) -> ProjectResponse:
    """Editors: name, description. Owner: also ``editorsManageMembers`` and ``status``."""
    try:
        project, role = _svc().update_project(
            project_id,
            user,
            name=body.name,
            description=body.description,
            editors_manage_members=body.editors_manage_members,
            status=body.status,
        )
    except ProjectError as e:
        raise _translate(e)
    return ProjectResponse.from_project(project, role)


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
    return ProjectResponse.from_project(project, "editor")


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
    return SharedTasksResponse(tasks=[SharedTaskResponse.from_pointer(p, user.user_id) for p in pointers])


# ---- members -----------------------------------------------------------


@router.get("/{project_id}/members", response_model=MembersResponse, response_model_by_alias=True)
def list_members(project_id: str, user: User = Depends(require_projects_user)) -> MembersResponse:
    try:
        project, role, members = _svc().list_members(project_id, user)
    except ProjectError as e:
        raise _translate(e)
    return MembersResponse.build(project, role, members)


@router.post("/{project_id}/members", response_model=AddMembersResponse, response_model_by_alias=True)
def add_members(
    project_id: str, body: AddMembersRequest, user: User = Depends(require_projects_user)
) -> AddMembersResponse:
    """Bulk invite by email. Invitees gain access immediately, signed in before or not."""
    try:
        result = _svc().add_members(project_id, user, body.emails, body.role)
    except ProjectError as e:
        raise _translate(e)
    return AddMembersResponse.from_result(result)


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
    return MemberResponse.from_member(member)


@router.delete("/{project_id}/members/{email}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(project_id: str, email: str, user: User = Depends(require_projects_user)) -> None:
    try:
        _svc().remove_member(project_id, user, email)
    except ProjectError as e:
        raise _translate(e)
