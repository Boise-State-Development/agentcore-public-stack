"""Suggested handoffs: the row ``suggest_handoff`` writes and the chip event it queues (§4.4, PR-0.5).

The model offers a separate conversation for a piece of work without stopping its turn. This
module holds everything but the tool's Strands wrapper (``agents.builtin_tools.suggest_handoff``):

- ``record_suggestion`` validates the arguments, applies the limits, writes an ``open``
  ``HANDOFF#`` row with ``kind: "suggested"`` and parks the chip's event.
- ``queue_suggestion`` / ``take_suggestions`` hand that event to the stream coordinator, which
  drains it after the tool's ``tool_result`` (the ``provenance_broker`` pattern), because a tool
  running inside Strands' event loop has no route to the SSE stream.

Keyed on ``(user_id, session_id)``, never on the session alone: a session id does not name a
conversation (CLAUDE.md), and a chip carries the user's own words.

**Nothing here is on the first-token path.** It runs only when the model calls the tool.

The Agent is **not** resolved here. The model names one in free text (an ``enum`` of the user's
Agents would change ``toolConfig`` whenever a pin changes), and the SPA matches that name against
the Agents the user can ``@``-mention, which is the list it already holds. The pin list lives in
app-api, which the Runtime may not import, so matching there would mean a second copy of it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.handoff.models import (
    SUGGESTION_AGENT_HINT_MAX_CHARS,
    SUGGESTION_PROMPT_MAX_CHARS,
    SUGGESTION_TITLE_MAX_CHARS,
    SUGGESTION_TLDR_MAX_CHARS,
    Handoff,
)
from apis.shared.handoff.store import HandoffStore, HandoffStoreUnavailable, get_handoff_store
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

#: Spec §4.4: one chip a turn, three a conversation. Past either, the tool answers with
#: ``LIMIT_REACHED`` rather than failing, so the model mentions the idea in prose instead.
MAX_SUGGESTIONS_PER_TURN = 1
MAX_SUGGESTIONS_PER_SESSION = 3

#: What the model reads back. Short on purpose: it is a tool result, so it is prompt text for the
#: rest of the conversation.
SHOWN = "Suggestion shown to the user."
LIMIT_REACHED = "Suggestion limit reached; mention it in your reply instead."
UNAVAILABLE = "Suggestions aren't available right now; mention it in your reply instead."

#: Pending chip events per conversation. Bounded so a stream that never drains can't grow it.
_MAX_PENDING_PER_KEY = 8

_pending: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
_lock = threading.Lock()


class SuggestionRefused(ValueError):
    """The model's arguments can't make a chip. The message is what the model is told."""


@dataclass(frozen=True)
class SuggestionArgs:
    title: str
    tldr: str
    prompt: str
    agent_hint: Optional[str]


def normalize_suggestion(title: Any, tldr: Any, prompt: Any, agent: Any = None) -> SuggestionArgs:
    """Trim and bound the arguments. Raises ``SuggestionRefused`` on a missing field or a long prompt.

    Title and reason are display text, so an over-long one is cut; the prompt is the new
    conversation's first message, so cutting it would send something the model never wrote.
    """
    title_text = _text(title)
    tldr_text = _text(tldr)
    prompt_text = _text(prompt)
    if not title_text or not tldr_text or not prompt_text:
        raise SuggestionRefused("title, tldr and prompt are all required.")
    if len(prompt_text) > SUGGESTION_PROMPT_MAX_CHARS:
        raise SuggestionRefused(
            f"prompt is {len(prompt_text)} characters; keep it under {SUGGESTION_PROMPT_MAX_CHARS}."
        )
    hint = _text(agent)[:SUGGESTION_AGENT_HINT_MAX_CHARS] or None
    return SuggestionArgs(
        title=_one_line(title_text)[:SUGGESTION_TITLE_MAX_CHARS],
        tldr=_one_line(tldr_text)[:SUGGESTION_TLDR_MAX_CHARS],
        prompt=prompt_text,
        agent_hint=hint,
    )


def record_suggestion(
    *,
    user_id: str,
    session_id: str,
    tool_use_id: str,
    args: SuggestionArgs,
    turn_count: int,
    store: Optional[HandoffStore] = None,
) -> str:
    """Write the suggestion's row and park its chip event. Returns what the model is told.

    ``turn_count`` is how many suggestions this turn has already made (the tool keeps it in the
    invocation state, which lives for exactly one turn). The conversation's count is read from its
    rows, so it survives a cold container and counts a dismissed chip too: a suggestion the user
    turned down still spent one of the three.
    """
    if turn_count >= MAX_SUGGESTIONS_PER_TURN:
        emit_handoff_metric("HandoffSuggestionLimited", "suggested", session_id=session_id, detail="turn")
        return LIMIT_REACHED
    store = store or get_handoff_store()
    if not store.enabled:
        return UNAVAILABLE
    existing = sum(1 for row in store.list_for_session(user_id, session_id) if row.kind == "suggested")
    if existing >= MAX_SUGGESTIONS_PER_SESSION:
        emit_handoff_metric("HandoffSuggestionLimited", "suggested", session_id=session_id, detail="session")
        return LIMIT_REACHED

    now = utc_now_iso()
    row = Handoff(
        handoff_id=str(uuid.uuid4()),
        user_id=user_id,
        session_id=session_id,
        kind="suggested",
        status="open",
        # No Agent until Start: the SPA resolves the hint, or the user picks one.
        agent_id="",
        request=args.prompt,
        source_message_index=-1,
        created_at=now,
        updated_at=now,
        title=args.title,
        tldr=args.tldr,
        agent_hint=args.agent_hint,
        tool_use_id=tool_use_id or None,
    )
    try:
        store.create(row)
    except HandoffStoreUnavailable:
        return UNAVAILABLE
    except Exception:  # noqa: BLE001 - a lost chip is never worth failing the turn's tool call
        logger.warning("suggest_handoff: row write failed", exc_info=True)
        return UNAVAILABLE

    queue_suggestion(user_id, session_id, suggestion_event(row))
    emit_handoff_metric("HandoffCreated", "suggested", session_id=session_id)
    return SHOWN


def suggestion_event(row: Handoff) -> Dict[str, Any]:
    """The ``handoff_suggested`` payload for a row, minus ``type``. Never the prompt (§4.4).

    The prompt is fetched on Start, so a long one is never streamed for a chip the user dismisses.
    """
    event: Dict[str, Any] = {
        "sessionId": row.session_id,
        "handoffId": row.handoff_id,
        "toolUseId": row.tool_use_id or "",
        "title": row.title or "",
        "tldr": row.tldr or "",
    }
    if row.agent_hint:
        event["agentName"] = row.agent_hint
    return event


def queue_suggestion(user_id: str, session_id: str, payload: Dict[str, Any]) -> None:
    """Park one chip's event for the stream to pick up."""
    if not user_id or not session_id or not payload:
        return
    with _lock:
        queue = _pending.setdefault((user_id, session_id), [])
        if len(queue) >= _MAX_PENDING_PER_KEY:
            queue.pop(0)
        queue.append(payload)


def take_suggestions(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Every chip event queued for this conversation, removing them."""
    if not user_id or not session_id:
        return []
    with _lock:
        return _pending.pop((user_id, session_id), [])


def discard_suggestions(user_id: str, session_id: str) -> None:
    """Drop anything still queued. The row survives, and a reload replays the chip from it."""
    with _lock:
        _pending.pop((user_id, session_id), None)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one_line(text: str) -> str:
    return " ".join(text.split())


__all__ = [
    "LIMIT_REACHED",
    "MAX_SUGGESTIONS_PER_SESSION",
    "MAX_SUGGESTIONS_PER_TURN",
    "SHOWN",
    "SuggestionArgs",
    "SuggestionRefused",
    "UNAVAILABLE",
    "discard_suggestions",
    "normalize_suggestion",
    "queue_suggestion",
    "record_suggestion",
    "suggestion_event",
    "take_suggestions",
]
