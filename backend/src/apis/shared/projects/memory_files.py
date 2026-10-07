"""A project's memory files, item by item: pins, provenance and the archive (Shared Projects 2.5a-2).

``scope`` addresses a space the way the harness tools do: ``project`` is the
project's shared space, ``mine`` the caller's own space in it. Roles come from
``MemorySpaceService`` (project-aware since 2.4a): any member reads the shared
space, editors pin and restore in it, and a member does anything in their own.
An archived project is read-only (409 on a write).
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Tuple

from apis.shared.auth.models import User

from .access import resolve_project_role
from .memory_proposals import ARCHIVED, NOT_A_MEMBER, NO_SHARED_MEMORY, ProposalProjectError
from .repository import ProjectRepository

MemoryScope = Literal["project", "mine"]
NO_PERSONAL_MEMORY = "You haven't kept anything of your own in this project yet."


class ProjectMemoryFiles:
    def __init__(self, repository: Optional[ProjectRepository] = None, memory: Any = None) -> None:
        self.repository = repository or ProjectRepository()
        self._memory = memory

    @property
    def memory(self):
        if self._memory is None:
            from apis.shared.memory.service import MemorySpaceService

            self._memory = MemorySpaceService()
        return self._memory

    def space(self, project_id: str, user: User, scope: MemoryScope, *, writable: bool) -> str:
        project, role = resolve_project_role(project_id, user.user_id, user.email, repository=self.repository)
        if project is None or role is None:
            raise ProposalProjectError(404, NOT_A_MEMBER)
        if writable and project.status != "active":
            raise ProposalProjectError(409, ARCHIVED)
        if scope == "project":
            if not project.shared_space_id:
                raise ProposalProjectError(409, NO_SHARED_MEMORY)
            return project.shared_space_id
        space_id = self.repository.get_personal_space_id(project_id, user.user_id)
        if not space_id:
            raise ProposalProjectError(404, NO_PERSONAL_MEMORY)
        return space_id

    def read(self, project_id: str, user: User, scope: MemoryScope, slug: str) -> Tuple[Any, Any, Any]:
        """``(ref, items, provenance)`` for one file."""
        space_id = self.space(project_id, user, scope, writable=False)
        return self.memory.read_file_items(space_id, user.user_id, user.email, slug)

    def set_pinned(self, project_id: str, user: User, scope: MemoryScope, slug: str, anchor: str, *, pinned: bool):
        space_id = self.space(project_id, user, scope, writable=True)
        return self.memory.set_pinned(space_id, user.user_id, user.email, slug, anchor, pinned=pinned)

    def archive(self, project_id: str, user: User, scope: MemoryScope):
        space_id = self.space(project_id, user, scope, writable=False)
        return self.memory.list_archived_items(space_id, user.user_id, user.email)

    def restore(self, project_id: str, user: User, scope: MemoryScope, archive_id: str):
        space_id = self.space(project_id, user, scope, writable=True)
        return self.memory.restore_archived_item(space_id, user.user_id, user.email, archive_id)
