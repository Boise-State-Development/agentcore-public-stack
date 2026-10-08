"""Maintenance runs on a project's shared memory (Shared Projects 2.6).

The owner or an editor starts a run over every file or one file. It runs in
the maintenance worker and ends in compaction proposals, which go through the
ordinary review queue; nothing here changes memory. These routes start a run
and report on it.

All behind ``require_projects_user`` (404 while ``PROJECTS_ENABLED`` is off).
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth.models import User
from apis.shared.memory.models import MaintenanceFileResult, MaintenanceRun
from apis.shared.projects.memory_maintenance import (
    MaintenanceRequestError,
    ProjectMemoryMaintenance,
    resolve_maintenance_model,
)

from .routes import _svc, require_projects_user

router = APIRouter(prefix="/projects/{project_id}/memory/maintenance", tags=["projects"])


class StartMaintenanceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: Optional[str] = Field(None, min_length=1, max_length=128, description="One file; omitted for every file")


class MaintenanceRunResponse(BaseModel):
    """A run as the owner and editors see it: no snapshot, no prices."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    state: str
    slug: Optional[str] = None
    requested_by_email: str = Field(..., alias="requestedByEmail")
    requested_by_name: Optional[str] = Field(None, alias="requestedByName")
    created_at: str = Field(..., alias="createdAt")
    started_at: Optional[str] = Field(None, alias="startedAt")
    finished_at: Optional[str] = Field(None, alias="finishedAt")
    results: List[MaintenanceFileResult] = Field(default_factory=list)
    error: Optional[str] = None

    @classmethod
    def build(cls, run: MaintenanceRun) -> "MaintenanceRunResponse":
        return cls(
            run_id=run.run_id, state=run.state, slug=run.slug, requested_by_email=run.requested_by_email,
            requested_by_name=run.requested_by_name, created_at=run.created_at, started_at=run.started_at,
            finished_at=run.finished_at, results=run.results, error=run.error,
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
    project_id: str, body: StartMaintenanceRequest, user: User = Depends(require_projects_user)
) -> MaintenanceRunResponse:
    """Owner or editor. Queues a run; 409 while another is in flight. Poll the run for its results."""
    try:
        model = await resolve_maintenance_model()
        run = await run_in_threadpool(_maintenance().start, project_id, user, model, slug=body.slug)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunResponse.build(run)


@router.get("", response_model=MaintenanceRunsResponse, response_model_by_alias=True)
def list_maintenance_runs(project_id: str, user: User = Depends(require_projects_user)) -> MaintenanceRunsResponse:
    """The project's recent runs, newest first. Owner and editors."""
    try:
        runs = _maintenance().list_runs(project_id, user)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunsResponse(runs=[MaintenanceRunResponse.build(r) for r in runs])


@router.get("/{run_id}", response_model=MaintenanceRunResponse, response_model_by_alias=True)
def get_maintenance_run(project_id: str, run_id: str, user: User = Depends(require_projects_user)) -> MaintenanceRunResponse:
    try:
        run = _maintenance().get_run(project_id, user, run_id)
    except MaintenanceRequestError as e:
        raise _translate(e)
    return MaintenanceRunResponse.build(run)
