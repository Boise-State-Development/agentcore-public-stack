"""Archiving the turn that just finished, from the stream coordinator.

Called once per turn, after ``done`` has been sent. Everything that touches the
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
from apis.shared.conversation_archive.store import schedule, write_turns
from apis.shared.feature_flags import conversation_index_enabled
from apis.shared.sessions.preview import is_preview_session

logger = logging.getLogger(__name__)


def schedule_turn_archive(
    *,
    messages: Sequence[Mapping[str, Any]],
    user_id: str,
    session_id: str,
    last_message_index: Optional[int],
    turn_first_index: Optional[int],
    original_message: Optional[str],
    project_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
) -> bool:
    """Queue the archive write for the turn ending at ``messages[-1]``.

    Args:
        messages: The agent's conversation after the turn (``agent.messages``).
            Only its tail is read; compaction trims the head, never the tail.
        last_message_index: Global index of ``messages[-1]``, the turn's last
            assistant message (the coordinator's ``message_id``). Maps list
            positions onto the ``msg-{session}-{index}`` ids the SPA anchors.
        turn_first_index: Global index of the first message THIS request
            appended (the coordinator's ``initial_message_count``). When the
            turn began in an earlier request — a resume after an interrupt, a
            continuation — the user's message is older than that, and its
            stored text may be the augmented prompt, so the clean
            ``displayText`` is looked up instead.
        original_message: What the user typed, when the prompt sent to the
            model was modified (RAG context, attachments). Preferred over the
            message's own text.

    Returns:
        True if a write was queued. Never raises.
    """
    try:
        if not conversation_index_enabled() or is_preview_session(session_id):
            return False
        if last_message_index is None or not messages:
            return False
        start = last_turn_start(messages)
        if start is None:
            return False
        turn_index = last_message_index - (len(messages) - 1 - start)
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
        needs_display_text = not original_message and turn_first_index is not None and turn_index < turn_first_index

        async def job() -> None:
            final = turn
            if needs_display_text:
                from apis.shared.sessions.metadata import get_user_display_text

                display_text = await asyncio.to_thread(get_user_display_text, session_id, user_id, turn_index)
                if display_text:
                    final = with_user_text(turn, display_text)
            await write_turns([final])

        schedule(job)
        return True
    except Exception:  # noqa: BLE001 — archiving must never affect the turn
        logger.warning("Conversation archive scheduling failed", exc_info=True)
        return False
