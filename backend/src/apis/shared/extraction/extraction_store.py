"""Reload persistence for extraction runs.

``docs/specs/structured-extraction.md`` §9. The ``document_extraction`` SSE
event is emitted once, mid-turn, as the rows land. It never re-streams, so
without a row a reload would show a conversation that mentions values with no
table and no provenance behind them — and the state a reader most needs is
``quote_unsupported``, which vanishing on refresh reads as a clean answer.

WHY NOT ON THE MESSAGE ITSELF
-----------------------------
The obvious home is the ``toolUse`` / ``toolResult`` block the rows belong to.
It is the wrong home twice over, exactly as it is for ``tool_summaries/store``
and ``provenance_store``:

1. The message content blocks ARE the Bedrock Converse payload, so a
   non-standard key on one is at best ignored and at worst rejected.
2. Conversation history is the cacheable prefix. Writing the quotes, locators
   and the rendered table into it would re-write that prefix on every later
   turn at the cache-write premium — paying model rates, forever, for display
   metadata.

STORAGE
-------
Reuses the ``sessions-metadata`` table and its ``SessionLookupIndex`` GSI
(``GSI_PK=SESSION#<id>``, Projection ALL), whose app-api Query grant already
exists, so this needs **zero new infra**. A new ``EXTR#`` SK prefix beside
``C#`` (cost), ``META``, ``TSUM#``, ``SRCV#``, ``APPCARD#`` and ``UIRES#``::

    PK:     USER#<user_id>
    SK:     EXTR#<tool_use_id>#<upload_id>
    GSI_PK: SESSION#<session_id>    (SessionLookupIndex)
    GSI_SK: EXTR#<created_at>

**One row per (call, document)**, not per call. A single
``document_extract`` call now carries N documents (§17), and they share one
``toolUseId`` — so a key of ``EXTR#<tool_use_id>`` alone would have had the
documents in a batch silently overwrite each other, leaving a reload showing
one row set where the user saw fourteen. Keying on both also keeps each row
independently bounded against the 400KB item limit: fourteen fields of one
document fit comfortably, where a whole batch in one item would not.

The ``toolUseId`` is still the grouping key a reader uses, so the SPA can
reassemble a batch from the rows without being told how many to expect; and a
re-run of the same invocation still overwrites its own rows rather than
accumulating.

Rows carry model-quoted passages from the user's own document, so quotes and
values are clamped and the field count is bounded. An item that grew past
DynamoDB's 400KB limit would fail the write and lose the whole run's
provenance, which is a bad trade for a longer quote — and if the clamped row
*still* would not fit, the fields are stored without their quotes rather than
not at all, because the artifact holds the full table either way.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:  # boto3 is absent in some local-dev setups
    import boto3
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    Key = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

#: Runs expire with the conversation; 90d matches the sibling stores.
_TTL_DAYS = 90

#: Clamp on one stored quote. The parser caps a field set at 40 fields, so 40
#: quotes at this length plus their values stay well inside the 400KB item
#: limit with room for the rest of the row.
_MAX_QUOTE_CHARS = 1000

#: Clamp on one stored value.
_MAX_VALUE_CHARS = 1000

#: Fields kept per run. Matches the parser's own cap, so a well-formed field
#: set is never truncated here; this guards the row against a future caller
#: that does not parse.
_MAX_FIELDS = 40

#: Above this, the quotes are dropped from the stored row before the write.
#: Chosen well under DynamoDB's 400KB so the headroom covers the rest of the
#: item plus attribute-name overhead.
_MAX_ITEM_CHARS = 300_000


class ExtractionStore:
    """Per-session store of one ``document_extract`` run's rows."""

    def __init__(self) -> None:
        self._table = None
        if boto3 is None:
            return
        table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
        if not table_name:
            return
        try:
            self._table = boto3.resource("dynamodb").Table(table_name)
        except Exception:  # noqa: BLE001 - dev without AWS creds
            logger.warning(
                "extraction store: DynamoDB unavailable; persistence disabled "
                "(runs will be live-only).",
                exc_info=True,
            )
            self._table = None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def store(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_use_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Persist one run. Best-effort, never raises.

        A failed write costs reload survival only — the user saw the rows live,
        and the artifact and the workbook are both already written — and
        failing the turn over it would turn a degraded record into a lost
        answer.
        """
        if self._table is None or not tool_use_id or not payload:
            return

        created_at = datetime.now(timezone.utc).isoformat()
        stored = _clamp_payload(payload)
        item: Dict[str, Any] = {
            "PK": f"USER#{user_id}",
            "SK": f"EXTR#{tool_use_id}#{payload.get('uploadId', '')}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"EXTR#{created_at}",
            "userId": user_id,
            "sessionId": session_id,
            "toolUseId": tool_use_id,
            **stored,
            "createdAt": created_at,
            "ttl": int(
                (datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp()
            ),
        }

        if _rough_size(item) > _MAX_ITEM_CHARS:
            # Names, values and states survive; the quotes go. The artifact
            # holds the full table regardless, so nothing is actually lost —
            # and a row that fails to write loses the states too.
            logger.info(
                "extraction store: dropping quotes to fit the item limit "
                "(session=%s, toolUse=%s)",
                session_id,
                tool_use_id,
            )
            for field in item.get("fields", []):
                field.pop("quote", None)
            item["quotesOmitted"] = True

        try:
            self._table.put_item(Item=item)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.warning(
                "extraction store: failed to persist run (session=%s, toolUse=%s)",
                session_id,
                tool_use_id,
                exc_info=True,
            )

    def list_for_session(
        self, *, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """This user's extraction runs for a session, oldest first.

        Queried off the session GSI then re-filtered by ``userId``, so a
        guessed session id cannot surface another user's extracted values — the
        same ownership re-check the sibling stores make, and it matters more
        here because these rows carry document text.
        """
        if self._table is None:
            return []
        try:
            items: List[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}")
                & Key("GSI_SK").begins_with("EXTR#"),
                "ScanIndexForward": True,
            }
            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.warning(
                "extraction store: query failed (session=%s)",
                session_id,
                exc_info=True,
            )
            return []

        runs: List[Dict[str, Any]] = []
        for item in items:
            if item.get("userId") != user_id:
                continue  # ownership re-check (guessed session id)
            runs.append(
                {
                    "toolUseId": str(item.get("toolUseId", "")),
                    "fieldSet": str(item.get("fieldSet", "")),
                    "fieldSetVersion": str(item.get("fieldSetVersion", "")),
                    "uploadId": str(item.get("uploadId", "")),
                    "filename": str(item.get("filename", "")),
                    "modelId": str(item.get("modelId", "")),
                    "schemaEnforced": bool(item.get("schemaEnforced", True)),
                    "artifactId": str(item.get("artifactId", "")) or None,
                    "downloadUploadId": str(item.get("downloadUploadId", "")) or None,
                    "quotesOmitted": bool(item.get("quotesOmitted", False)),
                    "fields": [dict(field) for field in (item.get("fields") or [])],
                }
            )
        return runs


def _rough_size(item: Dict[str, Any]) -> int:
    import json

    try:
        return len(json.dumps(item, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return 0


def _clamp_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One stored run, bounded, carrying nothing extra."""
    stored: Dict[str, Any] = {
        "fieldSet": str(payload.get("fieldSet", ""))[:120],
        "fieldSetVersion": str(payload.get("fieldSetVersion", ""))[:40],
        "uploadId": str(payload.get("uploadId", ""))[:120],
        "filename": str(payload.get("filename", ""))[:255],
        "modelId": str(payload.get("modelId", ""))[:160],
        "schemaEnforced": bool(payload.get("schemaEnforced", True)),
        "fields": [_clamp_field(f) for f in (payload.get("fields") or [])[:_MAX_FIELDS]],
    }
    for key in ("artifactId", "downloadUploadId"):
        value = payload.get(key)
        if value:
            stored[key] = str(value)[:120]
    return stored


def _clamp_field(field: Dict[str, Any]) -> Dict[str, Any]:
    stored: Dict[str, Any] = {
        "name": str(field.get("name", ""))[:120],
        "title": str(field.get("title", ""))[:200],
        "type": str(field.get("type", ""))[:20],
        "value": str(field.get("value") or "")[:_MAX_VALUE_CHARS],
        "state": str(field.get("state", "")),
        "method": str(field.get("method", "")),
    }
    quote = field.get("quote")
    if quote:
        stored["quote"] = str(quote)[:_MAX_QUOTE_CHARS]
    locator = field.get("locator")
    if isinstance(locator, dict):
        # `precision` is never dropped: a locator a reader cannot tell apart
        # from a measured one is the defect this whole check exists to avoid.
        stored["locator"] = {
            "kind": str(locator.get("kind", "")),
            "value": int(locator.get("value") or 0),
            "precision": str(locator.get("precision", "")),
        }
    return stored


_store: Optional[ExtractionStore] = None


def get_extraction_store() -> ExtractionStore:
    """Get or create the process-global store."""
    global _store
    if _store is None:
        _store = ExtractionStore()
    return _store


__all__ = ["ExtractionStore", "get_extraction_store"]
