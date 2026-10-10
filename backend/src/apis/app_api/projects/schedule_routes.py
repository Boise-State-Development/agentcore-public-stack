"""A project's schedules (Shared Projects 3.2).

A project schedule is a scheduled prompt that runs on the project's harness. It
lives in its creator's partition and runs as them, on their headless grant, so
only its creator can change what it does or resume it. Members see every
schedule and its run history; an editor or the owner can pause or delete anyone's,
because each one spends the project's budget and works in its name.

Creating one, and resuming one, need what running needs (see
``apis.shared.projects.schedules.run_block_reason``): an active project and a
creator who is an editor or the owner.

Needs both switches: ``PROJECTS_ENABLED`` (via ``require_projects_user``) and
``SCHEDULED_RUNS_ENABLED``; with either off these routes 404.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apis.app_api.schedules.models import CreateScheduleRequest, UpdateScheduleRequest
from apis.shared.audit import AuditAction
from apis.shared.auth.models import User
from apis.shared.directory import display_names
from apis.shared.feature_flags import scheduled_runs_enabled
from apis.shared.projects.models import ROLE_RANK, Project, ProjectRole, ProjectScheduleRun, normalize_email
from apis.shared.projects.schedules import SCHEDULE_ROLE, next_run_at, run_block_reason
from apis.shared.projects.service import ProjectError
from apis.shared.scheduled_prompts.models import IntervalUnit, ScheduleCadence, ScheduledPrompt, ScheduledPromptState
from apis.shared.scheduled_prompts.service import (
    MIN_INTERVAL_MINUTES,
    ScheduledPromptLimitExceeded,
    interval_to_minutes,
    create_scheduled_prompt,
    get_scheduled_prompt,
    set_schedule_state,
    update_scheduled_prompt,
)

from .routes import _svc, _translate, require_projects_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/schedules", tags=["projects"])

#: Why a create or resume was refused, by ``run_block_reason``'s code.
_BLOCKED = {
    "project_archived": "This project is archived. Restore it to make changes.",
    "project_deleted": "Project not found",
    "member_removed": "You're no longer a member of this project.",
    "not_editor": "Only an editor or the owner can run schedules in this project.",
    "projects_disabled": "Not found",
}


async def require_schedules_user(user: User = Depends(require_projects_user)) -> User:
    if not scheduled_runs_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return user


# ── models ──────────────────────────────────────────────────────────────


class CreateProjectScheduleRequest(CreateScheduleRequest):
    """A schedule's prompt and cadence. It always runs on the project's assistant, with its tools."""

    @model_validator(mode="after")
    def _no_agent_or_tools(self) -> "CreateProjectScheduleRequest":
        if self.assistant_id is not None or self.enabled_tools is not None:
            raise ValueError("A project schedule runs on the project's assistant and its tools")
        return self


class UpdateProjectScheduleRequest(UpdateScheduleRequest):
    """What a creator can change. Pause and resume have their own routes."""

    @model_validator(mode="after")
    def _content_only(self) -> "UpdateProjectScheduleRequest":
        if self.assistant_id is not None or self.enabled_tools is not None or self.clear_assistant or self.clear_tools:
            raise ValueError("A project schedule runs on the project's assistant and its tools")
        if self.state is not None:
            raise ValueError("Use pause or resume to change a schedule's state")
        return self


class ProjectScheduleResponse(BaseModel):
    """A schedule as a member sees it. The creator also gets their last run's task."""

    model_config = ConfigDict(populate_by_name=True)

    schedule_id: str = Field(..., alias="scheduleId")
    label: str
    prompt_text: str = Field(..., alias="promptText")
    cadence: ScheduleCadence
    hour_local: int = Field(..., alias="hourLocal")
    weekday: Optional[int] = None
    interval_value: Optional[int] = Field(None, alias="intervalValue")
    interval_unit: Optional[IntervalUnit] = Field(None, alias="intervalUnit")
    timezone: str
    state: ScheduledPromptState
    state_reason: Optional[str] = Field(None, alias="stateReason")
    next_run_at: Optional[str] = Field(None, alias="nextRunAt")
    last_run_at: Optional[str] = Field(None, alias="lastRunAt")
    last_run_status: Optional[str] = Field(None, alias="lastRunStatus")
    last_run_session_id: Optional[str] = Field(
        None, alias="lastRunSessionId", description="The creator's own task; absent for everyone else"
    )
    created_by_email: str = Field(..., alias="createdByEmail")
    created_by_name: Optional[str] = Field(None, alias="createdByName")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    is_mine: bool = Field(..., alias="isMine")
    can_edit: bool = Field(..., alias="canEdit", description="Change the prompt or cadence, and resume (the creator)")
    can_manage: bool = Field(..., alias="canManage", description="Pause and delete (the creator, an editor, the owner)")


class ProjectSchedulesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schedules: List[ProjectScheduleResponse] = Field(..., description="Oldest first")


class ProjectScheduleRunResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    status: Literal["completed", "error", "timeout", "paused"]
    finished_at: str = Field(..., alias="finishedAt")
    reason: Optional[str] = None


class ProjectScheduleRunsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    runs: List[ProjectScheduleRunResponse] = Field(..., description="Newest first")


# ── helpers ─────────────────────────────────────────────────────────────


def _authorize(project_id: str, user: User, min_role: ProjectRole = "viewer", *, writable: bool = False):
    try:
        return _svc().authorize(project_id, user, min_role, writable=writable)
    except ProjectError as e:
        raise _translate(e)


def _is_mine(schedule: ScheduledPrompt, user: User) -> bool:
    return schedule.user_id == user.user_id


def _can_moderate(project: Project, role: ProjectRole) -> bool:
    return project.status == "active" and ROLE_RANK[role] >= ROLE_RANK[SCHEDULE_ROLE]


def _response(
    schedule: ScheduledPrompt, user: User, project: Project, role: ProjectRole, names: dict
) -> ProjectScheduleResponse:
    mine = _is_mine(schedule, user)
    email = normalize_email(schedule.owner_email or "")
    return ProjectScheduleResponse(
        schedule_id=schedule.schedule_id,
        label=schedule.label,
        prompt_text=schedule.prompt_text,
        cadence=schedule.cadence,
        hour_local=schedule.hour_local,
        weekday=schedule.weekday,
        interval_value=schedule.interval_value,
        interval_unit=schedule.interval_unit,
        timezone=schedule.timezone,
        state=schedule.state,
        state_reason=schedule.state_reason,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        last_run_status=schedule.last_run_status,
        last_run_session_id=schedule.last_run_session_id if mine else None,
        created_by_email=email,
        created_by_name=names.get(email),
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
        is_mine=mine,
        can_edit=mine and project.status == "active",
        can_manage=mine or _can_moderate(project, role),
    )


async def _one(project_id: str, schedule_id: str) -> ScheduledPrompt:
    schedule = await asyncio.to_thread(_svc().schedules.get, project_id, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return schedule


async def _fresh(schedule: ScheduledPrompt) -> ScheduledPrompt:
    return await get_scheduled_prompt(schedule.user_id, schedule.schedule_id) or schedule


def _record(
    action: str, user: User, project_id: str, schedule: ScheduledPrompt, *, changes: Optional[List[str]] = None
) -> None:
    """One ``project.schedule_*`` record. A deletion names the schedule as it was (``before``)."""
    named = {"scheduleId": schedule.schedule_id, "label": schedule.label, "createdBy": schedule.owner_email}
    if action == AuditAction.PROJECT_SCHEDULE_DELETED:
        _svc().record(action, user, project_id, before=named)
    else:
        _svc().record(action, user, project_id, changes=changes, after=named)


# ── routes ──────────────────────────────────────────────────────────────


@router.get("", response_model=ProjectSchedulesResponse, response_model_by_alias=True)
async def list_schedules(project_id: str, user: User = Depends(require_schedules_user)) -> ProjectSchedulesResponse:
    """Every member sees every schedule (viewer+)."""
    project, role = await asyncio.to_thread(_authorize, project_id, user)
    found = await asyncio.to_thread(_svc().schedules.list, project_id)
    names = await asyncio.to_thread(display_names, {normalize_email(s.owner_email or "") for s in found})
    return ProjectSchedulesResponse(schedules=[_response(s, user, project, role, names) for s in found])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectScheduleResponse, response_model_by_alias=True)
