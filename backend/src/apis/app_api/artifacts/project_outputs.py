"""Artifacts shared with a Shared Project: its Outputs (Shared Projects 3.3).

An artifact reaches a project only because a member shares it with "Project
members" (``access_level: "project"``). The project is the one the artifact's
task belongs to: the version row's ``session_id``, whose
``preferences.projectId`` names it. So only artifacts made in a project task
can be shared with a project, as for conversation shares.

The artifact share row is the grant (its read check asks membership at read
time). ``OUTPUT#{artifactId}`` on the project only lists it: one per artifact,
rebuilt from the artifact's remaining project shares after every change, so
revoking the newest of several falls back to the next and revoking the last
removes it. Rebuilding is best-effort: a stale pointer only lists a share that
now 404s.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional, Tuple

from apis.shared.auth import User
from apis.shared.feature_flags import projects_enabled
from apis.shared.projects.access import resolve_project_role
from apis.shared.projects.models import Project, ProjectOutput
from apis.shared.projects.repository import ProjectRepository
from apis.shared.shares import artifact_shares
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)


class ArtifactProjectError(Exception):
    """Why an artifact can't be shared with a project. ``status_code`` is the HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _project_of_session(session_id: str, user: User) -> Optional[str]:
    if not session_id:
        return None
    from apis.shared.sessions.metadata import get_session_metadata

    metadata = await get_session_metadata(session_id=session_id, user_id=user.user_id)
    prefs = getattr(metadata, "preferences", None) if metadata else None
    return getattr(prefs, "project_id", None) if prefs else None


async def shareable_project(user: User, session_id: str, repository: Optional[ProjectRepository] = None) -> Optional[Project]:
    """The project an artifact from ``session_id`` may be shared with, or None. Never raises."""
    if not projects_enabled():
        return None
    try:
        project_id = await _project_of_session(session_id, user)
        if not project_id:
            return None
        project, role = resolve_project_role(project_id, user.user_id, user.email, repository=repository)
    except Exception:
        logger.warning("Could not resolve the project of session %s", scrub_log(session_id), exc_info=True)
        return None
    if project is None or role is None or project.status != "active":
        return None
    return project


async def require_shareable_project(
    user: User, session_id: str, repository: Optional[ProjectRepository] = None
) -> Project:
    """The project an artifact from ``session_id`` is shared with, or the reason it can't be."""
    if not projects_enabled():
        raise ArtifactProjectError(400, "Projects are not available")
    project_id = await _project_of_session(session_id, user)
    if not project_id:
        raise ArtifactProjectError(400, "Only an artifact made in a project conversation can be shared with a project")
    project, role = resolve_project_role(project_id, user.user_id, user.email, repository=repository)
    if project is None or role is None:
        raise ArtifactProjectError(403, "You are not a member of this artifact's project")
    if project.status != "active":
        raise ArtifactProjectError(409, "This project is archived. Restore it to share with it.")
    return project


def put_output(share: dict, repository: Optional[ProjectRepository] = None) -> None:
    """List a new project share. Raises, so the caller can undo a share nobody could find."""
    (repository or ProjectRepository()).put_output(artifact_shares.output_pointer(share))


def sync_output(
    owner_id: str, artifact_id: str, project_id: str, repository: Optional[ProjectRepository] = None
) -> None:
    """Point ``OUTPUT#{artifact_id}`` at the artifact's newest share with ``project_id``, or remove it."""
    from .service import _table

    try:
        artifact_shares.sync_project_output(_table(), owner_id, artifact_id, project_id, repository=repository)
    except Exception:
        logger.warning(
            "Could not sync output %s in project %s", scrub_log(artifact_id), scrub_log(project_id), exc_info=True
        )


def sync_outputs(owner_id: str, shares: Iterable[dict], repository: Optional[ProjectRepository] = None) -> None:
    """Re-sync every (artifact, project) pair a batch of changed shares touched. Never raises."""
    pairs: set[Tuple[str, str]] = {
        (str(s.get("artifact_id", "")), str(s["project_id"]))
        for s in shares
        if s.get("access_level") == "project" and s.get("project_id")
    }
    for artifact_id, project_id in pairs:
        sync_output(owner_id, artifact_id, project_id, repository=repository)


# Previews computed in one listing at once. A listing usually finds one new
# output; a project opened for the first time since this shipped finds them all.
_PREVIEW_WORKERS = 4


def _compute_preview(output: ProjectOutput) -> Optional[str]:
    """The excerpt of the version ``output`` lists, "" when it has none, None when unreadable for now."""
    from . import preview
    from .service import ArtifactNotFoundError, ArtifactTooLargeError, ArtifactContentService

    try:
        content, content_type = ArtifactContentService().get(
            owner_id=output.owner_id, artifact_id=output.artifact_id, version=output.version
        )
    except (ArtifactNotFoundError, ArtifactTooLargeError):
        return ""
    except Exception:
        logger.warning("Could not read output %s for its preview", scrub_log(output.artifact_id), exc_info=True)
        return None
    return preview.excerpt(content, content_type)


def fill_previews(outputs: List[ProjectOutput], repository: Optional[ProjectRepository] = None) -> None:
    """Give every output without an excerpt one, and keep it on its pointer. Never raises.

    The listing's caller is a member, and a project share lets every member
    read the artifact, so reading its content here grants nothing new.
    """
    missing = [o for o in outputs if o.preview is None]
    if not missing:
        return
    repo = repository or ProjectRepository()
    with ThreadPoolExecutor(max_workers=min(_PREVIEW_WORKERS, len(missing))) as pool:
        computed = list(pool.map(_compute_preview, missing))
    for output, text in zip(missing, computed):
        if text is None:
            continue
        output.preview = text
        try:
            repo.set_output_preview(output.project_id, output.artifact_id, output.version, text)
        except Exception:
            logger.warning("Could not keep the preview of output %s", scrub_log(output.artifact_id), exc_info=True)


def artifact_session_id(owner_id: str, artifact_id: str) -> str:
    """The task an artifact was made in, from its HEAD row in the owner's partition ("" if unknown)."""
    from .service import _table

    item = _table().get_item(Key={"PK": f"USER#{owner_id}", "SK": f"ARTIFACT#{artifact_id}#HEAD"}).get("Item")
    return str((item or {}).get("session_id", ""))


def record(action: str, user: User, share: dict) -> None:
    """A ``project.output_*`` record on the project's trail. Never raises."""
    try:
        from apis.shared.audit import TARGET_PROJECT, get_audit_service

        get_audit_service().record(
            action=action,
            actor=user,
            target_type=TARGET_PROJECT,
            target_id=str(share["project_id"]),
            after={"artifactId": share.get("artifact_id"), "version": share.get("version"), "title": share.get("title", "")},
        )
    except Exception:
        logger.warning("Could not audit %s", action, exc_info=True)
