"""Reload persistence for model-initiated MCP App UI resources (SEP-1865).

The `ui_resource` SSE event is INLINE — emitted once, right after the
`tool_result` it correlates to, with the App's HTML fetched server-side via
`resources/read` and inlined. It never re-streams, so on a page reload the
SPA's `McpAppStateService` is empty and the `mcp-app-frame` falls back to a
plain tool card. This store closes that gap exactly like the Artifacts
feature and the PR #6 app-card store (`card_store.py`): a small per-session
side-channel record the SPA replays on load to re-seed `McpAppStateService`
and re-instantiate the frame.

Unlike the PR #6 card store (app-INITIATED tool calls, surfaced as a static
historical card), this persists the MODEL-initiated UI resource — the live
iframe payload — so the App re-renders, not just a record of it.

Storage (mirrors decision #4 of the card store): reuse the existing
`sessions-metadata` DynamoDB table — its `SessionLookupIndex` GSI
(`GSI_PK=SESSION#<id>`, Projection ALL) and the app-api task role's Query
grant already exist, so this needs **zero new infra**. New `UIRES#` SK
prefix alongside the `C#` (cost), `META`, and `APPCARD#` rows:

    PK:     USER#<user_id>
    SK:     UIRES#<tool_use_id>          (last-write-wins per toolUseId)
    GSI_PK: SESSION#<session_id>         (SessionLookupIndex)
    GSI_SK: UIRES#<created_at>

The SK is keyed by `tool_use_id` (not a random id) so a tool that re-emits
for the same invocation overwrites its prior resource — matching
`McpAppStateService.recordLive`'s last-write-wins semantics.

Boundary: the **write** runs from the agents stream coordinator (where the
payload is born and where artifact stamping + per-message metadata writes
already happen); the **read** runs on the app-api messages endpoint. Both
reach this shared module and neither imports the other — import-boundary
safe. Dev/local has no table — every method degrades to a no-op / empty
list, consistent with the whole MCP Apps surface being gated by
`AGENTCORE_MCP_APPS_HOST_ENABLED` (default true since PR #7).
"""

from __future__ import annotations

import gzip
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

# UI resources expire with the conversation; 90d matches the card store and
# "conversation history" retention expectations.
_CARD_TTL_DAYS = 90
# DynamoDB item hard limit is 400KB (attribute names + values). The HTML
# dominates the record, so we gzip it and store the compressed bytes as a
# Binary attribute — App HTML (markup + inlined JS) typically compresses
# ~4-5x, so a ~450KB App lands around ~100KB, well inside one item. The cap
# is applied to the COMPRESSED size; an App still over it (original HTML
# beyond ~1.3MB) is NOT persisted — a placeholder would frame as a broken
# iframe, whereas skipping degrades to the plain tool card on reload. Those
# rare giants are the only case that needs the S3-backed path.
_MAX_HTML_GZ_BYTES = 380_000
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


def _to_bytes(value: Any) -> bytes:
    """Coerce a DynamoDB Binary attribute to raw bytes.

    boto3's resource API returns a `Binary` wrapper (with a `.value`); the
    in-memory test fake stores plain `bytes`. Handle both.
    """
    return value.value if hasattr(value, "value") else bytes(value)


