"""Hand-off of extraction rows from the tool to the SSE stream.

``docs/specs/structured-extraction.md`` §9. ``document_extract`` runs inside the
agent's event loop, which has no way to write to the SSE stream: a tool returns
a result, and that result is the Converse payload. The rich rows — quotes,
locators, precision, method, the per-field types — must *not* go there (D8), so
they are parked here and drained by ``stream_coordinator`` on the same pass
that drains tool-batch summaries and provenance verdicts.

WHY A SIBLING OF ``provenance_broker`` AND NOT A GENERALIZATION
---------------------------------------------------------------
The spec left this open to be decided against real code (§9). Having read it:
``provenance_broker`` is already generic — a thread-locked dict of payload
lists keyed on ``(user_id, session_id)`` — and the only thing making it
"provenance" is its name and the event type its drain emits. Generalizing it
would mean giving each queued payload a kind discriminator and making one drain
switch on it, which adds a branch to a path that currently has none and makes
a working feature's drain conditional on something it does not need. The
duplication avoided is a bounded dict and a lock, about forty lines. Two
independent brokers that each do one thing is the cheaper of the two, and it
keeps either feature's kill switch from touching the other's code.

Keyed on ``(user_id, session_id)``, never on the session alone. One container
serves many turns, and a session id alone does not name a conversation —
``(session, user)`` does (CLAUDE.md). Keying on the session would let two users
who happen to share a session id drain each other's quoted document text.

Drain-on-read, and the queue is deleted when it empties: a turn cancelled
between the tool call and the next drain leaves nothing behind, and the rows
still survive in their ``EXTR#`` row, which is what a reload reads.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: Pending payloads per conversation. Bounded per key so a model that calls the
#: tool in a loop cannot grow this without limit if nothing is draining. Lower
#: than the provenance broker's because an extraction payload is much larger —
#: forty fields with their quotes, rather than a handful of claims.
_MAX_PENDING_PER_KEY = 8

_pending: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
_lock = threading.Lock()


def queue_extraction(user_id: str, session_id: str, payload: Dict[str, Any]) -> None:
    """Park one run's rows for the stream to pick up."""
    if not user_id or not session_id or not payload:
        return
    key = (user_id, session_id)
    with _lock:
        queue = _pending.setdefault(key, [])
        if len(queue) >= _MAX_PENDING_PER_KEY:
            # Drop the oldest rather than the newest: the most recent rows are
            # the ones the user is still looking at, and every run is persisted
            # regardless, so a dropped payload costs the live view only.
            queue.pop(0)
        queue.append(payload)


def take_extractions(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Every payload queued for this conversation, removing them."""
    if not user_id or not session_id:
        return []
    with _lock:
        return _pending.pop((user_id, session_id), [])


def discard_extractions(user_id: str, session_id: str) -> None:
    """Drop anything still queued, so a dropped stream leaks nothing."""
    with _lock:
        _pending.pop((user_id, session_id), None)


__all__ = ["discard_extractions", "queue_extraction", "take_extractions"]
