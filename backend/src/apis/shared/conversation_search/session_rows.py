"""Conversation search's reads of the caller's own session rows.

Two reads, both confined to the caller's partition (``PK = USER#{caller}``), so
neither can return another user's conversation whatever it is asked for:

* :func:`search_user_sessions_lexical`, the lexical leg
  (``docs/specs/conversation-search.md`` §5): a ``contains()`` match on the
  ``titleLower`` and ``firstPrompt`` attributes over the user's ``S#`` rows.
  The base table rather than ``SessionRecencyIndex``, because archived rows are
  off that index and a user's rows are one partition either way. Scoped to a
  project, it reads ``ProjectSessionIndex`` (one member's tasks in one project)
  instead. Rows written before ``titleLower`` existed match only once the
  backfill (``scripts/backfill_session_search_attributes.py``) has run.
* :func:`get_owned_session_rows`, the text leg's join: every index hit is looked
  up as ``(USER#{caller}, S#{session_id})``, and a hit without a live row is
  dropped. A row under another user's partition is simply not found.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from apis.shared.sessions.search_text import normalize_search_text

logger = logging.getLogger(__name__)

#: At most this many rows are read per lexical search (§5). A user with more
#: sessions than this gets matches from the rows read first; the text leg still
#: covers everything.
LEXICAL_SCAN_CAP = 2_000

#: Rows read per page while scanning. Large pages keep the round trips few.
_PAGE_SIZE = 500

#: ``BatchGetItem`` takes at most 100 keys a call.
_BATCH_GET_MAX_KEYS = 100

#: Unprocessed keys are retried this many times before being given up on.
_BATCH_GET_RETRIES = 3

_PROJECT_SESSION_INDEX = "ProjectSessionIndex"

#: What a result needs from a row, and nothing more.
_PROJECTION = "#sid, #title, #tl, #fp, #lma, #st, #del, #prefs"
_PROJECTION_NAMES = {
    "#sid": "sessionId",
    "#title": "title",
    "#tl": "titleLower",
    "#fp": "firstPrompt",
    "#lma": "lastMessageAt",
    "#st": "status",
    "#del": "deleted",
    "#prefs": "preferences",
}

_SEARCHABLE_STATUSES = ("active", "archived")


@dataclass(frozen=True)
class SessionRow:
    """The parts of a session row a search result shows."""

    session_id: str
    title: str
    title_lower: str
    first_prompt: str
    last_message_at: str
    status: str
    project_id: Optional[str]
    assistant_id: Optional[str]

    @property
    def archived(self) -> bool:
        return self.status == "archived"


def _table():
    from apis.shared.aws_clients import get_dynamodb_table

    name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not name:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")
    return get_dynamodb_table(name)


def _is_live(item: Mapping[str, Any]) -> bool:
    return item.get("status") in _SEARCHABLE_STATUSES and not item.get("deleted")


def _to_row(item: Mapping[str, Any]) -> Optional[SessionRow]:
    """A row a result may show, or None for a deleted, preview or malformed one."""
    from apis.shared.sessions.preview import is_preview_session

    session_id = item.get("sessionId")
    if not isinstance(session_id, str) or not session_id or is_preview_session(session_id):
        return None
    if not _is_live(item):
        return None
    prefs = item.get("preferences") if isinstance(item.get("preferences"), dict) else {}
    return SessionRow(
        session_id=session_id,
        title=str(item.get("title") or ""),
        title_lower=str(item.get("titleLower") or ""),
        first_prompt=str(item.get("firstPrompt") or ""),
        last_message_at=str(item.get("lastMessageAt") or ""),
        status=str(item.get("status")),
        project_id=prefs.get("projectId") or None,
        assistant_id=prefs.get("assistantId") or None,
    )


def _require_user_id(user_id: Any) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("a session search requires the caller's user id")
    return user_id


def _lexical_query(table, user_id: str, needle: str, project_id: Optional[str], scan_cap: int) -> List[SessionRow]:
    from boto3.dynamodb.conditions import Attr, Key

    status_ok = Attr("status").is_in(list(_SEARCHABLE_STATUSES)) & (
        Attr("deleted").not_exists() | Attr("deleted").eq(False)
    )
    text_ok = Attr("titleLower").contains(needle) | Attr("firstPrompt").contains(needle)

    if project_id:
        key = Key("GSI5_PK").eq(f"PROJECT#{project_id}#USER#{user_id}")
        params: Dict[str, Any] = {"IndexName": _PROJECT_SESSION_INDEX}
    else:
        key = Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("S#")
        params = {}
    params.update(
        KeyConditionExpression=key,
        FilterExpression=status_ok & text_ok,
        ProjectionExpression=_PROJECTION,
        ExpressionAttributeNames=dict(_PROJECTION_NAMES),
    )

    rows: List[SessionRow] = []
    scanned = 0
    while scanned < scan_cap:
        params["Limit"] = min(_PAGE_SIZE, scan_cap - scanned)
        response = table.query(**params)
        scanned += int(response.get("ScannedCount", 0))
        for item in response.get("Items", []):
            row = _to_row(item)
            if row is not None:
                rows.append(row)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key
    return rows


async def search_user_sessions_lexical(
    user_id: str,
    query: str,
    *,
    project_id: Optional[str] = None,
    scan_cap: int = LEXICAL_SCAN_CAP,
) -> List[SessionRow]:
    """The caller's live sessions whose title or opening prompt contains the query.

    Archived sessions are included (a result marks them); deleted ones never
    are. Order is the table's; ranking is the merge's job. A query that
    normalizes to nothing matches nothing.
    """
    caller = _require_user_id(user_id)
    needle = normalize_search_text(query)
    if not needle:
        return []
    table = _table()
    return await asyncio.to_thread(_lexical_query, table, caller, needle, project_id or None, scan_cap)


def _batch_get(table, user_id: str, session_ids: List[str]) -> List[Mapping[str, Any]]:
    # The resource's client takes and returns plain Python values (boto3 applies
    # the DynamoDB type conversion on resource-created clients).
    client = table.meta.client
    items: List[Mapping[str, Any]] = []
    for start in range(0, len(session_ids), _BATCH_GET_MAX_KEYS):
        chunk = session_ids[start : start + _BATCH_GET_MAX_KEYS]
        request: Dict[str, Any] = {
            table.name: {
                "Keys": [{"PK": f"USER#{user_id}", "SK": f"S#{sid}"} for sid in chunk],
                "ProjectionExpression": _PROJECTION,
                "ExpressionAttributeNames": dict(_PROJECTION_NAMES),
            }
        }
        for _ in range(_BATCH_GET_RETRIES + 1):
            response = client.batch_get_item(RequestItems=request)
            items.extend(response.get("Responses", {}).get(table.name, []))
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
        else:
            logger.warning(
                "conversation search gave up on %d unprocessed session lookups",
                len(request.get(table.name, {}).get("Keys", [])),
            )
    return items


async def get_owned_session_rows(user_id: str, session_ids: Iterable[str]) -> Dict[str, SessionRow]:
    """The caller's live rows for these sessions, keyed by session id.

    Looked up under ``USER#{caller}`` only: a session id that belongs to someone
    else, or to no one, or whose row is deleted, is absent from the result.
    """
    caller = _require_user_id(user_id)
    wanted = list(dict.fromkeys(sid for sid in session_ids if sid))
    if not wanted:
        return {}
    table = _table()
    items = await asyncio.to_thread(_batch_get, table, caller, wanted)
    rows: Dict[str, SessionRow] = {}
    for item in items:
        row = _to_row(item)
        if row is not None:
            rows[row.session_id] = row
    return rows


__all__ = [
    "LEXICAL_SCAN_CAP",
    "SessionRow",
    "get_owned_session_rows",
    "search_user_sessions_lexical",
]
