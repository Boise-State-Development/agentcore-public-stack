"""Per-conversation app-initiated tool-event broker (MCP Apps PR #5).

When an embedded MCP App calls a server tool (`tools/call` over the
postMessage bridge), the result must surface as `tool_use` / `tool_result`
in the user's conversation thread — even though the call arrives out-of-band
on `POST /mcp-apps/proxy-call`, not on the chat stream.

This broker is the seam the scoping doc's "open implementation question"
calls for. The inference-api app-tool-call dispatch **publishes** synthesized
events here; the `StreamCoordinator` (the live conversation SSE stream)
**subscribes** and interleaves them. Delivery model:

- A subscriber is active (a chat turn is streaming for that session): the
  event is handed to every live subscriber queue — it lands in the thread
  live.
- No subscriber active (the App was used between turns): the event is
  buffered in a small bounded per-session ring. The next stream that
  subscribes drains the ring first, so the card shows when the user's next
  turn opens. (Full reload is covered separately by session persistence.)

Process-global and asyncio-only: the inference-api runtime is a single
event loop, so a module singleton with `asyncio.Queue` subscribers is
sufficient and avoids cross-process coupling. Bounded so a forgotten
session can't leak memory.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Deque, Dict, List, Set

logger = logging.getLogger(__name__)

# Cap buffered events for a session with no active stream. App-initiated
# calls are user-driven clicks, so this is generous; oldest is dropped.
_MAX_PENDING_PER_SESSION = 100


class AppToolEventBroker:
    """Per-`session_id` fan-out of synthesized app-initiated tool events."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, Set["asyncio.Queue[dict]"]] = {}
        self._pending: Dict[str, Deque[dict]] = {}

    def publish(self, session_id: str, event: Dict[str, Any]) -> None:
        """Deliver an event to the session's live stream, or buffer it.

        Never raises into the caller (a proxied tool call must still return
        its result to the App even if nothing is listening).
        """
        if not session_id:
            return
        subs = self._subscribers.get(session_id)
        if subs:
            for q in list(subs):
                try:
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001 - best effort fan-out
                    logger.warning(
                        "mcp-apps broker: failed to enqueue for an active "
                        "subscriber (session=%s)",
                        session_id,
                    )
            return
        # No live stream — buffer for the next one.
        ring = self._pending.setdefault(session_id, deque())
        ring.append(event)
        while len(ring) > _MAX_PENDING_PER_SESSION:
            ring.popleft()

    def add_subscriber(self, session_id: str) -> "asyncio.Queue[dict]":
        """Register an active stream as a subscriber and return its queue.

        Any events buffered while no stream was active are moved into the
        new queue so they surface on this turn. Caller MUST pair this with
        `remove_subscriber` (a generator `finally`); `subscribe` is the
        context-manager wrapper that does so automatically.
        """
        q: "asyncio.Queue[dict]" = asyncio.Queue()
        if session_id:
            self._subscribers.setdefault(session_id, set()).add(q)
            pending = self._pending.pop(session_id, None)
            if pending:
                for event in pending:
                    q.put_nowait(event)
        return q

    def remove_subscriber(
        self, session_id: str, q: "asyncio.Queue[dict]"
    ) -> None:
        subs = self._subscribers.get(session_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(session_id, None)

    @asynccontextmanager
    async def subscribe(
        self, session_id: str
    ) -> AsyncIterator["asyncio.Queue[dict]"]:
        """Context-manager form of add/remove_subscriber (used by tests)."""
        q = self.add_subscriber(session_id)
        try:
            yield q
        finally:
            self.remove_subscriber(session_id, q)

    def drain(self, queue: "asyncio.Queue[dict]") -> List[dict]:
        """Non-blocking pop of everything currently in a subscriber queue.

        The stream loop calls this between model events to interleave any
        app-initiated events without ever blocking on the agent stream.
        """
        out: List[dict] = []
        while True:
            try:
                out.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out


_broker: AppToolEventBroker | None = None


def get_app_tool_event_broker() -> AppToolEventBroker:
    """Get or create the process-global broker."""
    global _broker
    if _broker is None:
        _broker = AppToolEventBroker()
    return _broker
