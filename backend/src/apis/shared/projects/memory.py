"""The project's Memory Spaces, behind a narrow port (Shared Projects §3.3, Phase 2.4).

A project owns one ``shared`` space and one ``personal_in_project`` space per
member who has saved something. The spaces take their permissions from the
project (``MemorySpaceService.resolve_permission``), so this port only creates,
renames and purges them.

Like :mod:`.harness`, the port lets project orchestration be tested without
the memory table and bucket. The default imports the memory service lazily,
because that service resolves a project space's role through
:mod:`.access`, and because the lean Lambda images that copy this package
never carry ``apis.shared.memory``.
"""

from __future__ import annotations

from typing import Protocol


class ProjectMemoryGateway(Protocol):
    @property
    def enabled(self) -> bool:
        """Whether Memory Spaces exist here; while off, a project has no memory."""
        ...

    def create_space(
        self, *, project_id: str, scope: str, owner_id: str, owner_email: str, name: str, user_id: str | None = None
    ) -> str:
        """Create a project space (``shared`` or ``personal_in_project``); return its id."""
        ...

    def rename_space(self, space_id: str, name: str) -> None:
        ...

    def purge_space(self, space_id: str) -> None:
        """Delete a project space and its objects. Must tolerate an id that is already gone."""
        ...


class MemorySpacesGateway:
    """Default gateway over ``apis.shared.memory.service``."""

    @property
    def enabled(self) -> bool:
        from apis.shared.feature_flags import memory_spaces_enabled

        return memory_spaces_enabled()

    @staticmethod
    def _service():
        from apis.shared.memory.service import MemorySpaceService

        return MemorySpaceService()

    def create_space(
        self, *, project_id: str, scope: str, owner_id: str, owner_email: str, name: str, user_id: str | None = None
    ) -> str:
        space = self._service().create_project_space(
            project_id=project_id,
            scope=scope,  # type: ignore[arg-type]
            owner_id=owner_id,
            owner_email=owner_email,
            name=name,
            user_id=user_id,
        )
        return space.space_id

    def rename_space(self, space_id: str, name: str) -> None:
        self._service().rename_project_space(space_id, name)

    def purge_space(self, space_id: str) -> None:
        self._service().purge_project_space(space_id)
