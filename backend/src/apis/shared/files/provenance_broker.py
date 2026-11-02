"""Hand-off of source-provenance verdicts from the tool to the SSE stream.

``verify_sources`` runs inside the agent's event loop, which has no way to
write to the SSE stream: a tool returns a result, and that result is the
Converse payload. The rich verdict — quote, locator, precision, method — must
*not* go there (``docs/specs/source-provenance-verification.md`` §8), so it is
parked here and drained by ``stream_coordinator`` on the same pass that drains
tool-batch summaries.

Keyed on ``(user_id, session_id)``, never on the session alone. One container
serves many turns, and a session id alone does not name a conversation —
``(session, user)`` does (CLAUDE.md). Keying on the session would let two users
who happen to share a session id drain each other's quoted document text.

Drain-on-read, and the queue is deleted when it empties: a turn that is
cancelled between the tool call and the next drain leaves nothing behind, and
the verdict still survives in its ``SRCV#`` row, which is what a reload reads.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: Pending payloads per conversation. Bounded per key so a model that calls the
#: tool in a loop cannot grow this without limit if nothing is draining.
_MAX_PENDING_PER_KEY = 32

_pending: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
_lock = threading.Lock()


def queue_provenance(user_id: str, session_id: str, payload: Dict[str, Any]) -> None:
    """Park one call's verdicts for the stream to pick up."""
    if not user_id or not session_id or not payload:
        return
    key = (user_id, session_id)
    with _lock:
        queue = _pending.setdefault(key, [])
        if len(queue) >= _MAX_PENDING_PER_KEY:
            # Drop the oldest rather than the newest: the most recent verdicts
            # are the ones the user is still looking at, and every one of them
            # is persisted regardless.
            queue.pop(0)
        queue.append(payload)


def take_provenance(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Every payload queued for this conversation, removing them."""
    if not user_id or not session_id:
        return []
    with _lock:
        return _pending.pop((user_id, session_id), [])


def discard_provenance(user_id: str, session_id: str) -> None:
    """Drop anything still queued, so a dropped stream leaks nothing."""
    with _lock:
        _pending.pop((user_id, session_id), None)


__all__ = ["discard_provenance", "queue_provenance", "take_provenance"]
