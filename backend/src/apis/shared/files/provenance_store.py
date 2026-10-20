"""Reload persistence for source-provenance verdicts.

``docs/specs/source-provenance-verification.md`` §8.1. The
``source_provenance`` SSE event is emitted once, mid-turn, as the verdicts
land. It never re-streams, so without a row a reload would show the cited
values with no provenance at all — which is worse than it sounds, because the
state a reader most needs is ``quote_unsupported``, and a verdict that
disappears on refresh reads as a clean answer.

WHY NOT ON THE MESSAGE ITSELF
-----------------------------
The obvious home is the ``toolUse`` / ``toolResult`` block the verdicts belong
to. It is the wrong home twice over, exactly as it is for
``tool_summaries/store.py``:

1. The message content blocks ARE the Bedrock Converse payload, so a
   non-standard key on one is at best ignored and at worst rejected.
2. Conversation history is the cacheable prefix. Writing the quote, locator and
   method into it would re-write that prefix on every later turn at the
   cache-write premium (CLAUDE.md prompt-cache contract) — paying model rates,
   forever, for display metadata. The model is told the state and nothing else
   (§8); everything a reader needs beyond that lives here.

STORAGE
-------
Reuses the ``sessions-metadata`` table and its ``SessionLookupIndex`` GSI
(``GSI_PK=SESSION#<id>``, Projection ALL), whose app-api Query grant already
exists, so this needs **zero new infra**. A new ``SRCV#`` SK prefix beside
``C#`` (cost), ``META``, ``TSUM#``, ``APPCARD#`` and ``UIRES#``::

    PK:     USER#<user_id>
    SK:     SRCV#<tool_use_id>       (last-write-wins per call)
    GSI_PK: SESSION#<session_id>     (SessionLookupIndex)
    GSI_SK: SRCV#<created_at>

Keyed on the ``toolUseId`` of the ``verify_sources`` call, so a re-run of the
same invocation overwrites its own row rather than accumulating.

Rows carry model-quoted passages from the user's own document, so ``quote`` is
clamped and the claim count is bounded: an item that grows past DynamoDB's
400KB limit would fail the write and lose the whole turn's provenance, which is
a bad trade for a longer quote.
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

#: Verdicts expire with the conversation; 90d matches the sibling stores.
_TTL_DAYS = 90

#: Clamp on one stored quote. Long enough for any passage worth citing, short
#: enough that a bounded batch cannot approach the 400KB item limit. The tool
#: refuses a longer quote outright, so this is the defensive second line.
_MAX_QUOTE_CHARS = 1200

#: Claims kept per call. The tool caps its own input lower; this guards the row
#: against a future caller that does not.
_MAX_CLAIMS = 24


class SourceProvenanceStore:
    """Per-session store of one ``verify_sources`` call's verdicts."""

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
                "source-provenance store: DynamoDB unavailable; persistence "
                "disabled (verdicts will be live-only).",
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
        claims: List[Dict[str, Any]],
    ) -> None:
        """Persist one call's verdicts. Best-effort, never raises.

        A failed write costs reload survival only — the user saw the verdicts
        live — and failing the turn over it would turn a degraded citation into
        a lost answer.
        """
        if self._table is None or not tool_use_id or not claims:
            return

        created_at = datetime.now(timezone.utc).isoformat()
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"SRCV#{tool_use_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"SRCV#{created_at}",
            "userId": user_id,
            "sessionId": session_id,
            "toolUseId": tool_use_id,
            "claims": [_clamp(claim) for claim in claims[:_MAX_CLAIMS]],
            "createdAt": created_at,
            "ttl": int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp()),
        }
        try:
            self._table.put_item(Item=item)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.warning(
                "source-provenance store: failed to persist verdicts "
                "(session=%s, toolUse=%s)",
                session_id,
                tool_use_id,
                exc_info=True,
            )

    def list_for_session(self, *, session_id: str, user_id: str) -> List[Dict[str, Any]]:
        """This user's provenance verdicts for a session, oldest first.

        Queried off the session GSI then re-filtered by ``userId``, so a guessed
        session id cannot surface another user's quoted passages — the same
        ownership re-check the sibling stores make, and it matters more here
        because these rows carry document text.
        """
        if self._table is None:
            return []
        try:
            items: List[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}")
                & Key("GSI_SK").begins_with("SRCV#"),
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
                "source-provenance store: query failed (session=%s)",
                session_id,
                exc_info=True,
            )
            return []

        verdicts: List[Dict[str, Any]] = []
        for item in items:
            if item.get("userId") != user_id:
                continue  # ownership re-check (guessed session id)
            verdicts.append(
                {
                    "toolUseId": str(item.get("toolUseId", "")),
                    "claims": [dict(claim) for claim in (item.get("claims") or [])],
                }
            )
        return verdicts


def _clamp(claim: Dict[str, Any]) -> Dict[str, Any]:
    """One stored claim, with its quote bounded and nothing extra carried."""
    stored: Dict[str, Any] = {
        "id": str(claim.get("id", ""))[:120],
        "state": str(claim.get("state", "")),
        "method": str(claim.get("method", "")),
        "quote": str(claim.get("quote") or "")[:_MAX_QUOTE_CHARS],
        "uploadId": str(claim.get("uploadId", "")),
        "filename": str(claim.get("filename", ""))[:255],
    }
    locator = claim.get("locator")
    if isinstance(locator, dict):
        # `precision` is never dropped: a locator a reader cannot tell apart
        # from a measured one is the whole defect this feature exists to avoid.
        stored["locator"] = {
            "kind": str(locator.get("kind", "")),
            "value": int(locator.get("value") or 0),
            "precision": str(locator.get("precision", "")),
        }
    return stored


_store: Optional[SourceProvenanceStore] = None


def get_source_provenance_store() -> SourceProvenanceStore:
    """Get or create the process-global store."""
    global _store
    if _store is None:
        _store = SourceProvenanceStore()
    return _store


__all__ = ["SourceProvenanceStore", "get_source_provenance_store"]
