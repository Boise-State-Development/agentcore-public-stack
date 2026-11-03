"""Rewind a conversation to one of the user's earlier messages.

``docs/specs/conversation-rewind-and-fork.md`` §4–§5. The rewind is a single
conditional write to the session's metadata row: a new hidden span, the
compaction rules for a cut, and the removal of every breadcrumb that belonged
to the turns now hidden. Nothing in AgentCore Memory is deleted. The Runtime
applies the cut on its next turn: in place on a warm agent, so the retained
prefix is byte-identical and still in the prompt cache, or by filtering at
restore on a cold one.

Runs in app-api, off the turn path, under the session's single-flight lease
so it can never race a turn that is streaming.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apis.shared.aws_clients import get_dynamodb_table
from apis.shared.sessions.messages import list_stored_messages
from apis.shared.sessions.metadata import _get_session_by_gsi, get_user_display_text
from apis.shared.sessions.rewind import (
    REWIND_ATTRIBUTE,
    RewindState,
    is_rewind_target,
    parse_message_index,
)
from apis.shared.sessions.session_lease import (
    SessionBusyError,
    acquire_session_lease,
    release_session_lease,
)

logger = logging.getLogger(__name__)

# The marker the send path appends to a user message's stored text when files
# were attached (the SPA strips the same one when it renders a reloaded turn).
_ATTACHED_FILES_PATTERN = re.compile(r"\n\n\[Attached files: [^\]]+\]$")

# Breadcrumbs that describe the session's LATEST turn. Every rewind hides the
# latest turn (a span always runs to the end), so all of them go with it: a
# paused turn that no longer exists cannot be resumed, and an "interrupted"
# or "Continue" affordance would point at a reply the user cannot see.
_LATEST_TURN_ATTRIBUTES = (
    "pendingInterrupts",
    "pausedTurn",
    "lastTurnContinuable",
    "lastTurnInterrupted",
    "lastTurnInterruptReason",
    "lastTurnInterruptedAt",
)


class RewindRefused(Exception):
    """The rewind cannot be applied. ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class RewindResult:
    next_message_index: int
    prefill_text: str
    rewound_message_ids: List[str]
    epoch: int


