"""Provenance verdicts by version, joined at read time (spec §5.2 corrections, D6).

``verify_sources`` writes one ``SRCV#`` row per call (``apis.shared.files.provenance_store``):
per user, keyed by tool use, with no version stamp and no fleet-wide index. The
ledger cannot count verdicts at write time because the cost row does not carry
them, so the quality panel's ``quote_unsupported`` share is a join, made the
way the thumbs arm is made:

1. **Gate on the ledger.** ``LEDGER#TOOL`` already counts ``verify_sources``
   calls per day. No calls in range means no rows to find, so nothing else is
   read; that is the common case while ``SOURCE_PROVENANCE_ENABLED`` is off.
2. **Walk the range's top-cost users** (``PeriodCostIndex``, the top-sessions
   view's bounded fan-out: a verdict exists only where a user spent), and for
   each one query their ``SRCV#`` rows, filtered to the window.
3. **Attribute by the session's own cost rows.** A verdict is attributed to
   the ``C#`` row of the same session nearest before it in time (the call that
   asked for the check), whose stamp names the build. A verdict with no such
   row is counted as ``unattributed``, never guessed from the date.

**Content-free.** An ``SRCV#`` row carries model-quoted passages from the
user's document. The query projects ``sessionId``, ``createdAt`` and each
claim's ``state`` by list index, so a quote or value never leaves DynamoDB;
the session id is used for the join and never returned. Every cap is reported
as ``coverage`` rather than silently under-counting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from bisect import bisect_right
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from apis.shared.costs.ledger_reader import months_between
from apis.shared.files.source_provenance import (
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNASSESSED,
    STATE_UNVERIFIED,
)

logger = logging.getLogger(__name__)

VERIFY_SOURCES_TOOL = "verify_sources"
#: Counted per state, in the order the pure module defines them.
STATES = (STATE_SUPPORTED, STATE_QUOTE_UNSUPPORTED, STATE_UNASSESSED, STATE_UNVERIFIED)

#: The store keeps at most 24 claims a row (``provenance_store._MAX_CLAIMS``).
MAX_CLAIMS = 24
MAX_USERS = 200
MAX_SESSIONS = 300
CONCURRENCY = 16

#: The logical paths :func:`_projection` requests (``claims.state`` as
#: ``claims[i].state`` per index); registered in the content policy.
from apis.shared.observability.content_policy import PROVENANCE_STATE_PROJECTION  # noqa: E402


def verify_sources_calls(tool_rows: Iterable[Mapping[str, Any]]) -> int:
    return int(sum(float(r.get("calls") or 0) for r in tool_rows if r.get("toolId") == VERIFY_SOURCES_TOOL))


def _projection() -> Dict[str, Any]:
    names = {"#sid": "sessionId", "#cat": "createdAt", "#cl": "claims", "#st": "state"}
    paths = ["#sid", "#cat"] + [f"#cl[{i}].#st" for i in range(MAX_CLAIMS)]
    return {"ProjectionExpression": ", ".join(paths), "ExpressionAttributeNames": names}


def _user_verdicts(table: Any, user_id: str, start: str, end: str) -> List[Dict[str, Any]]:
    projection = _projection()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "FilterExpression": "#cat BETWEEN :start AND :end",
        "ExpressionAttributeValues": {":pk": f"USER#{user_id}", ":sk": "SRCV#", ":start": start, ":end": end},
        **projection,
    }
    rows: List[Dict[str, Any]] = []
    while True:
        response = table.query(**kwargs)
        for item in response.get("Items", []):
            states = [str(c.get("state") or "") for c in (item.get("claims") or []) if isinstance(c, Mapping)]
            rows.append({"sessionId": str(item.get("sessionId") or ""), "createdAt": str(item.get("createdAt") or ""),
                         "states": states})
        last = response.get("LastEvaluatedKey")
        if not last:
            return rows
        kwargs["ExclusiveStartKey"] = last


def _empty_counts() -> Dict[str, int]:
    return {"verdicts": 0, "claims": 0, **{state: 0 for state in STATES}}


def attribute(
    verdicts: Sequence[Mapping[str, Any]],
    records_by_session: Mapping[str, Sequence[Mapping[str, Any]]],
    key_of: Callable[[Mapping[str, Any]], str],
) -> Dict[str, Any]:
    """Count claims by state per key; pure. ``key_of`` maps a ``C#`` row to a unit key."""
    by_key: Dict[str, Dict[str, int]] = {}
    unattributed = 0
    for verdict in verdicts:
        records = [r for r in records_by_session.get(verdict["sessionId"], []) if r.get("timestamp")]
        if not records:
            unattributed += 1
            continue
        stamps = [str(r["timestamp"]) for r in records]
        position = bisect_right(stamps, verdict["createdAt"]) - 1
        record = records[position] if position >= 0 else records[0]
        counts = by_key.setdefault(key_of(record), _empty_counts())
        counts["verdicts"] += 1
        for state in verdict["states"]:
            counts["claims"] += 1
            if state in counts:
                counts[state] += 1
    return {"byKey": by_key, "unattributed": unattributed}


