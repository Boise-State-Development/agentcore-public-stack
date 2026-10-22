"""Write, list and mark notifications. Producers call :meth:`notify`, which never raises.

A notification is a courtesy about something that already happened (someone
was added to a project), so failing to write one must never fail that thing.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.shared.auth.models import User
from apis.shared.timestamps import utc_now_iso

from .models import RETENTION_DAYS, Notification, NotificationKind

logger = logging.getLogger(__name__)

INBOX_PK_PREFIX = "INBOX#"
NOTIF_SK_PREFIX = "NOTIF#"
_KEY_ATTRS = ("PK", "SK", "ttl")


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _pk(email: str) -> str:
    return f"{INBOX_PK_PREFIX}{_normalize(email)}"


def _new_id() -> str:
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:12]}"


class NotificationService:
    """The inbox on the projects table (``DYNAMODB_PROJECTS_TABLE_NAME``)."""

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name if table_name is not None else os.environ.get("DYNAMODB_PROJECTS_TABLE_NAME", "")
        self._table = None

    @property
    def enabled(self) -> bool:
        return bool(self.table_name)

    @property
    def table(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb").Table(self.table_name)
        return self._table

    # ── write ───────────────────────────────────────────────────────────

    def notify(
        self,
        *,
        recipient_email: str,
        kind: NotificationKind,
        actor: User,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Notification]:
        """Put one notification in ``recipient_email``'s inbox. Nobody is notified of their own action."""
        recipient = _normalize(recipient_email)
        if not self.enabled or not recipient or recipient == _normalize(actor.email):
            return None
        notification = self._build(recipient, kind, actor, project_id, project_name, payload)
        try:
            self.table.put_item(Item=self._to_item(notification))
            return notification
        except Exception:
            logger.warning("Could not write %s notification for project %s", kind, project_id, exc_info=True)
            return None

    def notify_many(
        self,
        recipient_emails: Iterable[str],
        *,
        kind: NotificationKind,
        actor: User,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Put the same notification in each inbox, in ``BatchWriteItem`` groups of 25.

        Duplicates and the actor are skipped. Returns how many were written; a
        failure is logged and the rest are skipped, never raised (see :meth:`notify`).
        """
        actor_email = _normalize(actor.email)
        recipients = sorted({r for r in map(_normalize, recipient_emails) if r and r != actor_email})
        if not self.enabled or not recipients:
            return 0
        written = 0
        try:
            with self.table.batch_writer() as batch:
                for recipient in recipients:
                    notification = self._build(recipient, kind, actor, project_id, project_name, payload)
                    batch.put_item(Item=self._to_item(notification))
                    written += 1
        except Exception:
            logger.warning(
                "Could not write all %s notifications for project %s (%d of %d queued)",
                kind, project_id, written, len(recipients), exc_info=True,
            )
        return written

    @staticmethod
    def _build(
        recipient: str,
        kind: NotificationKind,
        actor: User,
        project_id: Optional[str],
        project_name: Optional[str],
        payload: Optional[Dict[str, Any]],
    ) -> Notification:
        return Notification(
            notification_id=_new_id(),
            recipient_email=recipient,
            kind=kind,
            project_id=project_id,
            project_name=project_name,
            actor_email=_normalize(actor.email) or None,
            payload=payload or {},
            created_at=utc_now_iso(),
        )

    @staticmethod
    def _to_item(notification: Notification) -> Dict[str, Any]:
        item = notification.model_dump(by_alias=True, exclude_none=True)
        item.update(
            PK=_pk(notification.recipient_email),
            SK=f"{NOTIF_SK_PREFIX}{notification.notification_id}",
            ttl=int(time.time()) + RETENTION_DAYS * 86400,
        )
        return item

    # ── read ────────────────────────────────────────────────────────────

    def list(
        self, email: str, *, limit: int = 20, cursor: Optional[str] = None, unread_only: bool = False
    ) -> Tuple[List[Notification], Optional[str]]:
        """Newest first. ``cursor`` is the last notification id of the previous page."""
        if not self.enabled or not _normalize(email):
            return [], None
        from boto3.dynamodb.conditions import Attr, Key

        params: Dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(_pk(email)) & Key("SK").begins_with(NOTIF_SK_PREFIX),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if unread_only:
            params["FilterExpression"] = Attr("readAt").not_exists()
        if cursor:
            # The partition comes from the caller, never the cursor: a cursor
            # cannot page into someone else's inbox.
            params["ExclusiveStartKey"] = {"PK": _pk(email), "SK": f"{NOTIF_SK_PREFIX}{cursor}"}

        items: List[Notification] = []
        while True:
            resp = self.table.query(**params)
            items.extend(self._from_item(i) for i in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if len(items) >= limit or not last:
                break
            params["ExclusiveStartKey"] = last
        page = items[:limit]
        more = len(items) > limit or bool(last)
        return page, (page[-1].notification_id if more and page else None)

    def unread_count(self, email: str) -> int:
        if not self.enabled or not _normalize(email):
            return 0
        from boto3.dynamodb.conditions import Attr, Key

        params: Dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(_pk(email)) & Key("SK").begins_with(NOTIF_SK_PREFIX),
            "FilterExpression": Attr("readAt").not_exists(),
            "Select": "COUNT",
        }
        total = 0
        while True:
            resp = self.table.query(**params)
            total += int(resp.get("Count", 0))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return total
            params["ExclusiveStartKey"] = last

    # ── mark read ───────────────────────────────────────────────────────

    def mark_read(self, email: str, notification_id: str) -> bool:
        """Mark one read (idempotent). False if the caller has no such notification."""
        if not self.enabled or not _normalize(email):
            return False
        from botocore.exceptions import ClientError

        try:
            self.table.update_item(
                Key={"PK": _pk(email), "SK": f"{NOTIF_SK_PREFIX}{notification_id}"},
                UpdateExpression="SET readAt = if_not_exists(readAt, :now)",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":now": utc_now_iso()},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def mark_all_read(self, email: str) -> int:
        """Mark every unread notification read; returns how many there were."""
        marked = 0
        cursor = None
        while True:
            page, cursor = self.list(email, limit=100, cursor=cursor, unread_only=True)
            for n in page:
                marked += int(self.mark_read(email, n.notification_id))
            if not cursor:
                return marked

    @staticmethod
    def _from_item(item: Dict[str, Any]) -> Notification:
        return Notification.model_validate({k: v for k, v in item.items() if k not in _KEY_ATTRS})