async def create_schedule(
    project_id: str, body: CreateProjectScheduleRequest, user: User = Depends(require_schedules_user)
) -> ProjectScheduleResponse:
    """An editor or the owner; it runs as them on the project's assistant."""
    project, role = await asyncio.to_thread(_authorize, project_id, user, SCHEDULE_ROLE, writable=True)
    if not user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your account has no email, which project membership needs")
    try:
        schedule = await create_scheduled_prompt(
            user_id=user.user_id,
            label=body.label,
            prompt_text=body.prompt_text,
            cadence=body.cadence,
            hour_local=body.hour_local,
            timezone_name=body.timezone,
            weekday=body.weekday,
            interval_value=body.interval_value,
            interval_unit=body.interval_unit,
            assistant_id=project.harness_agent_id,
            # None: the harness's own tool bindings decide at run time, as for any
            # task in the project, rather than a snapshot of the creator's grants.
            enabled_tools=None,
            project_id=project_id,
            owner_email=normalize_email(user.email),
        )
    except ScheduledPromptLimitExceeded as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    try:
        await asyncio.to_thread(_svc().schedules.add_pointer, schedule)
    except Exception:
        # Unlisted, it would still run in the project with nobody able to see it.
        logger.error("Could not list schedule %s in project %s; removing it", schedule.schedule_id, project_id, exc_info=True)
        await asyncio.to_thread(_svc().schedules.delete, schedule)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "The schedule could not be saved. Try again.")
    _record(AuditAction.PROJECT_SCHEDULE_CREATED, user, project_id, schedule)
    names = await asyncio.to_thread(display_names, {normalize_email(user.email)})
    return _response(schedule, user, project, role, names)


@router.get("/{schedule_id}", response_model=ProjectScheduleResponse, response_model_by_alias=True)
async def get_schedule(
    project_id: str, schedule_id: str, user: User = Depends(require_schedules_user)
) -> ProjectScheduleResponse:
    project, role = await asyncio.to_thread(_authorize, project_id, user)
    schedule = await _one(project_id, schedule_id)
    names = await asyncio.to_thread(display_names, {normalize_email(schedule.owner_email or "")})
    return _response(schedule, user, project, role, names)


