"""What ``GET /sessions/{id}/messages`` replays for handoffs (handoffs spec §4.2–§4.3, PR-0.3).

Two things, both UI only and neither ever part of a model's history:

- **``handoffs``** on a *source* conversation: one view per handoff it sent, for the handoff rows the
  thread shows after ``sourceMessageIndex``. Only handoffs that were sent (they have a target); an
  open, dismissed or abandoned one has no row to show.
- **``spawnedFrom``** on a *target* conversation: where it came from, so its first user bubble can say
  "Context from *{title}*" and link back. The title is the one the context was wrapped with.

Off the turn path: this runs only on the messages read, beside the other side-channel replays.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.feature_flags import handoffs_enabled
from apis.shared.handoff.models import HandoffView
from apis.shared.handoff.store import HandoffStore, get_handoff_store

logger = logging.getLogger(__name__)


async def load_handoff_replay(
    session_id: str,
    user_id: str,
    session_row: Optional[Dict[str, Any]],
    store: Optional[HandoffStore] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(handoffs, spawnedFrom)`` for one conversation, owner-scoped. Never raises.

    ``session_row`` is the caller's ``META`` row (already read for its pending interrupts), so the
    target's link costs no extra session read: only a ``GetItem`` for its handoff's source title.
    """
    if not handoffs_enabled():
        return [], None
    store = store or get_handoff_store()
    try:
        rows, spawned = await asyncio.gather(
            asyncio.to_thread(store.list_for_session, user_id, session_id),
            asyncio.to_thread(_spawned_from, store, user_id, session_row),
        )
    except Exception:  # noqa: BLE001 - a replay that fails costs only the rows, never the thread
        logger.warning("Handoff replay failed", exc_info=True)
        return [], None
    views = [
        HandoffView.from_row(row).model_dump(by_alias=True, exclude_none=True)
        for row in rows
        if row.target_session_id and row.kind != "send_back"
    ]
    return views, spawned


def _spawned_from(
    store: HandoffStore, user_id: str, session_row: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    spawned = (session_row or {}).get("spawnedFrom")
    if not isinstance(spawned, dict):
        return None
    source_id = spawned.get("sessionId")
    handoff_id = spawned.get("handoffId")
    if not isinstance(source_id, str) or not isinstance(handoff_id, str):
        return None
    row = store.get(user_id, handoff_id)
    return {
        "sessionId": source_id,
        "handoffId": handoff_id,
        "sourceTitle": row.source_title if row is not None else "",
        "agentName": row.agent_name if row is not None else "",
    }


__all__ = ["load_handoff_replay"]
