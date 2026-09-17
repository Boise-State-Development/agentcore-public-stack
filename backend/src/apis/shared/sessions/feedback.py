"""Message feedback (thumbs up / down) storage — the ``F#`` row family.

The outcome signal the cost work has been missing (document-context-offload
spec §5 row 7, §6.1 "the outcome signal"; compaction thresholds spec §7.2).
One row per ``(user, session, message)``, content-free by construction: a
``value`` of +1 / -1, a timestamp and an optional reason *code* from
:data:`FEEDBACK_REASONS`. No free text can be stored here — the request
model's ``reason`` is a closed ``Literal`` and this module never accepts a
string outside the tuple.

Schema (``sessions-metadata`` table, beside the ``C#`` / ``D#`` rows — see the
row-family summary in ``apis.shared.sessions.metadata``)::

    PK:      USER#{user_id}
    SK:      F#{session_id}#{message_id}
    GSI_PK:  SESSION#{session_id}      (SessionLookupIndex)
    GSI_SK:  F#{message_id}
    sessionId, messageId, userId, value, reason?, signal, retryMessageId?, updatedAt, ttl

``signal`` is ``"explicit"`` for a thumb. ``docs/specs/response-feedback.md``
§10 adds implicit signals (copy, continue, edit-and-resend, abandonment) to
this same row family under ``signal: "implicit"``; every reader here filters
to explicit rows so that phase needs no backfill and the two are never summed.

``retryMessageId`` is the index of the user message the SPA sent as a
*retry with correction* after a down-thumb (response-feedback spec §7 "the
retry loop", §11 PR-1's consequence). It is a link, never the correction's
text: the profile counts retries and prices the rework from the cost rows
the link points at. A later re-thumb without the field keeps the link.

``messageId`` is the same 0-based index the ``C#`` cost row carries for the
assistant message, so an admin read joins feedback to the call's turn class
(``hasDocuments`` / ``documentDigests`` / ``documentReads``, #1137) on
``(sessionId, messageId)`` with no second lookup. The SK is deterministic, so
a second thumb on the same message *replaces* the first (``put_item``), and a
``DELETE`` removes it — one thumb per (user, message) by key design.

Session-row rollups: ``thumbsUp`` / ``thumbsDown`` are ``ADD``ed on the ``S#``
row like ``toolCallCount``, only while ``COST_DIAGNOSTICS_ENABLED`` (an absent
attribute reads "not tracked", never 0). A replace adjusts both counters so
the rollup always equals the count of live ``F#`` rows written while the
diagnostics were on.

Everything is gated by :func:`apis.shared.feature_flags.response_feedback_enabled`
at the route; this module is deliberately flag-free so a backfill or a test
can drive it directly.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .models import FEEDBACK_REASONS, MessageFeedback
from .preview import is_preview_session

logger = logging.getLogger(__name__)

#: Retention, matching the ``C#`` cost rows the feedback joins to.
FEEDBACK_TTL_DAYS = 365


class SessionNotOwned(Exception):
    """The session has no metadata row for this user (missing or another user's)."""


def _table_name() -> str:
    name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not name:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")
    return name


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(_table_name())


def feedback_sk(session_id: str, message_id: int) -> str:
    return f"F#{session_id}#{message_id}"


def _keys(user_id: str, session_id: str, message_id: int) -> Dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": feedback_sk(session_id, message_id)}


def _to_model(item: Dict[str, Any]) -> MessageFeedback:
    retry = item.get("retryMessageId")
    return MessageFeedback(
        value=int(item.get("value", 0)),
        reason=item.get("reason") if item.get("reason") in FEEDBACK_REASONS else None,
        retry_message_id=int(retry) if retry is not None else None,
        updated_at=str(item.get("updatedAt", "")),
    )


async def _owned_session_sk(session_id: str, user_id: str, table) -> str:
    """The session row's SK, or raise :class:`SessionNotOwned`."""
    from .metadata import _get_session_by_gsi

    existing = await _get_session_by_gsi(session_id, user_id, table)
    if not existing or not existing.get("SK"):
        raise SessionNotOwned(session_id)
    return existing["SK"]


def _bump_rollups(table, user_id: str, session_sk: str, *, up: int, down: int) -> None:
    """``ADD thumbsUp :up, thumbsDown :down`` on the session row, only while
    the content-free diagnostics are on. Best-effort: a failed bump never
    fails the user's click — the rows stay authoritative and the profile
    prefers them when present."""
    from apis.shared.feature_flags import cost_diagnostics_enabled

    if not cost_diagnostics_enabled():
        return
    try:
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": session_sk},
            UpdateExpression="ADD thumbsUp :up, thumbsDown :down",
            ExpressionAttributeValues={":up": int(up), ":down": int(down)},
        )
    except Exception as e:  # noqa: BLE001 - rollup drift is tolerable, a lost click is not
        logger.debug("feedback rollup bump failed for %s: %s", session_sk, e)


