"""The project's harness Agent, behind a narrow port.

A project's instructions, model, tools and knowledge live on an ordinary Agent
record marked ``kind="project"`` (shared-projects §2): the invocation path,
knowledge ingestion, retrieval and version diffs all key on an agent id, so a
project that *owns* an Agent inherits all of them.

The service talks to that Agent only through :class:`HarnessGateway`, for two
reasons. Tests can exercise project orchestration (including rollback) without
the assistants table. And deleting a harness for real needs the document and
sync-policy cleanup that lives in app-api, which ``apis.shared`` may not import —
so app-api injects a fuller gateway, while this default is enough to roll back a
create (a harness that just failed to attach has no documents yet).
"""

from __future__ import annotations

from typing import Protocol


class HarnessGateway(Protocol):
    async def create(
        self, *, project_id: str, owner_id: str, owner_name: str, name: str, description: str
    ) -> str:
        """Create the project's harness Agent; return its id."""
        ...

    async def delete(self, agent_id: str) -> None:
        """Delete the harness. Must tolerate an id that is already gone."""
        ...


class AssistantsHarnessGateway:
    """Default gateway over ``apis.shared.assistants.service``.

    Imports lazily: the assistants service resolves harness access through
    ``apis.shared.projects.access``.
    """

    async def create(
        self, *, project_id: str, owner_id: str, owner_name: str, name: str, description: str
    ) -> str:
        from apis.shared.assistants.service import create_assistant

        agent = await create_assistant(
            owner_id=owner_id,
            owner_name=owner_name,
            name=name,
            description=description,
            instructions="",
            visibility="PRIVATE",
            project_id=project_id,
        )
        return agent.assistant_id

    async def delete(self, agent_id: str) -> None:
        from apis.shared.assistants.service import delete_project_harness

        await delete_project_harness(agent_id)
