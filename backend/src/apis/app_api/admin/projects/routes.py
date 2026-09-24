"""``/admin/projects`` — oversight of every Shared Project (``admin.projects``, delegable).

Phase 1 is list, look, read the trail, and force-archive (or restore). Export and
the regulated-data designation (§5) arrive with the phases that give them
something to export or designate. Mounted only while ``PROJECTS_ENABLED``.

An admin acts on a project without being in it: nothing here resolves a project
role, and every change is recorded with the admin as actor.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, require_admin_scope
from apis.shared.projects.models import Project, ProjectStatus
from apis.shared.projects.service import ProjectConflictError, ProjectNotFoundError, ProjectService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["admin-projects"])

require_projects_admin = require_admin_scope("admin.projects")

_service: Optional[ProjectService] = None


def _svc() -> ProjectService:
    global _service
    if _service is None:
        from apis.app_api.projects.harness_gateway import AppApiHarnessGateway

        _service = ProjectService(harness=AppApiHarnessGateway())
    return _service


class AdminProjectResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(..., alias="projectId")
    name: str
    description: str
    owner_email: str = Field(..., alias="ownerEmail")
    status: ProjectStatus
    member_count: int = Field(..., alias="memberCount", description="Members besides the owner")
    harness_agent_id: str = Field(..., alias="harnessAgentId")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    @classmethod
    def from_project(cls, p: Project) -> "AdminProjectResponse":
        return cls(
            project_id=p.project_id, name=p.name, description=p.description, owner_email=p.owner_email,
            status=p.status, member_count=p.member_count, harness_agent_id=p.harness_agent_id,
            created_at=p.created_at, updated_at=p.updated_at,
        )


class AdminProjectListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    projects: List[AdminProjectResponse]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")


class AdminSetStatusRequest(BaseModel):
    status: ProjectStatus
    reason: Optional[str] = Field(None, max_length=500, description="Recorded on the project's audit trail")


@router.get("", response_model=AdminProjectListResponse, response_model_by_alias=True)
def list_projects(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, max_length=64),
    admin: User = Depends(require_projects_admin),
) -> AdminProjectListResponse:
    """Every project, active and archived, in storage order."""
    projects, next_cursor = _svc().admin_list_projects(limit=limit, after=cursor)
    return AdminProjectListResponse(
        projects=[AdminProjectResponse.from_project(p) for p in projects], next_cursor=next_cursor
    )


@router.get("/{project_id}", response_model=AdminProjectResponse, response_model_by_alias=True)
def get_project(project_id: str, admin: User = Depends(require_projects_admin)) -> AdminProjectResponse:
    try:
        return AdminProjectResponse.from_project(_svc().admin_get_project(project_id))
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.patch("/{project_id}", response_model=AdminProjectResponse, response_model_by_alias=True)
def set_project_status(
    project_id: str, body: AdminSetStatusRequest, admin: User = Depends(require_projects_admin)
) -> AdminProjectResponse:
    """Force-archive (or restore) a project. Archived is read-only for every member."""
    try:
        project = _svc().admin_set_status(project_id, admin, body.status, reason=body.reason)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ProjectConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return AdminProjectResponse.from_project(project)


@router.get("/{project_id}/audit")
def project_audit(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, max_length=128),
    admin: User = Depends(require_projects_admin),
) -> Dict[str, Any]:
    """The project's full audit trail, newest first, including actor user ids."""
    try:
        records, next_cursor = _svc().audit_trail(project_id, limit=limit, after=cursor)
    except Exception:
        logger.exception("Failed to read the audit trail for project %s", project_id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Audit log is unavailable.")
    return {"records": [r.to_response() for r in records], "nextCursor": next_cursor}