class UiResourceStore:
    """Per-session store of model-initiated MCP App UI resources."""

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
                "mcp-apps ui-resource store: DynamoDB unavailable; "
                "persistence disabled (Apps will be live-only).",
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
        resource_uri: str,
        html: str,
        mime_type: str,
        csp: Dict[str, Any],
        permissions: Dict[str, Any],
        sandbox_origin: str = "",
        server_name: str = "",
        icon: str = "",
        tool_name: str = "",
        produced_by_message_index: Optional[int] = None,
    ) -> None:
        """Persist one MCP App UI resource. Best-effort.

        Never raises into the stream — a failed persistence write must not
        break the live turn (the App still rendered live via the
        `ui_resource` SSE event; only the reload survival is lost).

        The HTML is gzipped and stored as a Binary attribute; an App whose
        COMPRESSED size still exceeds `_MAX_HTML_GZ_BYTES` is skipped entirely
        (logged) rather than truncated — see the constant.
        """
        if self._table is None:
            return

        try:
            raw = html.encode("utf-8")
        except (AttributeError, UnicodeError):
            logger.warning(
                "mcp-apps ui-resource store: App for toolUseId=%s has "
                "non-text HTML; not persisting.",
                tool_use_id,
            )
            return
        html_gz = gzip.compress(raw)
        if len(html_gz) > _MAX_HTML_GZ_BYTES:
            logger.warning(
                "mcp-apps ui-resource store: App for toolUseId=%s is %d bytes "
                "(%d gzipped, > %d cap); not persisting (will not survive "
                "reload). Needs the S3-backed path.",
                tool_use_id,
                len(raw),
                len(html_gz),
                _MAX_HTML_GZ_BYTES,
            )
            return

        created_at = datetime.now(timezone.utc).isoformat()
        ttl = int(
            (datetime.now(timezone.utc) + timedelta(days=_CARD_TTL_DAYS)).timestamp()
        )
        item = {
            "PK": f"USER#{user_id}",
            # Keyed by toolUseId so a re-emit for the same invocation
            # overwrites (last-write-wins, matching recordLive).
            "SK": f"UIRES#{tool_use_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"UIRES#{created_at}",
            "userId": user_id,
            "sessionId": session_id,
            "toolUseId": tool_use_id,
            "resourceUri": resource_uri,
            # gzipped HTML as a Binary attribute (decompressed on read).
            "htmlGz": html_gz,
            "mimeType": mime_type,
            "csp": csp or {},
            "permissions": permissions or {},
            # Persisted because the read side (app-api messages endpoint)
            # cannot recompute it — it's the inference-side env config and
            # app-api may not have it wired. Served back verbatim on reload.
            "sandboxOrigin": sandbox_origin or "",
            # Server identity for the App header — round-tripped verbatim on
            # reload (the read side copies all non-key attrs back onto the
            # resource), so a refreshed conversation re-shows the same header.
            "serverName": server_name or "",
            # Only persist a SMALL icon (e.g. a URL). A large base64 `data:` URI
            # (the auto-fetched server-manifest logo, ~100KB+) would risk the
            # 400KB DynamoDB item limit alongside the gzipped HTML and regress
            # HTML persistence — so it's dropped here; the live event still
            # carried it, and reload falls back to the generic glyph.
            "icon": icon if (icon and len(icon) <= 8192) else "",
            "toolName": tool_name or "",
            "createdAt": created_at,
            "producedByMessageIndex": produced_by_message_index,
            "ttl": ttl,
        }
        try:
            self._table.put_item(Item=_floats_to_decimal(item))
            logger.info(
                "mcp-apps ui-resource store: persisted resource "
                "(session=%s, toolUseId=%s, %d bytes html -> %d gzipped)",
                session_id,
                tool_use_id,
                len(raw),
                len(html_gz),
            )
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.warning(
                "mcp-apps ui-resource store: failed to persist resource "
                "(session=%s, toolUseId=%s)",
                session_id,
                tool_use_id,
                exc_info=True,
            )

    def list_for_session(
        self, *, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """Return this user's MCP App UI resources for a session.

        Queried off the session GSI then re-filtered by `userId` so a guessed
        session id can't surface another user's resources (mirrors the card
        store / Artifacts ownership re-check). Oldest-first for stable order.
        """
        if self._table is None:
            return []
        try:
            items: List[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}")
                & Key("GSI_SK").begins_with("UIRES#"),
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
                "mcp-apps ui-resource store: query failed (session=%s)",
                session_id,
                exc_info=True,
            )
            return []

        resources: List[Dict[str, Any]] = []
        for item in items:
            if item.get("userId") != user_id:
                continue  # ownership re-check (guessed session id)
            html_gz = item.get("htmlGz")
            resource = _decimal_to_native(
                {
                    k: v
                    for k, v in item.items()
                    if k not in _KEY_ATTRS and k != "htmlGz"
                }
            )
            # Decompress back to the inline `html` the SSE-event shape carries,
            # so the messages-endpoint sidecar needs no special handling. A
            # corrupt row is dropped rather than surfacing garbage to the App.
            if html_gz is not None:
                try:
                    resource["html"] = gzip.decompress(_to_bytes(html_gz)).decode(
                        "utf-8"
                    )
                except Exception:  # noqa: BLE001 - skip a corrupt row
                    logger.warning(
                        "mcp-apps ui-resource store: failed to decompress html "
                        "(session=%s)",
                        session_id,
                        exc_info=True,
                    )
                    continue
            resources.append(resource)
        return resources


_store: Optional[UiResourceStore] = None


def get_ui_resource_store() -> UiResourceStore:
    """Get or create the process-global UI-resource store."""
    global _store
    if _store is None:
        _store = UiResourceStore()
    return _store