def _text_of(message: Dict[str, Any]) -> str:
    parts = [
        block["text"]
        for block in message.get("content") or []
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "".join(parts)


def _prefill_text(session_id: str, user_id: str, index: int, message: Dict[str, Any]) -> str:
    """What the user typed: the stored ``displayText`` when the sent prompt was
    augmented, else the stored text without the attachment marker."""
    display = get_user_display_text(session_id, user_id, index)
    if display:
        return display
    return _ATTACHED_FILES_PATTERN.sub("", _text_of(message)).strip()


def _compaction_after_cut(compaction: Optional[Dict[str, Any]], logical_cut: int) -> Optional[Dict[str, Any]]:
    """The compaction map after a cut at logical index ``logical_cut`` (spec §5).

    ``None`` when there is nothing to change. Raises ``RewindRefused`` for a cut
    the summary already covers.
    """
    if not isinstance(compaction, dict):
        return None
    checkpoint = int(compaction.get("checkpoint") or 0)
    if checkpoint > 0 and logical_cut <= checkpoint:
        # The summary stands in for everything up to the checkpoint and the
        # previous checkpoint is not stored, so there is nothing correct to
        # roll back to.
        raise RewindRefused(409, "That message is part of the conversation's summary and can't be rewound to")
    updated = dict(compaction)
    # Required, not cosmetic: truncation applies strictly below the anchor, so
    # an anchor above the cut would truncate the NEW messages that take the
    # logical indices from the cut onwards. Nothing below the cut changes: it
    # was already below the old anchor.
    anchor = int(compaction.get("truncationAnchor", checkpoint) or 0)
    updated["truncationAnchor"] = min(anchor, logical_cut)
    # A parked cut was computed over the history being hidden.
    for key in ("pendingCheckpoint", "pendingSummary", "pendingHardCeiling", "pendingSince"):
        updated[key] = None
    return updated


def _apply_rewind(session_id: str, user_id: str, physical_cut: int, row: Dict[str, Any], table: Any) -> RewindResult:
    """The rewind itself, under the caller's lease, against the row it read. Blocking."""
    stored = list_stored_messages(session_id, user_id)
    physical_end = len(stored)
    if not 0 <= physical_cut < physical_end:
        raise RewindRefused(404, "Message not found")

    state = RewindState.from_row(row)
    if state.is_hidden(physical_cut):
        raise RewindRefused(409, "That message has already been rewound")
    target = stored[physical_cut]
    if not is_rewind_target(target):
        raise RewindRefused(400, "Only your own messages can be rewound to")

    now = datetime.now(timezone.utc).isoformat()
    new_state = state.with_cut(physical_cut, physical_end, updated_at=now)
    compaction = _compaction_after_cut(row.get("compaction"), new_state.cuts[-1].cut)

    set_parts = ["#rw = :rw"]
    names: Dict[str, str] = {"#rw": REWIND_ATTRIBUTE}
    values: Dict[str, Any] = {":rw": new_state.to_dict()}
    if compaction is not None:
        set_parts.append("#cmp = :cmp")
        names["#cmp"] = "compaction"
        values[":cmp"] = _to_dynamo(compaction)
    remove_parts = []
    for i, attribute in enumerate(_LATEST_TURN_ATTRIBUTES):
        if attribute in row:
            names[f"#r{i}"] = attribute
            remove_parts.append(f"#r{i}")
    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)

    # Optimistic on the epoch: two rewinds racing from two tabs cannot both
    # compute their span against the same starting state.
    if state.epoch:
        condition = "#rw.#ep = :prev"
        names["#ep"] = "epoch"
        values[":prev"] = state.epoch
    else:
        condition = "attribute_not_exists(#rw)"

    from botocore.exceptions import ClientError

    try:
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": row["SK"]},
            UpdateExpression=expression,
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise RewindRefused(409, "The conversation changed; reload and try again") from e
        raise

    rewound_ids = [
        f"msg-{session_id}-{i}" for i in range(physical_cut, physical_end) if not state.is_hidden(i)
    ]
    logger.info(
        "Rewound session to physical %d (logical %d): epoch %d, %d message(s) hidden",
        physical_cut, new_state.cuts[-1].cut, new_state.epoch, len(rewound_ids),
    )
    return RewindResult(
        next_message_index=physical_end,
        prefill_text=_prefill_text(session_id, user_id, physical_cut, target),
        rewound_message_ids=rewound_ids,
        epoch=new_state.epoch,
    )


def _to_dynamo(value: Any) -> Any:
    """Floats back to Decimal: ``_get_session_by_gsi`` hands the row over as floats."""
    from decimal import Decimal

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


async def rewind_session(session_id: str, user_id: str, message_id: str) -> RewindResult:
    """Rewind ``session_id`` to just before ``message_id``.

    Raises ``RewindRefused`` with the status to answer, including 409 while a
    turn is streaming.
    """
    physical_cut = parse_message_index(session_id, message_id)
    if physical_cut is None:
        raise RewindRefused(400, "Not a message in this conversation")
    table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not table_name:
        raise RewindRefused(503, "Session storage is not configured")
    table = get_dynamodb_table(table_name)

    try:
        lease = await acquire_session_lease(session_id, user_id)
    except SessionBusyError as e:
        raise RewindRefused(409, "Wait for the response to finish before rewinding") from e
    try:
        # Read under the lease: the epoch condition below is what makes the
        # write safe, and a turn that finished just before we took the lease
        # has written its compaction state by now.
        row = await _get_session_by_gsi(session_id, user_id, table)
        if not row or not row.get("SK"):
            raise RewindRefused(404, "Session not found")
        return await asyncio.to_thread(_apply_rewind, session_id, user_id, physical_cut, row, table)
    finally:
        await release_session_lease(lease)