async def put_message_feedback(
    session_id: str,
    user_id: str,
    message_id: int,
    value: int,
    reason: Optional[str] = None,
    retry_message_id: Optional[int] = None,
) -> MessageFeedback:
    """Write (or replace) this user's thumb on one message.

    An upsert (``update_item``): ``value`` / ``reason`` / ``updatedAt`` are
    replaced on every call, ``retryMessageId`` is set when given and kept
    otherwise, so a user who thumbs again after retrying does not lose the
    link. ``ReturnValues=ALL_OLD`` tells us what a replace replaced, so the
    session rollups move by the delta rather than double-counting.

    Raises :class:`SessionNotOwned` when the session is not this user's, and
    ``ValueError`` on a value outside ``{1, -1}`` or a reason outside
    :data:`FEEDBACK_REASONS` — the route's request model already rejects
    both, this is the storage layer refusing to become a text field.
    """
    if value not in (1, -1):
        raise ValueError("feedback value must be 1 or -1")
    if reason is not None and reason not in FEEDBACK_REASONS:
        raise ValueError("feedback reason must be one of the fixed codes")
    if retry_message_id is not None and (not isinstance(retry_message_id, int) or retry_message_id < 0):
        raise ValueError("retryMessageId must be a non-negative message index")
    if is_preview_session(session_id):
        # Preview sessions persist nothing; echo the thumb so the UI is consistent.
        return MessageFeedback(value=value, reason=reason, retry_message_id=retry_message_id, updated_at=_now())

    table = _table()
    session_sk = await _owned_session_sk(session_id, user_id, table)

    now = _now()
    ttl = int((datetime.now(timezone.utc) + timedelta(days=FEEDBACK_TTL_DAYS)).timestamp())
    sets = {
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": f"F#{message_id}",
        "sessionId": session_id,
        "messageId": int(message_id),
        "userId": user_id,
        "#value": int(value),
        "#signal": "explicit",
        "updatedAt": now,
        "#ttl": ttl,
    }
    if reason:
        sets["reason"] = reason
    if retry_message_id is not None:
        sets["retryMessageId"] = int(retry_message_id)
    names = {"#value": "value", "#signal": "signal", "#ttl": "ttl"}
    values: Dict[str, Any] = {}
    set_parts = []
    for i, (attr, val) in enumerate(sets.items()):
        placeholder = f":v{i}"
        values[placeholder] = val
        set_parts.append(f"{attr} = {placeholder}")
    expression = "SET " + ", ".join(set_parts)
    if not reason:
        expression += " REMOVE reason"

    response = table.update_item(
        Key=_keys(user_id, session_id, message_id),
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_OLD",
    )
    previous = response.get("Attributes") or {}
    previous_value = int(previous.get("value", 0)) if previous else 0
    item: Dict[str, Any] = {
        "value": int(value),
        "reason": reason,
        "retryMessageId": (
            int(retry_message_id) if retry_message_id is not None else previous.get("retryMessageId")
        ),
        "updatedAt": now,
    }

    up = (1 if value == 1 else 0) - (1 if previous_value == 1 else 0)
    down = (1 if value == -1 else 0) - (1 if previous_value == -1 else 0)
    if up or down:
        _bump_rollups(table, user_id, session_sk, up=up, down=down)

    logger.info("👍 feedback stored for message %s in session %s", message_id, session_id)
    return _to_model(item)


async def delete_message_feedback(session_id: str, user_id: str, message_id: int) -> bool:
    """Remove this user's thumb on one message. Returns whether a row existed."""
    if is_preview_session(session_id):
        return False
    table = _table()
    session_sk = await _owned_session_sk(session_id, user_id, table)

    response = table.delete_item(Key=_keys(user_id, session_id, message_id), ReturnValues="ALL_OLD")
    previous = response.get("Attributes") or {}
    if not previous:
        return False
    previous_value = int(previous.get("value", 0))
    _bump_rollups(
        table, user_id, session_sk,
        up=-1 if previous_value == 1 else 0,
        down=-1 if previous_value == -1 else 0,
    )
    return True


def query_session_feedback(table, session_id: str, user_id: Optional[str] = None) -> Dict[str, MessageFeedback]:
    """All ``F#`` rows for a session via ``SessionLookupIndex``, keyed by
    message id (as ``str``, matching the metadata index). With ``user_id``
    set, rows belonging to another user are dropped (the SPA read); the
    admin reader passes ``None`` and gets every user's thumb."""
    from boto3.dynamodb.conditions import Key

    out: Dict[str, MessageFeedback] = {}
    last_key = None
    while True:
        kwargs: Dict[str, Any] = {
            "IndexName": "SessionLookupIndex",
            "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").begins_with("F#"),
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        response = table.query(**kwargs)
        for item in response.get("Items", []):
            if user_id is not None and item.get("userId") != user_id:
                continue
            if not is_explicit(item):
                continue
            raw_id = item.get("messageId")
            try:
                message_id = str(int(raw_id))
            except (TypeError, ValueError):
                continue
            out[message_id] = _to_model(item)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
    return out


def is_explicit(row: Dict[str, Any]) -> bool:
    """A thumb, as opposed to a §10 implicit signal. Rows written before the
    discriminator existed carry none and are explicit by construction."""
    return row.get("signal") in (None, "explicit")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