@router.patch("/{schedule_id}", response_model=ProjectScheduleResponse, response_model_by_alias=True)
async def update_schedule(
    project_id: str,
    schedule_id: str,
    body: UpdateProjectScheduleRequest,
    user: User = Depends(require_schedules_user),
) -> ProjectScheduleResponse:
    """The creator changes the prompt, label or cadence: it runs as them."""
    project, role = await asyncio.to_thread(_authorize, project_id, user, writable=True)
    schedule = await _one(project_id, schedule_id)
    if not _is_mine(schedule, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who made a schedule can change it.")

    changes = sorted(
        k for k, v in body.model_dump(by_alias=True, exclude_none=True).items() if k not in ("clearAssistant", "clearTools")
    )
    cadence = body.cadence or schedule.cadence
    if cadence == "weekly" and (body.weekday if body.weekday is not None else schedule.weekday) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "weekday is required when cadence is 'weekly'")
    if cadence == "interval":
        minutes = interval_to_minutes(
            body.interval_value if body.interval_value is not None else schedule.interval_value,
            body.interval_unit if body.interval_unit is not None else schedule.interval_unit,
        )
        if minutes is None or minutes < MIN_INTERVAL_MINUTES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"interval must be at least {MIN_INTERVAL_MINUTES} minutes"
            )
    try:
        updated = await update_scheduled_prompt(
            schedule.user_id,
            schedule_id,
            label=body.label,
            prompt_text=body.prompt_text,
            cadence=body.cadence,
            hour_local=body.hour_local,
            weekday=body.weekday,
            interval_value=body.interval_value,
            interval_unit=body.interval_unit,
            timezone_name=body.timezone,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if changes:
        _record(AuditAction.PROJECT_SCHEDULE_UPDATED, user, project_id, updated, changes=changes)
    names = await asyncio.to_thread(display_names, {normalize_email(updated.owner_email or "")})
    return _response(updated, user, project, role, names)


@router.post("/{schedule_id}/pause", response_model=ProjectScheduleResponse, response_model_by_alias=True)
async def pause_schedule(
    project_id: str, schedule_id: str, user: User = Depends(require_schedules_user)
) -> ProjectScheduleResponse:
    """The creator, or an editor or the owner (who tells the creator)."""
    project, role = await asyncio.to_thread(_authorize, project_id, user)
    schedule = await _one(project_id, schedule_id)
    mine = _is_mine(schedule, user)
    if not mine and not _can_moderate(project, role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who made it, or an editor, can pause it.")
    if schedule.state == "active":
        if mine:
            await set_schedule_state(schedule.user_id, schedule_id, "paused", state_reason="Paused by user")
        else:
            await asyncio.to_thread(_svc().schedules.pause, schedule, "paused_by_editor", project=project, actor=user)
        _record(AuditAction.PROJECT_SCHEDULE_PAUSED, user, project_id, schedule)
        schedule = await _fresh(schedule)
    names = await asyncio.to_thread(display_names, {normalize_email(schedule.owner_email or "")})
    return _response(schedule, user, project, role, names)


@router.post("/{schedule_id}/resume", response_model=ProjectScheduleResponse, response_model_by_alias=True)
async def resume_schedule(
    project_id: str, schedule_id: str, user: User = Depends(require_schedules_user)
) -> ProjectScheduleResponse:
    """The creator only: it runs as them. Refused while anything would stop it running."""
    project, role = await asyncio.to_thread(_authorize, project_id, user)
    schedule = await _one(project_id, schedule_id)
    if not _is_mine(schedule, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who made a schedule can resume it.")
    if schedule.state != "active":
        reason = await asyncio.to_thread(run_block_reason, schedule)
        if reason is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, _BLOCKED.get(reason, "This schedule can't run now."))
        await set_schedule_state(schedule.user_id, schedule_id, "active", next_run_at=next_run_at(schedule))
        _record(AuditAction.PROJECT_SCHEDULE_RESUMED, user, project_id, schedule)
        schedule = await _fresh(schedule)
    names = await asyncio.to_thread(display_names, {normalize_email(schedule.owner_email or "")})
    return _response(schedule, user, project, role, names)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(project_id: str, schedule_id: str, user: User = Depends(require_schedules_user)) -> Response:
    """The creator, or an editor or the owner of an active project."""
    project, role = await asyncio.to_thread(_authorize, project_id, user)
    schedule = await _one(project_id, schedule_id)
    mine = _is_mine(schedule, user)
    if not mine and not _can_moderate(project, role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who made it, or an editor, can delete it.")
    await asyncio.to_thread(_svc().schedules.delete, schedule)
    _record(AuditAction.PROJECT_SCHEDULE_DELETED, user, project_id, schedule)
    if not mine and schedule.owner_email:
        await asyncio.to_thread(
            _svc()._notify, "project_schedule_paused", schedule.owner_email, user, project,
            scheduleId=schedule.schedule_id, label=schedule.label, reason="deleted",
            createdBy=normalize_email(schedule.owner_email),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{schedule_id}/runs", response_model=ProjectScheduleRunsResponse, response_model_by_alias=True)
async def list_runs(
    project_id: str, schedule_id: str, user: User = Depends(require_schedules_user)
) -> ProjectScheduleRunsResponse:
    """The schedule's recent runs and pauses, newest first (viewer+)."""
    await asyncio.to_thread(_authorize, project_id, user)
    await _one(project_id, schedule_id)
    runs: List[ProjectScheduleRun] = await asyncio.to_thread(_svc().schedules.runs, project_id, schedule_id)
    return ProjectScheduleRunsResponse(
        runs=[
            ProjectScheduleRunResponse(run_id=r.run_id, status=r.status, finished_at=r.finished_at, reason=r.reason)
            for r in runs
        ]
    )
