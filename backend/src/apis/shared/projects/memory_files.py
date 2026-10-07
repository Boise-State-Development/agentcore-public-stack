"""A project's memory files, item by item: pins, provenance, the archive (Shared Projects 2.5a-2) and edits (2.8b).

``scope`` addresses a space the way the harness tools do: ``project`` is the
project's shared space, ``mine`` the caller's own space in it. Roles come from
``MemorySpaceService`` (project-aware since 2.4a): any member reads the shared
space, editors pin and restore in it, and a member does anything in their own.
An archived project is read-only (409 on a write).

Edits from the Memory tab (2.8b) are structured: the caller sends items, each
with the anchor it was read with (or none for a new item), and this layer
renders them as the file text the save pipeline takes. A new file joins its
scope's ``MEMORY.md`` the way the harness's ``memory_save`` does, and a
deleted file leaves it. With an ``audit`` service (app-api), edits and deletes
of the shared memory are ``project.memory_*`` records; a member's own memory
is theirs alone and is not audited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Literal, Optional, Sequence, Tuple

from apis.shared.audit.models import AuditAction
from apis.shared.auth.models import User

from .access import resolve_project_role
from .memory_proposals import ARCHIVED, NOT_A_MEMBER, NO_SHARED_MEMORY, ProposalProjectError
from .repository import ProjectRepository

logger = logging.getLogger(__name__)

MemoryScope = Literal["project", "mine"]
NO_PERSONAL_MEMORY = "You haven't kept anything of your own in this project yet."
CHANGED_SINCE = "Someone changed this file after you opened it. Reload it and make your change again."
ALREADY_EXISTS = "A file with that name already exists. Open it to edit it, or pick another name."


@dataclass(frozen=True)
class EditedItem:
    """One item as the editor sends it: its anchor if it was read with one, and its text."""

    text: str
    anchor: Optional[str] = None


def render_items_for_save(items: Sequence[EditedItem]) -> str:
    """Items as the file text a save takes: ``- text <!-- e:anchor -->``, continuation lines indented.

    A new item goes without an anchor, so the pipeline mints one. Validation
    (empty items, stray anchors in the text, unknown anchors) is the
    pipeline's, so its messages reach the editor unchanged.
    """
    out: List[str] = []
    for item in items:
        lines = (item.text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
        rendered = [f"- {lines[0]}"] + [f"  {line}" if line.strip() else "" for line in lines[1:]]
        if item.anchor:
            rendered[-1] = f"{rendered[-1]} <!-- e:{item.anchor} -->"
        out.extend(rendered)
    return "\n".join(out) + ("\n" if out else "")


class ProjectMemoryFiles:
    def __init__(self, repository: Optional[ProjectRepository] = None, memory: Any = None, audit: Any = None) -> None:
        self.repository = repository or ProjectRepository()
        self._memory = memory
        self.audit = audit

    @property
    def memory(self):
        if self._memory is None:
            from apis.shared.memory.service import MemorySpaceService

            self._memory = MemorySpaceService()
        return self._memory

    def space(self, project_id: str, user: User, scope: MemoryScope, *, writable: bool) -> str:
        return self._resolve(project_id, user, scope, writable=writable)[1]

    def _resolve(self, project_id: str, user: User, scope: MemoryScope, *, writable: bool) -> Tuple[Any, str]:
        project, role = resolve_project_role(project_id, user.user_id, user.email, repository=self.repository)
        if project is None or role is None:
            raise ProposalProjectError(404, NOT_A_MEMBER)
        if writable and project.status != "active":
            raise ProposalProjectError(409, ARCHIVED)
        if scope == "project":
            if not project.shared_space_id:
                raise ProposalProjectError(409, NO_SHARED_MEMORY)
            return project, project.shared_space_id
        space_id = self.repository.get_personal_space_id(project_id, user.user_id)
        if not space_id:
            raise ProposalProjectError(404, NO_PERSONAL_MEMORY)
        return project, space_id

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
        """Put an archived item back; a file it recreates rejoins the index, as Delete took it out."""
        space_id = self.space(project_id, user, scope, writable=True)
        result = self.memory.restore_archived_item(space_id, user.user_id, user.email, archive_id)
        if result.ref.version == 1:
            self._index_new_file(space_id, user, scope, result)
        return result

    # ---- edits (2.8b) -------------------------------------------------

    def save(
        self,
        project_id: str,
        user: User,
        scope: MemoryScope,
        slug: str,
        items: Sequence[EditedItem],
        *,
        description: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        base_version: Optional[int] = None,
    ) -> Tuple[Any, Optional[str]]:
        """Save a file from its items (editor+ for ``project``; anyone in ``mine``).

        ``base_version`` is the version the editor opened (0 for a new file).
        A file that moved on since, or a new file whose name is taken, is a 409,
        so one editor never silently overwrites another's change. Returns
        ``(save_result, index_outcome)``, the outcome being how a new file's
        index line went (None for an existing file).
        """
        project, space_id = self._resolve(project_id, user, scope, writable=True)
        if base_version is not None:
            current = next(
                (e for e in self.memory.list_entries(space_id, user.user_id, user.email) if e.slug == slug), None
            )
            if base_version == 0 and current is not None:
                raise ProposalProjectError(409, ALREADY_EXISTS)
            if base_version > 0 and (current is None or (current.version or 1) != base_version):
                raise ProposalProjectError(409, CHANGED_SINCE)
        result = self.memory.save_entry(
            space_id, user.user_id, user.email, slug, render_items_for_save(items),
            description=description, aliases=aliases, reason="edit",
        )
        indexed = self._index_new_file(space_id, user, scope, result) if result.ref.version == 1 else None
        if scope == "project":
            after = {"slug": result.ref.slug, "version": result.ref.version}
            if result.ref.version == 1:
                after["created"] = True
            self._record(AuditAction.PROJECT_MEMORY_EDITED, user, project, after=after)
        return result, indexed

    def delete(self, project_id: str, user: User, scope: MemoryScope, slug: str) -> None:
        """Delete a file: its items go to the archive and its index line goes too."""
        project, space_id = self._resolve(project_id, user, scope, writable=True)
        self.memory.delete_entry(space_id, user.user_id, user.email, slug)
        try:
            self.memory.remove_index_link(space_id, user.user_id, user.email, slug)
        except Exception:
            logger.warning("Could not drop the index line of deleted file %s in %s", slug, space_id, exc_info=True)
        if scope == "project":
            self._record(AuditAction.PROJECT_MEMORY_DELETED, user, project, before={"slug": slug})

    def save_index(self, project_id: str, user: User, scope: MemoryScope, content: str) -> None:
        """Replace a scope's ``MEMORY.md`` (editor+ for ``project``; anyone in ``mine``)."""
        project, space_id = self._resolve(project_id, user, scope, writable=True)
        self.memory.update_index(space_id, user.user_id, user.email, content)
        if scope == "project":
            self._record(AuditAction.PROJECT_MEMORY_EDITED, user, project, after={"slug": "MEMORY.md"})

    def _index_new_file(self, space_id: str, user: User, scope: MemoryScope, result: Any) -> Optional[str]:
        from apis.shared.memory.hydration import MINE_MEMORY_MAX_TOKENS, PROJECT_MEMORY_MAX_TOKENS

        budget = PROJECT_MEMORY_MAX_TOKENS if scope == "project" else MINE_MEMORY_MAX_TOKENS
        try:
            return self.memory.add_index_link(space_id, user.user_id, user.email, result.ref, max_tokens=budget)
        except Exception:
            logger.warning("Could not index new file %s in %s", result.ref.slug, space_id, exc_info=True)
            return None

    def _record(self, action: str, user: User, project: Any, *, before: Optional[dict] = None, after: Optional[dict] = None) -> None:
        if self.audit is None:
            return
        from apis.shared.audit import TARGET_PROJECT

        self.audit.record(
            action=action, actor=user, target_type=TARGET_PROJECT, target_id=project.project_id,
            before=before, after=after,
        )
