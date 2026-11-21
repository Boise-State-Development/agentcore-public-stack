"""What ``GET /sessions/{id}/messages`` replays for handoffs (handoffs spec §4.2–§4.3, PR-0.3).

Two things, both UI only and neither ever part of a model's history:

- **``handoffs``** on a *source* conversation: one view per handoff it sent, for the handoff rows the
  thread shows after ``sourceMessageIndex``. Only handoffs that were sent (they have a target); an
  open, dismissed or abandoned one has no row to show. The exception is a **suggestion** still
  ``open`` (§4.4, PR-0.5): its chip, placed under the message holding its ``toolUseId``, with
  ``title``, ``tldr`` and the model's ``agentHint``.
  A ``needs_input`` handoff also carries ``relayedPrompts``: what its target is waiting on, so the
  source can answer it under the row (PR-0.3c, ``apis.shared.handoff.relay``).
- **``workflowRuns``** on a *source* conversation: its workflow runs (``docs/specs/workflow-runs.md``
  §9), each with its steps' handoff views, including steps not sent yet. Only while
  ``WORKFLOW_RUNS_ENABLED`` is on.
- **``spawnedFrom``** on a *target* conversation: where it came from, so its first user bubble can say
  "Context from *{title}*" and link back. The title is the one the context was wrapped with.

Off the turn path: this runs only on the messages read, beside the other side-channel replays.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.feature_flags import handoffs_enabled, workflow_runs_enabled
from apis.shared.handoff.delivery import DeliveryStore, DeliveryView, get_delivery_store
from apis.shared.handoff.models import Handoff, HandoffView
from apis.shared.handoff.relay import MetaLoader, load_relayed_prompts
from apis.shared.handoff.runs import WorkflowRunStore, WorkflowRunView, get_workflow_run_store
from apis.shared.handoff.store import HandoffStore, get_handoff_store

logger = logging.getLogger(__name__)


async def load_handoff_replay(
    session_id: str,
    user_id: str,
    session_row: Optional[Dict[str, Any]],
    store: Optional[HandoffStore] = None,
    meta_loader: Optional[MetaLoader] = None,
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
    views = [HandoffView.from_row(row) for row in rows if _replayed(row)]
    for view in views:
        if view.kind == "suggested" and not view.target_session_id:
            # A chip's prompt comes with Start (POST .../start), never with the thread: most chips
            # are dismissed or ignored, and the prompt can be 4,000 characters.
            view.request = ""
    await load_relayed_prompts(views, user_id, meta_loader=meta_loader)
    return [view.model_dump(by_alias=True, exclude_none=True) for view in views], spawned


async def load_delivery_replay(
    session_id: str, user_id: str, store: Optional[DeliveryStore] = None
) -> List[Dict[str, Any]]:
    """This conversation's pending deliveries, oldest first, as composer cards (§4.8). Never raises.

    Only ``pending``: an attached delivery's block is already in the user message that carried it,
    which is what a reloaded bubble renders, and a dismissed one is gone.
    """
    if not handoffs_enabled():
        return []
    store = store or get_delivery_store()
    try:
        rows = await asyncio.to_thread(store.list_pending, user_id, session_id)
    except Exception:  # noqa: BLE001 - a replay that fails costs only the cards, never the thread
        logger.warning("Delivery replay failed", exc_info=True)
        return []
    return [DeliveryView.from_row(row).model_dump(by_alias=True, exclude_none=True) for row in rows]


async def load_workflow_run_replay(
    session_id: str,
    user_id: str,
    run_store: Optional[WorkflowRunStore] = None,
    handoff_store: Optional[HandoffStore] = None,
) -> List[Dict[str, Any]]:
    """This conversation's workflow runs, oldest first, each with its steps' rows (spec §9). Never raises.

    Every run, whatever its status: the SPA decides what an open, dismissed or abandoned run shows.
    A conversation with no runs costs one query; only one with runs reads its handoff rows too, for
    the steps' current views. Those views carry no ``relayedPrompts``: a step that was sent is in
    ``handoffs`` as well, with them. Nothing while ``WORKFLOW_RUNS_ENABLED`` is off.
    """
    if not workflow_runs_enabled():
        return []
    run_store = run_store or get_workflow_run_store()
    handoff_store = handoff_store or get_handoff_store()
    try:
        runs = await asyncio.to_thread(run_store.list_for_session, user_id, session_id)
        if not runs:
            return []
        rows = await asyncio.to_thread(handoff_store.list_for_session, user_id, session_id)
    except Exception:  # noqa: BLE001 - a replay that fails costs only the runs, never the thread
        logger.warning("Workflow run replay failed", exc_info=True)
        return []
    views = {row.handoff_id: HandoffView.from_row(row) for row in rows if row.run_id}
    return [
        WorkflowRunView.from_row(run, views).model_dump(by_alias=True, exclude_none=True) for run in runs
    ]


def _replayed(row: Handoff) -> bool:
    """A handoff that was sent (its row), or a suggestion still waiting on the user (its chip)."""
    if row.kind in ("send_back", "prompt"):
        # A prompt step has no conversation to link to; its run's replay carries it.
        return False
    if row.target_session_id:
        return True
    return row.kind == "suggested" and row.status == "open"


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


__all__ = ["load_delivery_replay", "load_handoff_replay", "load_workflow_run_replay"]
