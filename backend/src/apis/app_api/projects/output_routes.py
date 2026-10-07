"""A project's Outputs: artifacts members shared with it (Shared Projects 3.3).

Sharing happens on the artifact (``POST /artifacts/{id}/shares`` with
``accessLevel: "project"``); this surface lists the ``OUTPUT#`` pointers and
removes one. Removing revokes the project share itself, so the artifact link
stops working for members too. The sharer may always remove their own; an
editor or the owner may remove anyone's while the project is active.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apis.app_api.artifacts import project_outputs
from apis.app_api.artifacts.service import ArtifactShareService, RenderTokenConfigError
from apis.shared.audit import AuditAction
from apis.shared.auth.models import User
from apis.shared.directory import display_names
from apis.shared.projects.models import ROLE_RANK, ProjectOutput
from apis.shared.projects.service import ProjectError

from .routes import _svc, _translate, require_projects_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/outputs", tags=["projects"])


class ProjectOutputResponse(BaseModel):
    """An output as a member sees it: never the sharer's user id or task."""

    model_config = ConfigDict(populate_by_name=True)

    artifact_id: str = Field(..., alias="artifactId")
    share_id: str = Field(..., alias="shareId")
    version: int
    title: str
    content_type: str = Field(..., alias="contentType")
    shared_by_email: str = Field(..., alias="sharedByEmail")
    shared_by_name: Optional[str] = Field(None, alias="sharedByName")
    shared_at: str = Field(..., alias="sharedAt")
    share_url: str = Field(..., alias="shareUrl", description="The shared-artifact view")
    is_mine: bool = Field(..., alias="isMine")
    can_remove: bool = Field(..., alias="canRemove", description="The caller may remove it from the project")


class ProjectOutputsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    outputs: List[ProjectOutputResponse] = Field(..., description="Most recently shared first")


def _response(o: ProjectOutput, user: User, can_moderate: bool, names: dict) -> ProjectOutputResponse:
    mine = o.owner_id == user.user_id
    return ProjectOutputResponse(
        artifact_id=o.artifact_id,
        share_id=o.share_id,
        version=o.version,
        title=o.title or "Untitled artifact",
        content_type=o.content_type,
        shared_by_email=o.owner_email,
        shared_by_name=names.get(o.owner_email),
        shared_at=o.shared_at,
        share_url=f"/shared-artifact/{o.share_id}",
        is_mine=mine,
        can_remove=mine or can_moderate,
    )


@router.get("", response_model=ProjectOutputsResponse, response_model_by_alias=True)
def list_outputs(project_id: str, user: User = Depends(require_projects_user)) -> ProjectOutputsResponse:
    """Every member sees every output (viewer+)."""
    try:
        project, role = _svc().authorize(project_id, user, "viewer")
    except ProjectError as e:
        raise _translate(e)
    outputs = sorted(_svc().repository.list_outputs(project_id), key=lambda o: o.shared_at, reverse=True)
    can_moderate = project.status == "active" and ROLE_RANK[role] >= ROLE_RANK["editor"]
    names = display_names({o.owner_email for o in outputs})
    return ProjectOutputsResponse(outputs=[_response(o, user, can_moderate, names) for o in outputs])


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_output(project_id: str, artifact_id: str, user: User = Depends(require_projects_user)) -> Response:
    """Stop sharing an artifact with the project: the sharer, or an editor of an active project."""
    try:
        project, role = _svc().authorize(project_id, user, "viewer")
    except ProjectError as e:
        raise _translate(e)
    output = _svc().repository.get_output(project_id, artifact_id)
    if output is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if output.owner_id != user.user_id:
        if ROLE_RANK[role] < ROLE_RANK["editor"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who shared it, or an editor, can remove it.")
        if project.status != "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "This project is archived. Restore it to make changes.")

    service = ArtifactShareService()
    try:
        shares = [
            s for s in service.list_for_artifact(owner_id=output.owner_id, artifact_id=artifact_id)
            if s.get("access_level") == "project" and s.get("project_id") == project_id
        ]
        for share in shares:
            service.delete_share(share)
    except RenderTokenConfigError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Artifact sharing is unavailable")
    _svc().repository.delete_output(project_id, artifact_id)
    project_outputs.record(
        AuditAction.PROJECT_OUTPUT_REMOVED, user,
        {"project_id": project_id, "artifact_id": artifact_id, "version": output.version, "title": output.title},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
