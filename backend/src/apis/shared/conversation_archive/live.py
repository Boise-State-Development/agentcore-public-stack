"""Archiving the turn that just finished, from the stream coordinator.

Called once per turn, after ``done`` has been sent (or from the interruption
arm, once a stopped turn's partial is persisted). Everything that touches the
network runs in a background task; the synchronous part only slices the
agent's message list, so it adds no measurable time to the end of the stream
and none at all before the first token.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from apis.shared.conversation_archive.documents import (
    build_turn,
    last_turn_start,
    message_text,
    with_user_text,
)
from apis.shared.conversation_archive.store import schedule, write_turn_guarded
from apis.shared.feature_flags import conversation_index_enabled
from apis.shared.sessions.preview import is_preview_session

logger = logging.getLogger(__name__)


def tail_anchor(messages: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The message at the tail of ``messages`` before a request appends to it.

    Captured by the coordinator just before the agent runs and handed back to
    :func:`appended_since` after it, so the archive counts what this request
    added by identity rather than by length: the sliding window and compaction
    trim the list's head mid-turn, never its tail.
    """
    return messages[-1] if messages else None


def appended_since(messages: Sequence[Mapping[str, Any]], anchor: Optional[Mapping[str, Any]]) -> Optional[int]:
    """How many messages sit after ``anchor`` at the tail of ``messages``.

    None when the anchor is gone (the list was replaced wholesale), because
    the turn can then no longer be placed. Never raises: the interruption arms
    call it while the request is being torn down.
    """
    try:
        if anchor is None:
            return len(messages)
        for pos in range(len(messages) - 1, -1, -1):
            if messages[pos] is anchor:
                return len(messages) - 1 - pos
    except Exception:  # noqa: BLE001
        logger.warning("Could not count this request's messages for the archive", exc_info=True)
    return None


def schedule_turn_archive(
    *,
    messages: Sequence[Mapping[str, Any]],
    user_id: str,
    session_id: str,
    turn_first_index: Optional[int],
    appended_count: Optional[int],
    original_message: Optional[str],
    project_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
) -> bool:
    """Queue the archive write for the turn ending at ``messages[-1]``.

    Args:
        messages: The agent's conversation after the turn (``agent.messages``).
            Only its tail is read; compaction trims the head, never the tail.
        turn_first_index: Global index of the first message THIS request
            appended (the coordinator's ``initial_message_count``, the stored
            message count before the turn). Maps list positions onto the
            ``msg-{session}-{index}`` ids the SPA anchors.
        appended_count: How many messages at the tail of ``messages`` this
            request appended (:func:`appended_since`). Counted, not assumed:
            a turn may end on a tool result with no assistant message after
            it (a resume the model closed without prose), so the tail's role
            says nothing about its index. When the turn began in an earlier
            request (a resume after an interrupt, a continuation), its user
            message precedes those, and its stored text may be the augmented
            prompt, so the clean ``displayText`` is looked up instead. Zero
            means the request changed nothing, so nothing is written (a
            rewrite would only move the turn's ``createdAt``).
        original_message: What the user typed, when the prompt sent to the
            model was modified (RAG context, attachments). Preferred over the
            message's own text.

    The write never replaces another turn's object: a turn this request
    started is written create-only, and a turn that began earlier replaces its
    key only when the object there is the same turn (``put_turn_guarded``).

    Returns:
        True if a write was queued. Never raises.
    """
    try:
        if not conversation_index_enabled() or is_preview_session(session_id):
            return False
        if turn_first_index is None or turn_first_index < 0 or not messages:
            return False
        if appended_count is None or not 0 <= appended_count <= len(messages):
            logger.warning("Conversation archive skipped: this request's messages could not be counted")
            return False
        if appended_count == 0:
            return False
        start = last_turn_start(messages)
        if start is None:
            return False
        first_appended = len(messages) - appended_count
        turn_index = turn_first_index + (start - first_appended)
        if turn_index < 0:
            return False

        # Built now, from a list the next turn will mutate; only the
        # displayText lookup waits for the background task.
        turn = build_turn(
            user_id=user_id,
            session_id=session_id,
            message_index=turn_index,
            user_text=original_message or message_text(messages[start]),
            assistant_messages=list(messages[start + 1 :]),
            created_at=datetime.now(timezone.utc).isoformat(),
            project_id=project_id,
            assistant_id=assistant_id,
        )
        if turn is None:
            return False
        began_earlier = start < first_appended
        needs_display_text = not original_message and began_earlier

        async def job() -> None:
            final = turn
            if needs_display_text:
                from apis.shared.sessions.metadata import get_user_display_text

                display_text = await asyncio.to_thread(get_user_display_text, session_id, user_id, turn_index)
                if display_text:
                    final = with_user_text(turn, display_text)
            await write_turn_guarded(final, expect_existing=began_earlier)

        schedule(job)
        return True
    except Exception:  # noqa: BLE001 — archiving must never affect the turn
        logger.warning("Conversation archive scheduling failed", exc_info=True)
        return False
