"""DynamoDB repository for feature announcements and per-user acknowledgements.

Two access patterns, one table:

  - announcements: ``query`` on the fixed ``ANNOUNCEMENTS`` partition, exactly
    as ``UserMenuLinksRepository.list_links`` does. Volume is tens of items, so
    no GSI.
  - acknowledgements: ``query`` on ``USER#<id>`` with ``begins_with(SK, "ACK#")``,
    bounded by the number of announcements a user has ever interacted with.

The write worth reading closely is :meth:`record_ack`.
"""

import logging
import os
import uuid
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from apis.shared.timestamps import utc_now_iso

from .models import (
    ACK_SK_PREFIX,
    ANNOUNCEMENT_SK_PREFIX,
    ANNOUNCEMENTS_PK,
    Announcement,
    AnnouncementAck,
    AnnouncementCreate,
    AnnouncementUpdate,
    action_rank,
    validate_announcement_invariants,
)
from apis.shared.user_menu_links.models import validate_http_url

logger = logging.getLogger(__name__)


class AnnouncementsRepository:
    """CRUD for announcements + the monotonic ack write path."""

    def __init__(self, table_name: Optional[str] = None, region: Optional[str] = None):
        self._table_name = table_name or os.getenv("DYNAMODB_ANNOUNCEMENTS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_ANNOUNCEMENTS_TABLE_NAME not set. "
                "Announcements repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized announcements repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Announcements
    # ------------------------------------------------------------------

    async def list_announcements(
        self, states: Optional[List[str]] = None
    ) -> List[Announcement]:
        if not self._enabled:
            return []

        kwargs = dict(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": ANNOUNCEMENTS_PK,
                ":sk": ANNOUNCEMENT_SK_PREFIX,
            },
        )
        try:
            response = self._table.query(**kwargs)
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs
                )
                items.extend(response.get("Items", []))
        except ClientError:
            logger.error("Error listing announcements", exc_info=True)
            raise

        announcements = [Announcement.from_dynamo_item(item) for item in items]
        if states:
            wanted = set(states)
            announcements = [a for a in announcements if a.state in wanted]
        # Newest first — the admin list and the What's-New panel both read
        # reverse-chronologically.
        announcements.sort(key=lambda a: (a.publish_at, a.created_at), reverse=True)
        return announcements

    async def get_announcement(self, announcement_id: str) -> Optional[Announcement]:
        if not self._enabled:
            return None
        try:
            response = self._table.get_item(
                Key={
                    "PK": ANNOUNCEMENTS_PK,
                    "SK": f"{ANNOUNCEMENT_SK_PREFIX}{announcement_id}",
                }
            )
            item = response.get("Item")
            if not item:
                return None
            return Announcement.from_dynamo_item(item)
        except ClientError:
            logger.error("Error getting announcement", exc_info=True)
            raise

    async def create_announcement(
        self, data: AnnouncementCreate, created_by: Optional[str] = None
    ) -> Announcement:
        if not self._enabled:
            raise RuntimeError("Announcements repository is not enabled")

        now = utc_now_iso()
        announcement = Announcement(
            announcement_id=str(uuid.uuid4()),
            title=data.title,
            body_markdown=data.body_markdown,
            summary=data.summary,
            surfaces=list(data.surfaces),
            severity=data.severity,
            state=data.state,
            publish_at=data.publish_at or now,
            expires_at=data.expires_at,
            target_roles=list(data.target_roles),
            show_to_new_users=data.show_to_new_users,
            requires_ack=data.requires_ack,
            cta_label=data.cta_label,
            cta_url=data.cta_url,
            revision=1,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        try:
            self._table.put_item(
                Item=announcement.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError:
            logger.error("Error creating announcement", exc_info=True)
            raise

        logger.info(f"Created announcement: {announcement.announcement_id}")
        return announcement

    async def update_announcement(
        self, announcement_id: str, updates: AnnouncementUpdate
    ) -> Optional[Announcement]:
        """Partial update. ``revision`` is untouched by design (§D4) — a typo
        fix must not re-fire a modal at the whole user base."""
        if not self._enabled:
            return None

        existing = await self.get_announcement(announcement_id)
        if not existing:
            return None

        for field_name, value in updates.model_dump(exclude_none=True).items():
            setattr(existing, field_name, value)
        existing.updated_at = utc_now_iso()

        # Re-validate the merged record: a PATCH that only adds `banner` to
        # `surfaces` is individually valid and jointly not.
        validate_announcement_invariants(
            surfaces=list(existing.surfaces),
            expires_at=existing.expires_at,
            publish_at=existing.publish_at,
            cta_label=existing.cta_label,
            cta_url=existing.cta_url,
        )
        validate_http_url(existing.cta_url)

        try:
            self._table.put_item(Item=existing.to_dynamo_item())
        except ClientError:
            logger.error("Error updating announcement", exc_info=True)
            raise

        logger.info(f"Updated announcement: {announcement_id}")
        return existing

    async def set_state(
        self, announcement_id: str, state: str
    ) -> Optional[Announcement]:
        if not self._enabled:
            return None
        existing = await self.get_announcement(announcement_id)
        if not existing:
            return None
        existing.state = state
        existing.updated_at = utc_now_iso()
        try:
            self._table.put_item(Item=existing.to_dynamo_item())
        except ClientError:
            logger.error("Error updating announcement state", exc_info=True)
            raise
        logger.info(f"Announcement {announcement_id} state -> {state}")
        return existing

    async def bump_revision(self, announcement_id: str) -> Optional[Announcement]:
        """"Show this again" (§D4).

        Every user's suppression lapses at once because their acks are keyed by
        the old revision. The R1 acks stay readable, which is what lets the
        panel mark the entry *Updated* rather than plain unread.
        """
        if not self._enabled:
            return None
        existing = await self.get_announcement(announcement_id)
        if not existing:
            return None
        existing.revision = int(existing.revision) + 1
        existing.updated_at = utc_now_iso()
        try:
            self._table.put_item(Item=existing.to_dynamo_item())
        except ClientError:
            logger.error("Error bumping announcement revision", exc_info=True)
            raise
        logger.info(
            f"Announcement {announcement_id} revision -> {existing.revision}"
        )
        return existing

    async def delete_announcement(self, announcement_id: str) -> bool:
        if not self._enabled:
            return False
        existing = await self.get_announcement(announcement_id)
        if not existing:
            return False
        try:
            self._table.delete_item(
                Key={
                    "PK": ANNOUNCEMENTS_PK,
                    "SK": f"{ANNOUNCEMENT_SK_PREFIX}{announcement_id}",
                }
            )
        except ClientError:
            logger.error("Error deleting announcement", exc_info=True)
            raise
        logger.info(f"Deleted announcement: {announcement_id}")
        return True

    # ------------------------------------------------------------------
    # Acknowledgements
    # ------------------------------------------------------------------

    async def record_ack(
        self,
        *,
        user_id: str,
        announcement_id: str,
        revision: int,
        action: str,
        surface: str,
        ttl: Optional[int] = None,
    ) -> bool:
        """Record an ack, **monotonically** (§D2).

        Returns True if this write raised the stored rank, False if the guard
        rejected it because an equal-or-stronger action was already recorded.
        **False is success, not an error.** ``seen`` is written automatically
        the moment a surface renders, so it races the user's click on the ✕;
        without the condition a late ``seen`` would overwrite ``dismissed`` and
        the banner would come back on the next load. The guard belongs in the
        database, not in application ordering — see #741 / #751, whose shape
        was exactly this.
        """
        if not self._enabled:
            return False

        rank = action_rank(action)
        now = utc_now_iso()

        expression_values = {
            ":rank": rank,
            ":action": action,
            ":now": now,
            ":announcementId": announcement_id,
            ":revision": int(revision),
            ":surface": surface,
        }
        set_clause = (
            "SET actionRank = :rank, #action = :action, actionAt = :now, "
            "announcementId = :announcementId, #revision = :revision, "
            "#surface = :surface"
        )
        names = {
            "#action": "action",       # reserved word
            "#revision": "revision",
            "#surface": "surface",
        }
        if ttl is None:
            # A compliance-bearing ack (§5): clear any TTL a weaker earlier
            # action may have set, rather than letting it expire the record.
            update_expression = f"{set_clause} REMOVE #ttl"
            names["#ttl"] = "ttl"
        else:
            update_expression = f"{set_clause}, #ttl = :ttl"
            names["#ttl"] = "ttl"
            expression_values[":ttl"] = int(ttl)

        try:
            self._table.update_item(
                Key={
                    "PK": AnnouncementAck.partition_key(user_id),
                    "SK": AnnouncementAck.sort_key(announcement_id, revision),
                },
                UpdateExpression=update_expression,
                ConditionExpression=(
                    "attribute_not_exists(actionRank) OR actionRank < :rank"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=expression_values,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Already at or above this rank. Nothing to do, and nothing wrong.
                logger.debug(
                    "Ack not raised (already >= rank): user=%s announcement=%s "
                    "revision=%s action=%s",
                    user_id,
                    announcement_id,
                    revision,
                    action,
                )
                return False
            logger.error("Error recording announcement ack", exc_info=True)
            raise
        return True

    async def list_acks(self, user_id: str) -> List[AnnouncementAck]:
        """Every ack this user has ever written, across revisions."""
        if not self._enabled:
            return []

        kwargs = dict(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": AnnouncementAck.partition_key(user_id),
                ":sk": ACK_SK_PREFIX,
            },
        )
        try:
            response = self._table.query(**kwargs)
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs
                )
                items.extend(response.get("Items", []))
        except ClientError:
            logger.error("Error listing announcement acks", exc_info=True)
            raise

        return [AnnouncementAck.from_dynamo_item(item) for item in items]

    async def get_ack(
        self, user_id: str, announcement_id: str, revision: int
    ) -> Optional[AnnouncementAck]:
        if not self._enabled:
            return None
        try:
            response = self._table.get_item(
                Key={
                    "PK": AnnouncementAck.partition_key(user_id),
                    "SK": AnnouncementAck.sort_key(announcement_id, revision),
                }
            )
            item = response.get("Item")
            if not item:
                return None
            return AnnouncementAck.from_dynamo_item(item)
        except ClientError:
            logger.error("Error getting announcement ack", exc_info=True)
            raise


_repository: Optional[AnnouncementsRepository] = None


def get_announcements_repository() -> AnnouncementsRepository:
    global _repository
    if _repository is None:
        _repository = AnnouncementsRepository()
    return _repository
