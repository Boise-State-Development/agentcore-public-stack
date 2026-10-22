"""Reload persistence for model-generated tool-batch summaries.

The `tool_group_summary` SSE event is emitted once, mid-turn, as the
summarizer task finishes. It never re-streams, so on a page reload the SPA
would fall back to its deterministic client-side formatter and the prose line
the user saw ("Found the Syllabus Acknowledgment assignment in BIO 101") would
silently downgrade to "Listed 4 assignments". This store closes that gap the
same way `mcp_apps/ui_resource_store.py` does for App frames: a small
per-session side-channel record the messages endpoint replays on load.

WHY NOT ON THE MESSAGE ITSELF
-----------------------------
A summary describes a `toolUse` block, so the obvious home is a field on that
block. It is the wrong home, twice over:

1. The message content blocks ARE the Bedrock Converse payload. A non-standard
   key on a `toolUse` block is at best ignored and at worst rejected.
2. Far more expensive: conversation history is the cacheable prefix. Writing a
   summary into it would re-write the prefix on the next turn at the
   cache-write premium (CLAUDE.md prompt-cache contract) — paying model rates,
   every turn, for a display string. A side-channel row costs one small
   DynamoDB write and nothing at inference time.

STORAGE
-------
Reuses the existing `sessions-metadata` table — its `SessionLookupIndex` GSI
(`GSI_PK=SESSION#<id>`, Projection ALL) and the app-api task role's Query grant
already exist, so this needs **zero new infra**. New `TSUM#` SK prefix
alongside `C#` (cost), `META`, `APPCARD#` and `UIRES#`:

    PK:     USER#<user_id>
    SK:     TSUM#<batch_id>            (last-write-wins per batch)
    GSI_PK: SESSION#<session_id>       (SessionLookupIndex)
    GSI_SK: TSUM#<created_at>

`batch_id` is the first `toolUseId` of the batch, so a re-run of the same
invocation overwrites its own row rather than accumulating. The row also
carries every `toolUseId` in the batch, which is what the SPA keys on: it
groups the rail by tool-use id, not by batch, and needs to find the summary
from any call in the group.

Rows are tiny (a sentence plus a handful of ids), so unlike the UI-resource
store there is no compression and no size gate — only a defensive length clamp
on the summary text itself.
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

# Summaries expire with the conversation; 90d matches the sibling stores.
_SUMMARY_TTL_DAYS = 90
# Defensive clamp. The summarizer already caps output tokens; this guards
# against a model that ignores the instruction, so one bad generation cannot
# bloat the row.
_MAX_SUMMARY_CHARS = 240
# A batch cannot realistically fan out past this; the cap keeps the id list
# from turning a tiny row into a large one.
_MAX_TOOL_USE_IDS = 24
_KEY_ATTRS = ("PK", "SK", "GSI_PK", "GSI_SK", "ttl")


class ToolSummaryStore:
    """Per-session store of model-generated tool-batch summaries."""

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
                "tool-summary store: DynamoDB unavailable; persistence "
                "disabled (summaries will be live-only).",
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
        batch_id: str,
        tool_use_ids: List[str],
        summary: str,
    ) -> None:
        """Persist one batch summary. Best-effort, never raises.

        A failed write costs only reload survival — the user already saw the
        summary live, and the SPA's deterministic formatter covers the reload.
        That is a good enough fallback that it is never worth failing a turn.
        """
        if self._table is None or not summary or not batch_id:
            return

        created_at = datetime.now(timezone.utc).isoformat()
        ttl = int(
            (datetime.now(timezone.utc) + timedelta(days=_SUMMARY_TTL_DAYS)).timestamp()
        )
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"TSUM#{batch_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"TSUM#{created_at}",
            "userId": user_id,
            "sessionId": session_id,
            "batchId": batch_id,
            "toolUseIds": list(tool_use_ids)[:_MAX_TOOL_USE_IDS],
            "summary": summary[:_MAX_SUMMARY_CHARS],
            "createdAt": created_at,
            "ttl": ttl,
        }
        try:
            self._table.put_item(Item=item)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.warning(
                "tool-summary store: failed to persist summary "
                "(session=%s, batch=%s)",
                session_id,
                batch_id,
                exc_info=True,
            )

    def list_for_session(
        self, *, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """Return this user's tool-batch summaries for a session.

        Queried off the session GSI then re-filtered by `userId`, so a guessed
        session id cannot surface another user's summaries (mirrors the
        UI-resource store's ownership re-check). Oldest-first for stable order.
        """
        if self._table is None:
            return []
        try:
            items: List[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}")
                & Key("GSI_SK").begins_with("TSUM#"),
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
                "tool-summary store: query failed (session=%s)",
                session_id,
                exc_info=True,
            )
            return []

        summaries: List[Dict[str, Any]] = []
        for item in items:
            if item.get("userId") != user_id:
                continue  # ownership re-check (guessed session id)
            summaries.append(
                {
                    "batchId": str(item.get("batchId", "")),
                    "toolUseIds": [str(t) for t in (item.get("toolUseIds") or [])],
                    "summary": str(item.get("summary", "")),
                }
            )
        return summaries


_store: Optional[ToolSummaryStore] = None


def get_tool_summary_store() -> ToolSummaryStore:
    """Get or create the process-global tool-summary store."""
    global _store
    if _store is None:
        _store = ToolSummaryStore()
    return _store
