"""Reload persistence for app-initiated tool-call cards (MCP Apps PR #6).

PR #5's broker (`broker.py`) is in-memory only: when an embedded MCP App
runs a server tool via `tools/call`, the synthesized `tool_use`/
`tool_result` surface in the *live* conversation stream, but on a full
page reload they're gone (the App iframe itself isn't re-instantiated on
reload either). "Option A" of the scoping doc closes that gap with a
side-channel store, mirroring the Artifacts feature: a small per-session
provenance record the SPA replays as a *static historical card* on load.

**This is provenance / UI only.** Model-visible state flows solely through
`ui/update-model-context` (`app_context_dispatch.py`). We deliberately do
NOT persist a synthetic tool turn into AgentCore Memory — that breaks
Bedrock's user/assistant role alternation, a hazard this codebase has been
bitten by (see `stream_coordinator` / max_tokens re-persist comments).

Storage (decision #4): reuse the existing `sessions-metadata` DynamoDB
table — its `SessionLookupIndex` GSI (`GSI_PK=SESSION#<id>`, Projection
ALL) and the app-api task role's Query/Read grant already exist, so this
needs **zero new infra**. New `APPCARD#` SK prefix, alongside the `C#`
(cost) and `META` rows:

    PK:     USER#<user_id>
    SK:     APPCARD#<created_at>#<card_id>
    GSI_PK: SESSION#<session_id>      (SessionLookupIndex)
    GSI_SK: APPCARD#<created_at>

The write stays on the **app-api boundary** (called from
`/mcp-apps/proxy-call` after a successful dispatch) so inference-api keeps
its inference-only scope. Dev/local has no table — every method degrades
to a no-op / empty list, consistent with the whole MCP Apps surface being
gated by `AGENTCORE_MCP_APPS_HOST_ENABLED` (default true since PR #7).
"""

from __future__ import annotations

import logging
import os
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

# Cards expire with the conversation; 90d matches "conversation history"
# retention expectations without keeping provenance forever.
_CARD_TTL_DAYS = 90
# DynamoDB item hard limit is 400KB; cap the embedded result well under
# that so a chatty tool can't fail the write (or bloat hydration).
_MAX_CONTENT_BYTES = 200_000
_KEY_ATTRS = ("PK", "SK", "GSI_PK", "GSI_SK", "ttl")


def _floats_to_decimal(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj


def _decimal_to_native(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # int when whole, else float — keeps message-index style fields tidy.
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_native(v) for v in obj]
    return obj


class AppCardStore:
    """Per-session store of app-initiated tool-call provenance cards."""

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
                "mcp-apps card store: DynamoDB unavailable; persistence "
                "disabled (cards will be live-only).",
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
        tool_name: str,
        arguments: Dict[str, Any],
        content: List[Dict[str, Any]],
        is_error: bool,
        produced_by_message_index: Optional[int] = None,
    ) -> None:
        """Persist one app-initiated tool-call card. Best-effort.

        Never raises into the proxy path — a failed provenance write must
        not fail the tool call the App is waiting on (it still surfaced
        live via the broker; only the reload card is lost).
        """
        if self._table is None:
            return
        created_at = datetime.now(timezone.utc).isoformat()
        card_id = uuid_lib.uuid4().hex[:12]

        safe_content = content
        try:
            import json

            if len(json.dumps(content, ensure_ascii=False).encode()) > _MAX_CONTENT_BYTES:
                safe_content = [
                    {
                        "type": "text",
                        "text": "[result omitted from history — too large to persist]",
                    }
                ]
        except (TypeError, ValueError):
            safe_content = [
                {"type": "text", "text": "[result not serializable for history]"}
            ]

        ttl = int(
            (datetime.now(timezone.utc) + timedelta(days=_CARD_TTL_DAYS)).timestamp()
        )
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"APPCARD#{created_at}#{card_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"APPCARD#{created_at}",
            "userId": user_id,
            "sessionId": session_id,
            "cardId": card_id,
            "toolUseId": tool_use_id,
            "toolName": tool_name,
            "arguments": arguments,
            "content": safe_content,
            "isError": is_error,
            "createdAt": created_at,
            "producedByMessageIndex": produced_by_message_index,
            "ttl": ttl,
        }
        try:
            self._table.put_item(Item=_floats_to_decimal(item))
        except Exception:  # noqa: BLE001 - provenance is best-effort
            logger.warning(
                "mcp-apps card store: failed to persist card (session=%s)",
                session_id,
                exc_info=True,
            )

    def list_for_session(
        self, *, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """Return this user's app-initiated tool cards for a session.

        Queried off the session GSI then re-filtered by `userId` so a
        guessed session id can't surface another user's cards (mirrors the
        Artifacts ownership re-check). Oldest-first for stable rendering.
        """
        if self._table is None:
            return []
        try:
            items: List[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}")
                & Key("GSI_SK").begins_with("APPCARD#"),
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
                "mcp-apps card store: query failed (session=%s)",
                session_id,
                exc_info=True,
            )
            return []

        cards: List[Dict[str, Any]] = []
        for raw in items:
            if raw.get("userId") != user_id:
                continue  # ownership re-check (guessed session id)
            card = _decimal_to_native(
                {k: v for k, v in raw.items() if k not in _KEY_ATTRS}
            )
            cards.append(card)
        return cards


_store: Optional[AppCardStore] = None


def get_app_card_store() -> AppCardStore:
    """Get or create the process-global card store."""
    global _store
    if _store is None:
        _store = AppCardStore()
    return _store
