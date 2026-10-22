"""Project role resolution — the one question every project surface asks.

Deliberately imports nothing but the repository and models. The assistants
service calls :func:`resolve_project_role` to authorize a project's hidden
harness Agent, while the project service calls the assistants service to create
that harness; keeping this module free of the service breaks the cycle.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from .models import Project, ProjectRole, normalize_email
from .repository import ProjectRepository

logger = logging.getLogger(__name__)


def resolve_project_role(
    project_id: str,
    user_id: str,
    user_email: Optional[str],
    repository: Optional[ProjectRepository] = None,
) -> Tuple[Optional[Project], Optional[ProjectRole]]:
    """``(project, role)`` for a caller; ``(None, None)`` if the project is gone.

    Owner comes from META by user id; everyone else from their ``MEMBER#``
    row by email — the only thing access checks read. An archived project
    still resolves (members may read it); callers that write check ``status``.

    The first time a member resolves, their ``userId`` is back-filled on the
    member row (needed for ownership transfer and cost attribution). That write
    is best-effort: a failure costs nothing but a retry on the next request.
    """
    repo = repository or ProjectRepository()
    project = repo.get_project(project_id)
    if project is None:
        return None, None
    if project.owner_id == user_id:
        return project, "owner"
    if not user_email:
        return project, None

    member = repo.get_member(project_id, normalize_email(user_email))
    if member is None:
        return project, None
    if member.user_id is None and user_id:
        try:
            repo.set_member_user_id(project_id, member.email, user_id)
        except Exception:
            logger.warning("Could not back-fill userId on project %s member row", project_id, exc_info=True)
    return project, member.role
