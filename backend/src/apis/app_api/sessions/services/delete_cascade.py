"""What deleting a conversation removes, in one place.

``SessionService.delete_session`` only soft-deletes the row. Everything else a
conversation left behind goes in a cascade of five cleanups:

1. AgentCore Memory: the session's short-term events and its summary records.
2. The session's uploaded files (S3 objects, metadata rows, quota).
3. Share snapshots, so share links stop working.
4. Artifact shares from the session, scoped to the owner.
5. The session's archived turns. Each S3 deletion raises *Object Deleted*,
   which is how the conversation-search index drops the turn.

Three callers run it: the single delete route, the bulk delete route, and the
retention pruner (``apis/app_api/conversation_index/session_pruner.py``). They
all go through :class:`SessionDeleteCascade` so a sixth cleanup added later
cannot reach two of them and miss the third.

The routes queue the steps as FastAPI background tasks so the response does
not wait on them. The pruner awaits them one at a time, because it is throttled
work with nobody waiting and it should know when a session is fully gone before
it moves to the next.

Collaborators are passed in rather than looked up here, so the routes keep
resolving them from their own module (which is what their tests patch).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionDeleteCascade:
    """The five cleanups that follow a soft delete. See the module docstring."""

    session_service: Any
    share_service: Any
    artifact_share_service: Any
    delete_archive: Callable[[str, str], int]

    def steps(self, user_id: str, session_id: str) -> List[Tuple[str, Callable[..., Any], Tuple[Any, ...]]]:
        """``(name, callable, args)`` for each cleanup, in order."""
        return [
            ("agentcore_memory", self.session_service.delete_agentcore_memory, (session_id, user_id)),
            ("session_files", self.session_service.delete_session_files, (session_id,)),
            ("share_snapshots", self.share_service.delete_shares_for_session, (session_id,)),
            # SessionIndex is not user-partitioned, so the owner id is what
            # keeps this off other users' shares.
            ("artifact_shares", self.artifact_share_service.delete_for_session, (session_id, user_id)),
            # Not gated on the index flag: a deployment that turned indexing off
            # must still remove what it already wrote.
            ("conversation_archive", self.delete_archive, (user_id, session_id)),
        ]

    def queue(self, background_tasks: Any, user_id: str, session_id: str) -> None:
        """Queue every cleanup as a FastAPI background task (fire-and-forget)."""
        for _, fn, args in self.steps(user_id, session_id):
            background_tasks.add_task(fn, *args)

    async def run(self, user_id: str, session_id: str) -> List[str]:
        """Run every cleanup now, in order; returns the names of any that raised.

        Each step is isolated: one failing (they are all best-effort and most
        already swallow their own errors) does not stop the rest. Synchronous
        steps run in a worker thread, because ``delete_session_files`` drives
        its own event loop and cannot run on a thread that already has one.
        """
        failed: List[str] = []
        for name, fn, args in self.steps(user_id, session_id):
            try:
                if inspect.iscoroutinefunction(fn):
                    await fn(*args)
                else:
                    await asyncio.to_thread(fn, *args)
            except Exception:  # noqa: BLE001 - reported to the caller by name
                logger.warning("Session delete cascade step %s failed", name, exc_info=True)
                failed.append(name)
        return failed


def default_cascade(session_service: Any) -> SessionDeleteCascade:
    """The cascade wired to the real services, for callers outside the routes."""
    from apis.app_api.artifacts.service import get_artifact_share_service
    from apis.app_api.shares.service import get_share_service
    from apis.shared.conversation_archive import delete_session_archive

    return SessionDeleteCascade(
        session_service=session_service,
        share_service=get_share_service(),
        artifact_share_service=get_artifact_share_service(),
        delete_archive=delete_session_archive,
    )


__all__ = ["SessionDeleteCascade", "default_cascade"]
