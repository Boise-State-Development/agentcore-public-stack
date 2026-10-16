"""The artifacts a session produced, at HEAD, for a share's snapshot body.

One GSI query (``SessionIndex`` on the artifacts table, which only HEAD rows
carry), shared by app-api's artifact service and the scheduled-runs worker
(Shared Projects 3.2b), which snapshots a delivered run without app-api.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

SESSION_INDEX = "SessionIndex"


def artifacts_table():
    """The artifacts table, or None while ``DYNAMODB_ARTIFACTS_TABLE_NAME`` is unset."""
    import boto3

    name = os.environ.get("DYNAMODB_ARTIFACTS_TABLE_NAME", "")
    return boto3.resource("dynamodb").Table(name) if name else None


def session_artifact_heads(table: Any, *, user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Each artifact the session produced, at HEAD, newest first: one row per artifact.

    ``user_id`` filters rather than keys, because ``SessionIndex`` is partitioned by
    session and is NOT user-scoped. Dropping that filter would let a borrowed
    session id enumerate somebody else's artifacts. Raises the client's error.
    """
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "IndexName": SESSION_INDEX,
        "KeyConditionExpression": Key("GSI1PK").eq(f"SESSION#{session_id}"),
        "ScanIndexForward": False,  # GSI1SK embeds updated_at → newest first
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last: Optional[Dict[str, Any]] = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    heads: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        artifact_id = str(item.get("artifact_id", ""))
        if not artifact_id or item.get("user_id") != user_id or artifact_id in seen:
            continue
        seen.add(artifact_id)
        produced_by = item.get("produced_by_message_index")
        heads.append(
            {
                "artifact_id": artifact_id,
                "version": int(item.get("version", 1)),
                "title": str(item.get("title", "")),
                "content_type": str(item.get("content_type", "text/html; charset=utf-8")),
                "produced_by_message_index": int(produced_by) if produced_by is not None else None,
            }
        )
    return heads
