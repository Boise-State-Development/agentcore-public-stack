"""Maintenance runs on a project's memory (Shared Projects 2.6).

``scope=project`` (the default) is the shared memory: the owner or an editor
starts a run over every file or one file, it runs in the maintenance worker
and ends in compaction proposals, which go through the ordinary review queue.
``scope=mine`` is the caller's own memory in the project (2.6b): any member
tidies their own, the worker saves the changes straight away, and the run can
be undone within the personal archive retention. These routes start a run,
report on it and undo it; none of them changes memory except the undo.

All behind ``require_projects_user`` (404 while ``PROJECTS_ENABLED`` is off).
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth.models import User
from apis.shared.memory.models import MaintenanceFileResult, MaintenanceRun
from apis.shared.projects.memory_maintenance import (
    MaintenanceRequestError,
    MaintenanceScope,
    ProjectMemoryMaintenance,
    resolve_maintenance_model,
)

from .routes import _svc, require_projects_user

router = APIRouter(prefix="/projects/{project_id}/memory/maintenance", tags=["projects"])


class StartMaintenanceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: Optional[str] = Field(None, min_length=1, max_length=128, description="One file; omitted for every file")


class MaintenanceFileResultResponse(MaintenanceFileResult):
    """One file's result without the hash an undo checks against, which is the server's business."""

    content_hash: Optional[str] = Field(None, alias="contentHash", exclude=True)


class MaintenanceRunResponse(BaseModel):
    """A run as the people who may see it see it: no snapshot, no prices.

    ``scope`` is the Memory page's (``project`` or ``mine``). An applied run on
    the caller's own memory carries each file's changes in ``results`` and,
    while it can still be undone, ``undoableUntil``.
    """

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    state: str
    scope: MaintenanceScope = "project"
    slug: Optional[str] = None
    requested_by_email: str = Field(..., alias="requestedByEmail")
    requested_by_name: Optional[str] = Field(None, alias="requestedByName")
    created_at: str = Field(..., alias="createdAt")
    started_at: Optional[str] = Field(None, alias="startedAt")
    finished_at: Optional[str] = Field(None, alias="finishedAt")
    results: List[MaintenanceFileResultResponse] = Field(default_factory=list)
    error: Optional[str] = None
    undone_at: Optional[str] = Field(None, alias="undoneAt")
    undoable_until: Optional[str] = Field(None, alias="undoableUntil")

    @classmethod
    def build(cls, run: MaintenanceRun, maintenance: Optional[ProjectMemoryMaintenance] = None) -> "MaintenanceRunResponse":
        results = [MaintenanceFileResultResponse.model_validate(r.model_dump()) for r in run.results]
        return cls(
            run_id=run.run_id, state=run.state, scope="mine" if run.space_scope == "personal_in_project" else "project",
            slug=run.slug, requested_by_email=run.requested_by_email, requested_by_name=run.requested_by_name,
            created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at, results=results,
            error=run.error, undone_at=run.undone_at,
            undoable_until=maintenance.undoable_until(run) if maintenance is not None else None,
        )


class MaintenanceRunsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    runs: List[MaintenanceRunResponse] = Field(..., description="Newest first")


def _maintenance() -> ProjectMemoryMaintenance:
    svc = _svc()
    return ProjectMemoryMaintenance(repository=svc.repository, audit=svc.audit)


def _translate(e: MaintenanceRequestError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=str(e))


@router.post("", response_model=MaintenanceRunResponse, response_model_by_alias=True, status_code=status.HTTP_202_ACCEPTED)
async def start_maintenance(
    project_id: str,
    body: StartMaintenanceRequest,
    scope: MaintenanceScope = Query("project"),
    user: User = Depends(require_projects_user),
) -> MaintenanceRunResponse:
    """Queue a run; 409 while another is in flight. Poll the run for its results.

    ``project``: owner or editor. ``mine``: any member, on their own memory only.
    """
    try:
        model = await resolve_maintenance_model()
        maintenance = _maintenance()
        run = await run_in_threadpool(maintenance.start, project_id, user, model, slug=body.slug, scope=scope)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunResponse.build(run, maintenance)


@router.get("", response_model=MaintenanceRunsResponse, response_model_by_alias=True)
def list_maintenance_runs(
    project_id: str, scope: MaintenanceScope = Query("project"), user: User = Depends(require_projects_user)
) -> MaintenanceRunsResponse:
    """Recent runs on one scope, newest first. ``project``: owner and editors; ``mine``: your own."""
    maintenance = _maintenance()
    try:
        runs = maintenance.list_runs(project_id, user, scope=scope)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunsResponse(runs=[MaintenanceRunResponse.build(r, maintenance) for r in runs])


@router.get("/{run_id}", response_model=MaintenanceRunResponse, response_model_by_alias=True)
def get_maintenance_run(
    project_id: str, run_id: str, scope: MaintenanceScope = Query("project"), user: User = Depends(require_projects_user)
) -> MaintenanceRunResponse:
    maintenance = _maintenance()
    try:
        run = maintenance.get_run(project_id, user, run_id, scope=scope)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunResponse.build(run, maintenance)


@router.post("/{run_id}/undo", response_model=MaintenanceRunResponse, response_model_by_alias=True)
def undo_maintenance_run(
    project_id: str, run_id: str, user: User = Depends(require_projects_user)
) -> MaintenanceRunResponse:
    """Undo a tidy-up of your own memory. Files saved since the run are left as they are and reported.

    Only runs on the caller's own memory can be undone; the shared memory's
    changes went through review. 409 once undone, past retention, or while a
    run is in flight.
    """
    maintenance = _maintenance()
    try:
        run = maintenance.undo(project_id, user, run_id)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunResponse.build(run, maintenance)
