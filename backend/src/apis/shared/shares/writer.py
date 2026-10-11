"""Writing a conversation share: the snapshot body, the share row, and its project pointer.

The one writer for both callers that make shares. app-api's ``ShareService``
validates a person's request, reads the conversation, and calls :func:`write_share`;
the scheduled-runs worker (Shared Projects 3.2b) does the same for a scheduled
run delivered to its project. Neither can import the other (the worker never
imports ``apis.app_api``), so what they share lives here, with no app-api,
FastAPI or auth-package imports.

A share is a point-in-time copy: the body (metadata, messages, artifacts pinned
at their version, the tool-batch summaries) goes to S3 because a long
conversation outgrows DynamoDB's item limit, and the row keeps the control
fields plus a pointer to it. A project share also gets a ``SHARED_TASK#`` pointer
on the project, without which it is unlisted, so the two land together or the
share is rolled back.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apis.shared.projects.models import SharedTask
from apis.shared.projects.repository import ProjectRepository

from .snapshot_store import ShareSnapshotStore, ShareSnapshotStoreError

logger = logging.getLogger(__name__)

#: Stamped on the S3 body pointer, for forward migration if the body shape changes.
SNAPSHOT_SCHEMA_VERSION = 1


class ShareStorageError(Exception):
    """The snapshot body could not be stored, so no share was made."""


def shares_table():
    """The shared-conversations table, or None while ``SHARED_CONVERSATIONS_TABLE_NAME`` is unset."""
    import boto3

    name = os.environ.get("SHARED_CONVERSATIONS_TABLE_NAME", "")
    return boto3.resource("dynamodb").Table(name) if name else None


def share_title(item: Dict[str, Any]) -> str:
    """The title denormalized on the row (shares made before that carry none)."""
    return str(item.get("title") or "")


def project_pointer(item: Dict[str, Any]) -> SharedTask:
    return SharedTask(
        project_id=item["project_id"],
        session_id=item["session_id"],
        share_id=item["share_id"],
        owner_id=item["owner_id"],
        owner_email=item.get("owner_email", ""),
        title=share_title(item),
        shared_at=item["created_at"],
        note=item.get("note"),
        schedule_id=item.get("schedule_id"),
    )


def write_share(
    *,
    table,
    snapshot_store: ShareSnapshotStore,
    projects: Optional[ProjectRepository],
    session_id: str,
    owner_id: str,
    owner_email: str,
    access_level: str,
    metadata: Dict[str, Any],
    messages: List[Dict[str, Any]],
    artifacts: List[Dict[str, Any]],
    tool_summaries: List[Dict[str, Any]],
    allowed_emails: Optional[List[str]] = None,
    project_id: Optional[str] = None,
    note: Optional[str] = None,
    schedule_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Store the body, write the row and (for a project share) its pointer. Returns the row.

    Raises :class:`ShareStorageError` when the body can't be stored. A failed
    pointer write deletes the row and the body and re-raises: an unlisted project
    share is one nobody can find.
    """
    if not snapshot_store.enabled:
        raise ShareStorageError("Share storage is not configured")
    share_id = str(uuid.uuid4())
    body = json.dumps(
        {"metadata": metadata, "messages": messages, "artifacts": artifacts, "toolSummaries": tool_summaries}
    ).encode("utf-8")
    try:
        bucket_key = snapshot_store.put(share_id=share_id, body=body)
    except ShareSnapshotStoreError as e:
        raise ShareStorageError(str(e)) from e

    item: Dict[str, Any] = {
        "share_id": share_id,
        "session_id": session_id,
        "owner_id": owner_id,
        "owner_email": owner_email,
        "access_level": access_level,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Denormalized from the snapshot so a project pointer can be rebuilt from the row alone.
        "title": str(metadata.get("title") or ""),
        "body_ref": {
            "bucket_key": bucket_key,
            "format": "json",
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "byte_size": len(body),
        },
    }
    if allowed_emails is not None:
        item["allowed_emails"] = allowed_emails
    if project_id:
        item["project_id"] = project_id
    if note:
        item["note"] = note
    if schedule_id:
        item["schedule_id"] = schedule_id

    table.put_item(Item=item)
    if project_id:
        try:
            (projects or ProjectRepository()).put_shared_task(project_pointer(item))
        except Exception:
            logger.error("Project pointer write failed for share %s; rolling back", share_id, exc_info=True)
            delete_share(table, snapshot_store, item)
            raise
    return item


def delete_share(table, snapshot_store: ShareSnapshotStore, item: Dict[str, Any]) -> None:
    """Delete the row, then the body (best-effort: a stray body is unreachable without its row)."""
    table.delete_item(Key={"share_id": item["share_id"]})
    key = (item.get("body_ref") or {}).get("bucket_key")
    if key:
        snapshot_store.delete(key)