async def provenance_by_unit(
    storage: Any,
    *,
    from_date: str,
    to_date: str,
    tool_rows: Sequence[Mapping[str, Any]],
    key_of: Callable[[Mapping[str, Any]], str],
) -> Dict[str, Any]:
    """``{byKey: {key: counts}, coverage}`` for ``[from_date, to_date]``. Never raises."""
    coverage: Dict[str, Any] = {
        "verifySourcesCalls": verify_sources_calls(tool_rows),
        "usersScanned": 0, "usersTruncated": False, "sessionsJoined": 0, "sessionsOmitted": 0,
        "verdicts": 0, "unattributed": 0, "skipped": None,
    }
    if coverage["verifySourcesCalls"] == 0:
        coverage["skipped"] = "no_verify_sources_calls"
        return {"byKey": {}, "coverage": coverage}
    table = getattr(storage, "sessions_metadata_table", None)
    if table is None and os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME"):
        from apis.shared.aws_clients import get_dynamodb_table

        table = get_dynamodb_table(os.environ["DYNAMODB_SESSIONS_METADATA_TABLE_NAME"])
    if table is None:
        coverage["skipped"] = "unavailable"
        return {"byKey": {}, "coverage": coverage}

    try:
        users: List[str] = []
        for month in months_between(from_date, to_date):
            for row in await storage.get_top_users_by_cost(period=month, limit=MAX_USERS + 1):
                user_id = str(row.get("userId") or "")
                if user_id and user_id not in users:
                    users.append(user_id)
        coverage["usersTruncated"] = len(users) > MAX_USERS
        users = users[:MAX_USERS]
        coverage["usersScanned"] = len(users)

        start, end = f"{from_date}T00:00:00", f"{to_date}T￿"
        verdicts: List[Dict[str, Any]] = []
        for offset in range(0, len(users), CONCURRENCY):
            batch = users[offset:offset + CONCURRENCY]
            results = await asyncio.gather(
                *(asyncio.to_thread(_user_verdicts, table, user_id, start, end) for user_id in batch),
                return_exceptions=True,
            )
            for result in results:
                if not isinstance(result, BaseException):
                    verdicts.extend(result)

        session_ids: List[str] = []
        for verdict in verdicts:
            if verdict["sessionId"] and verdict["sessionId"] not in session_ids:
                session_ids.append(verdict["sessionId"])
        coverage["sessionsOmitted"] = max(0, len(session_ids) - MAX_SESSIONS)
        session_ids = session_ids[:MAX_SESSIONS]
        records: Dict[str, List[Dict[str, Any]]] = {}
        for offset in range(0, len(session_ids), CONCURRENCY):
            batch = session_ids[offset:offset + CONCURRENCY]
            results = await asyncio.gather(
                *(storage.get_session_cost_records(sid) for sid in batch), return_exceptions=True,
            )
            for sid, result in zip(batch, results):
                if not isinstance(result, BaseException):
                    records[sid] = list(result or [])
        coverage["sessionsJoined"] = len(records)
        # A verdict of an omitted session is reported in `sessionsOmitted`, not
        # counted; one whose session's rows failed to read is `unattributed`.
        kept = set(session_ids)
        joined = [v for v in verdicts if v["sessionId"] in kept or not v["sessionId"]]
        result = attribute(joined, records, key_of)
        coverage["verdicts"] = len(joined)
        coverage["unattributed"] = result["unattributed"]
        return {"byKey": result["byKey"], "coverage": coverage}
    except Exception as exc:  # noqa: BLE001 - provenance is one panel of several
        logger.warning("Release ledger: provenance join failed: %s", type(exc).__name__)
        coverage["skipped"] = "error"
        return {"byKey": {}, "coverage": coverage}


def unit_counts(by_key: Mapping[str, Mapping[str, int]], keys: Iterable[str]) -> Dict[str, int]:
    """Sum the counts of several keys (a version's builds)."""
    total = _empty_counts()
    for key in keys:
        for name, value in (by_key.get(key) or {}).items():
            total[name] = total.get(name, 0) + int(value)
    return total


def matching_keys(by_key: Mapping[str, Any], unit_key: str, is_build: bool) -> List[str]:
    """Keys in a ``build``-keyed map that belong to a unit (``1.33.0`` matches ``1.33.0#*``)."""
    if is_build:
        return [unit_key] if unit_key in by_key else []
    return [k for k in by_key if k.split("#", 1)[0] == unit_key]


__all__ = [
    "PROVENANCE_STATE_PROJECTION", "STATES", "attribute", "matching_keys",
    "provenance_by_unit", "unit_counts", "verify_sources_calls",
]

