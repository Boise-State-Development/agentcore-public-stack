"""Finish the calls a dying MCP session leaves behind, so none of them hangs.

Strands' `MCPClient` runs its session on a background thread's event loop
(`_background_task` → `run_until_complete(_async_background_thread())`). Each
tool call is a `run_async` task on that loop that waits on the call *and* on
the session's close future, and on close it still has cleanup to await before
it resolves the caller's future.

When the session dies on its own — the MCP SDK ends it on any non-2xx answer
to a POST, e.g. GitHub's MCP endpoint answering a `tools/call` with 503 —
`_async_background_thread` sets the close future and returns at once, so
`run_until_complete` stops the loop while that call is mid-cleanup. The task is
never scheduled again and the caller's future never resolves. With the
`cancel_signal` every agent passes (`MCPAgentTool.stream` → `agent._cancel_signal`)
it loses that race every time (strands-agents 1.57.2, mcp 1.30.0): the
model-driven call that hit the 503 hung its turn instead of failing it.

Running the loop on until those tasks finish lets each one reach its own
"Connection to the MCP server was closed" and resolve its caller. A normal
`stop()` leaves no task behind, so this costs it nothing; the wait is bounded
so a task that never finishes cannot hold the thread (and `stop()`'s join).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Each stranded call's own cleanup is bounded at ~1s per step by Strands.
_RELEASE_TIMEOUT_SECONDS = 3.0


def release_stranded_calls(loop: Any) -> None:
    """Run `loop` until the tasks still pending on it finish, or time out."""
    if loop is None or loop.is_closed():
        return
    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        if not pending:
            return
        loop.run_until_complete(asyncio.wait(pending, timeout=_RELEASE_TIMEOUT_SECONDS))
        left = sum(1 for task in pending if not task.done())
        if left:
            logger.warning(
                "%d MCP call(s) still unresolved %.0fs after their session ended",
                left,
                _RELEASE_TIMEOUT_SECONDS,
            )
    except Exception:  # noqa: BLE001 - never let cleanup take the thread down noisily
        logger.warning("could not release calls left by an ended MCP session", exc_info=True)


class ReleasesCallsOnSessionEnd:
    """`MCPClient` mixin: no call outlives its session unresolved."""

    def _background_task(self) -> None:
        try:
            super()._background_task()  # type: ignore[misc]
        finally:
            release_stranded_calls(getattr(self, "_background_thread_event_loop", None))
