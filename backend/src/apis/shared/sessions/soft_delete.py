"""Soft-deleting a conversation's metadata row, shared by app-api and the scheduled-runs worker.

The row leaves every listing and stays readable as a tombstone (``status: deleted``);
its ``C#`` cost rows are separate items and are never touched, so quota, the
release ledger and audit still count the conversation. app-api's
``SessionService.delete_session`` calls this (and keeps the cascades: files,
memory events, handoffs); the scheduled-runs worker calls it for a quiet
watcher run (docs/specs/quiet-watcher.md §3.3), whose conversation must leave
nothing behind.

Blocking (one Query and one or two writes): callers on an event loop use
``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from apis.shared.aws_clients import get_dynamodb_table
from apis.shared.sessions.metadata import _get_session_by_gsi_sync, _static_session_sk

logger = logging.getLogger(__name__)

#: Sparse index keys that list a session (recency, project, pinned). Live rows
#: may carry them; a soft-deleted row never should.
LISTING_KEY_ATTRS = ("GSI4_PK", "GSI4_SK", "GSI5_PK", "GSI5_SK", "GSI6_PK", "GSI6_SK")


def _float_to_decimal(obj: Any) -> Any:
    """Recursively convert float to Decimal: ``put_item`` refuses floats."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _float_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_float_to_decimal(item) for item in obj]
    return obj


def _default_table() -> Any:
    name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "SessionsMetadata")
    return get_dynamodb_table(name)


def soft_delete_session_row(user_id: str, session_id: str, table: Optional[Any] = None) -> bool:
    """Soft-delete the user's session row. Returns ``False`` only when there is no such row.

    A row already deleted returns ``True``, and loses any listing keys a write
    after the delete put back (#1764), so deleting again repairs it. A row under
    the static SK (``S#{session_id}``) is flipped in place; a legacy row is moved
    to the static tombstone. Other errors raise: the caller decides how to fail.
    """
    table = table if table is not None else _default_table()

    # Resolve the raw row (with its actual SK) via GSI. Do NOT reconstruct the
    # SK from lastMessageAt — under the static-SK schema (issue #175) a migrated
    # row lives at S#{session_id}, not S#ACTIVE#{lastMessageAt}#{id}, so a
    # reconstructed key would miss it entirely.
    existing = _get_session_by_gsi_sync(session_id, user_id, table)
    if not existing:
        logger.info("Session not found for deletion")
        return False

    if existing.get("deleted") or existing.get("status") == "deleted":
        # A tombstone should carry no listing keys. If a write landed after the
        # delete and put some back (#1764), the conversation is in the sidebar
        # again and can't be opened or pinned; remove them so deleting again
        # repairs it.
        stale = [k for k in LISTING_KEY_ATTRS if k in existing]
        if stale:
            logger.warning("Deleted session still had listing keys %s; removing them", stale)
            table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": existing["SK"]},
                UpdateExpression="REMOVE " + ", ".join(stale),
            )
        else:
            logger.info("Session already deleted")
        return True

    deleted_at = datetime.now(timezone.utc).isoformat()
    pk = f"USER#{user_id}"
    old_sk = existing["SK"]
    target_sk = _static_session_sk(session_id)

    if old_sk == target_sk:
        # Already migrated — soft-delete in place: flip status + drop the sparse
        # recency keys (user and project) so the row leaves both active
        # listings. No row move.
        table.update_item(
            Key={"PK": pk, "SK": target_sk},
            UpdateExpression=(
                "SET #s = :d, deleted = :true, deletedAt = :da "
                "REMOVE GSI4_PK, GSI4_SK, GSI5_PK, GSI5_SK, GSI6_PK, GSI6_SK"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "deleted", ":true": True, ":da": deleted_at},
        )
    else:
        # Legacy row — migrate to the static tombstone (status=deleted, no
        # recency keys) and drop the old row. One-time move; carry existing fields.
        deleted_item = {
            k: v
            for k, v in existing.items()
            if k not in ("PK", "SK", "GSI4_PK", "GSI4_SK", "GSI5_PK", "GSI5_SK", "GSI6_PK", "GSI6_SK")
        }
        deleted_item.update({
            "PK": pk,
            "SK": target_sk,
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": "META",
            "sessionId": session_id,
            "userId": user_id,
            "status": "deleted",
            "deleted": True,
            "deletedAt": deleted_at,
        })
        # existing came back with Decimals converted to floats — put_item needs Decimal.
        table.put_item(Item=_float_to_decimal(deleted_item))
        table.delete_item(Key={"PK": pk, "SK": old_sk})

    logger.info("Soft-deleted session")
    return True


__all__ = ["LISTING_KEY_ATTRS", "soft_delete_session_row"]
