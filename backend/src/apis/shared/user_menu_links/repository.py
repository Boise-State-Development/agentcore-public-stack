"""DynamoDB repository for admin-managed user-menu links."""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import UserMenuLink, UserMenuLinkCreate, UserMenuLinkUpdate

logger = logging.getLogger(__name__)


class UserMenuLinksRepository:
    """CRUD for user-menu links in DynamoDB.

    Single-table design, fixed PK ``USER_MENU_LINKS``. All items are queried
    with a single ``query`` by PK; no GSI needed.
    """

    def __init__(self, table_name: Optional[str] = None, region: Optional[str] = None):
        self._table_name = table_name or os.getenv("DYNAMODB_USER_MENU_LINKS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_USER_MENU_LINKS_TABLE_NAME not set. "
                "User menu links repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized user-menu links repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def list_links(self, enabled_only: bool = False) -> List[UserMenuLink]:
        if not self._enabled:
            return []

        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={":pk": "USER_MENU_LINKS", ":sk": "LINK#"},
            )
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                    ExpressionAttributeValues={":pk": "USER_MENU_LINKS", ":sk": "LINK#"},
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
        except ClientError:
            logger.error("Error listing user-menu links", exc_info=True)
            raise

        links = [UserMenuLink.from_dynamo_item(item) for item in items]
        if enabled_only:
            links = [link for link in links if link.enabled]
        links.sort(key=lambda link: (link.order, link.label.lower()))
        return links

    async def get_link(self, link_id: str) -> Optional[UserMenuLink]:
        if not self._enabled:
            return None
        try:
            response = self._table.get_item(
                Key={"PK": "USER_MENU_LINKS", "SK": f"LINK#{link_id}"}
            )
            item = response.get("Item")
            if not item:
                return None
            return UserMenuLink.from_dynamo_item(item)
        except ClientError:
            logger.error("Error getting user-menu link", exc_info=True)
            raise

    async def create_link(
        self, data: UserMenuLinkCreate, created_by: Optional[str] = None
    ) -> UserMenuLink:
        if not self._enabled:
            raise RuntimeError("User-menu links repository is not enabled")

        now = datetime.now(timezone.utc).isoformat() + "Z"
        link = UserMenuLink(
            link_id=str(uuid.uuid4()),
            label=data.label,
            kind=data.kind,
            enabled=data.enabled,
            order=data.order,
            url=data.url,
            body_markdown=data.body_markdown,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        try:
            self._table.put_item(
                Item=link.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError:
            logger.error("Error creating user-menu link", exc_info=True)
            raise

        logger.info(f"Created user-menu link: {link.link_id}")
        return link

    async def update_link(
        self, link_id: str, updates: UserMenuLinkUpdate
    ) -> Optional[UserMenuLink]:
        if not self._enabled:
            return None

        existing = await self.get_link(link_id)
        if not existing:
            return None

        update_fields = updates.model_dump(exclude_none=True)
        for field_name, value in update_fields.items():
            setattr(existing, field_name, value)
        existing.updated_at = datetime.now(timezone.utc).isoformat() + "Z"

        # Re-validate the merged kind/url/body_markdown invariant before persist.
        if existing.kind == "external" and not existing.url:
            raise ValueError("external links require url")
        if existing.kind == "modal" and not existing.body_markdown:
            raise ValueError("modal links require body_markdown")
        if existing.url:
            lowered = existing.url.strip().lower()
            if not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError("url must start with http:// or https://")

        try:
            self._table.put_item(Item=existing.to_dynamo_item())
        except ClientError:
            logger.error("Error updating user-menu link", exc_info=True)
            raise

        logger.info(f"Updated user-menu link: {link_id}")
        return existing

    async def delete_link(self, link_id: str) -> bool:
        if not self._enabled:
            return False
        existing = await self.get_link(link_id)
        if not existing:
            return False
        try:
            self._table.delete_item(
                Key={"PK": "USER_MENU_LINKS", "SK": f"LINK#{link_id}"}
            )
        except ClientError:
            logger.error("Error deleting user-menu link", exc_info=True)
            raise
        logger.info(f"Deleted user-menu link: {link_id}")
        return True


_repository: Optional[UserMenuLinksRepository] = None


def get_user_menu_links_repository() -> UserMenuLinksRepository:
    global _repository
    if _repository is None:
        _repository = UserMenuLinksRepository()
    return _repository
