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
    SK:     APPCARD#<card_key>
    GSI_PK: SESSION#<session_id>      (SessionLookupIndex)
    GSI_SK: APPCARD#<created_at>

**One card per distinct action, not per run.** An App re-runs its setup
calls every time its frame mounts (GitHub's `issue_write` App issues five
`ui_get` lookups), and the frame re-mounts on every reload. A card per run
grew the frame's "N actions · M failed" chip each time a conversation was
reopened. So `<card_key>` is a hash of the call's identity — the session,
the originating `toolUseId` (the frame), the tool and its arguments — and a
repeat run updates that card's result in place, keeping its first
`createdAt` so it holds its position. The cost: two byte-identical calls
from one frame (the same issue submitted twice with the same text) record
as one card showing the latest result. Rows written before this keyed on
`<created_at>#<random id>`; `list_for_session` collapses those the same way.

The write stays on the **app-api boundary** (called from
`/mcp-apps/proxy-call` after a successful dispatch) so inference-api keeps
its inference-only scope. Dev/local has no table — every method degrades
to a no-op / empty list, consistent with the whole MCP Apps surface being
gated by `AGENTCORE_MCP_APPS_HOST_ENABLED` (default true since PR #7).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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


def _canonical_arguments(arguments: Any) -> str:
    """Arguments as a stable string: key order and float spelling don't vary."""
    return json.dumps(
        _decimal_to_native(arguments or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def card_key(
    *, session_id: str, tool_use_id: str, tool_name: str, arguments: Any
) -> str:
    """The identity of an app-initiated call: same frame, tool and arguments.

    Re-running a call (an App's setup lookups on every frame mount) yields
    the same key, so it updates one card instead of adding another.
    """
    identity = json.dumps(
        [session_id, tool_use_id, tool_name, _canonical_arguments(arguments)],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def _collapse_reruns(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold repeat runs of one call into its first card, with the latest result.

    `cards` is oldest-first. Rows keyed by `card_key` are already one per
    call; this collapses rows written before that, one per run.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    for card in cards:
        key = card_key(
            session_id=str(card.get("sessionId") or ""),
            tool_use_id=str(card.get("toolUseId") or ""),
            tool_name=str(card.get("toolName") or ""),
            arguments=card.get("arguments"),
        )
        first = by_key.get(key)
        if first is None:
            by_key[key] = card
            continue
        first["content"] = card.get("content")
        first["isError"] = card.get("isError")
    return list(by_key.values())


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

        A repeat of a call already on record (same frame, tool and
        arguments) updates that card's result and keeps its `createdAt`,
        so reopening a conversation, which re-mounts the App and re-runs
        its setup calls, never adds cards.

        Never raises into the proxy path — a failed provenance write must
        not fail the tool call the App is waiting on (it still surfaced
        live via the broker; only the reload card is lost).
        """
        if self._table is None:
            return
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        key = card_key(
            session_id=session_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            arguments=arguments,
        )

        safe_content = content
        try:
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

        ttl = int((now + timedelta(days=_CARD_TTL_DAYS)).timestamp())
        # Set on the first run only, so a re-run keeps the card's position.
        first_run = {
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"APPCARD#{created_at}",
            "userId": user_id,
            "sessionId": session_id,
            "cardId": key[:16],
            "toolUseId": tool_use_id,
            "toolName": tool_name,
            "arguments": arguments,
            "createdAt": created_at,
            "producedByMessageIndex": produced_by_message_index,
        }
        # Refreshed on every run: the latest result is the one to show.
        every_run = {
            "content": safe_content,
            "isError": is_error,
            "updatedAt": created_at,
            "ttl": ttl,
        }
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        clauses: List[str] = []
        for i, (attr, value) in enumerate({**first_run, **every_run}.items()):
            names[f"#a{i}"] = attr
            values[f":v{i}"] = value
            clauses.append(
                f"#a{i} = if_not_exists(#a{i}, :v{i})"
                if attr in first_run
                else f"#a{i} = :v{i}"
            )
        try:
            self._table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"APPCARD#{key}"},
                UpdateExpression="SET " + ", ".join(clauses),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=_floats_to_decimal(values),
            )
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
        Artifacts ownership re-check). Oldest-first for stable rendering,
        one card per distinct call (see `_collapse_reruns`).
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
        return _collapse_reruns(cards)


_store: Optional[AppCardStore] = None


def get_app_card_store() -> AppCardStore:
    """Get or create the process-global card store."""
    global _store
    if _store is None:
        _store = AppCardStore()
    return _store
