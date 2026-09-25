"""A project harness turn's memory: its two spaces, their prompt blocks, its tools.

Shared Projects 2.4b. A project's harness reaches memory by scope, not by an
Agent binding: ``project`` is the project's shared space (``sharedSpaceId`` on
Project META, which the turn has already read to refuse archived projects) and
``mine`` is the member's ``personal_in_project`` space (one ``PERSONAL_SPACE#``
GetItem).

**Turn-path cost.** :func:`load_project_memory` reads the two spaces
concurrently. The critical path is the personal side: the pointer, the space's
META and its ``MEMORY.md`` object. It skips ``resolve_permission`` (a project
META and a ``MEMBER#`` read per space), because the harness access check has
already resolved the caller as a member of this active project; the lean read
checks that each space is the one the project points at instead. The route
starts it as a task as soon as the project is known and awaits it at prompt
assembly, so it overlaps binding resolution and the knowledge-base search. It
logs how long it took and how long the turn actually waited for it.

A project without ``sharedSpaceId`` (made before 2.4a and never opened since)
gets no project block. The Runtime never backfills it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.auth.models import User

logger = logging.getLogger(__name__)


@dataclass
class ProjectMemoryTurn:
    """What one harness turn knows about its project's memory."""

    project_id: str
    shared_space_id: Optional[str]
    personal_space_id: Optional[str]
    memory_context: str = ""
    load_ms: float = 0.0

    def binding_key(self) -> Dict[str, Any]:
        """The ``memory_binding`` a harness turn passes to ``get_agent``.

        Its shape (``projectId`` present) selects the multi-scope digest in
        ``memory_binding_digest``. It holds exactly what the harness tools
        close over, apart from the member, whose user id is a key element
        already.
        """
        return {
            "projectId": self.project_id,
            "sharedSpaceId": self.shared_space_id,
            "personalSpaceId": self.personal_space_id,
        }


def _read_index(service: Any, space_id: str, project_id: str, scope: str, user_id: str) -> Optional[str]:
    from apis.shared.memory.service import MemorySpaceNotFoundError

    try:
        return service.read_project_space_index(space_id, project_id=project_id, scope=scope, user_id=user_id)
    except MemorySpaceNotFoundError:
        logger.warning("Project %s points at %s space %s, which is not its own; skipping", project_id, scope, space_id)
        return None


def _read_personal(service: Any, project_id: str, user_id: str) -> Tuple[Optional[str], Optional[str]]:
    from apis.shared.projects.repository import ProjectRepository

    space_id = ProjectRepository().get_personal_space_id(project_id, user_id)
    if not space_id:
        return None, None
    return space_id, _read_index(service, space_id, project_id, "personal_in_project", user_id)


async def load_project_memory(project_id: str, shared_space_id: Optional[str], user_id: str) -> ProjectMemoryTurn:
    """Read both spaces' ``MEMORY.md`` and render the turn's memory blocks.

    Never raises: memory is best-effort context, and a failed read costs the
    turn its block, not the turn. A failed personal lookup leaves the id
    unknown, and the tools look it up again when first used.
    """
    from apis.shared.memory.hydration import render_project_memory
    from apis.shared.memory.service import MemorySpaceService

    start = time.perf_counter()
    turn = ProjectMemoryTurn(project_id=project_id, shared_space_id=shared_space_id, personal_space_id=None)
    try:
        service = MemorySpaceService()
        shared_read = (
            asyncio.to_thread(_read_index, service, shared_space_id, project_id, "shared", user_id)
            if shared_space_id
            else asyncio.sleep(0, result=None)
        )
        shared, personal = await asyncio.gather(
            shared_read,
            asyncio.to_thread(_read_personal, service, project_id, user_id),
            return_exceptions=True,
        )
        if isinstance(shared, BaseException):
            logger.error("Could not read project %s shared memory; continuing", project_id, exc_info=shared)
            shared = None
        personal_text = None
        if isinstance(personal, BaseException):
            logger.error("Could not read project %s personal memory; continuing", project_id, exc_info=personal)
        else:
            turn.personal_space_id, personal_text = personal
        turn.memory_context = render_project_memory(shared, personal_text)
    except Exception:
        logger.error("Could not load project %s memory; continuing without it", project_id, exc_info=True)
    turn.load_ms = (time.perf_counter() - start) * 1000
    return turn


async def await_project_memory(task: "asyncio.Task[ProjectMemoryTurn]") -> ProjectMemoryTurn:
    """Await the turn's memory load and log what it cost the turn.

    ``waitedMs`` is what the turn actually spent here, which is the number the
    TTFT budget cares about; ``loadMs`` is the load itself, most of which ran
    under the knowledge-base search.
    """
    start = time.perf_counter()
    turn = await task
    waited = (time.perf_counter() - start) * 1000
    logger.info(
        "project_memory project=%s loadMs=%.1f waitedMs=%.1f chars=%d shared=%s personal=%s",
        turn.project_id, turn.load_ms, waited, len(turn.memory_context),
        bool(turn.shared_space_id), bool(turn.personal_space_id),
    )
    return turn


def build_project_memory_tools(turn: ProjectMemoryTurn, user: User) -> List[Any]:
    """The harness's scope-addressed memory tools, closed over this turn's spaces and member.

    Memoized: building the four tools (Strands derives a schema per function)
    costs ~1.2 ms, and it runs on every turn before the agent-cache lookup, on
    the path to the first token. The same inputs build equivalent tools, and
    they hold no request state (the member is copied without their token), so
    a member's later turns reuse them.
    """
    return list(
        _project_memory_tools(
            turn.project_id, turn.shared_space_id, turn.personal_space_id, user.user_id, user.email, user.name
        )
    )


@functools.lru_cache(maxsize=512)
def _project_memory_tools(
    project_id: str,
    shared_space_id: Optional[str],
    personal_space_id: Optional[str],
    user_id: str,
    email: str,
    name: str,
) -> Tuple[Any, ...]:
    from agents.builtin_tools.memory_spaces.project_tools import (
        ProjectMemoryScopes,
        make_project_memory_tools,
    )

    member = User(email=email, user_id=user_id, name=name, roles=[])
    scopes = ProjectMemoryScopes.for_member(project_id, shared_space_id, personal_space_id, member)
    return tuple(make_project_memory_tools(scopes))
