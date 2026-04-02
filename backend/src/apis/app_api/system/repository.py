"""DynamoDB repository for system settings (first-boot state)."""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# DynamoDB key constants
FIRST_BOOT_PK = "SYSTEM_SETTINGS#first-boot"
FIRST_BOOT_SK = "SYSTEM_SETTINGS#first-boot"


class SystemSettingsRepository:
    """
    Repository for system settings CRUD operations in DynamoDB.

    Stores the first-boot completion state using a single-item pattern
    in the auth providers table. Uses conditional writes for race
    condition protection on first-boot completion.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self._table_name = table_name or os.getenv("DYNAMODB_AUTH_PROVIDERS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_AUTH_PROVIDERS_TABLE_NAME not set. "
                "System settings repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)

        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized system settings repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get_first_boot_status(self) -> Optional[Dict[str, Any]]:
        """
        Get the first-boot completion status.

        Returns:
            Dict with first-boot item attributes if completed, None if
            the item does not exist (system not yet bootstrapped).
        """
        if not self._enabled:
            return None

        try:
            response = self._table.get_item(
                Key={"PK": FIRST_BOOT_PK, "SK": FIRST_BOOT_SK}
            )
            return response.get("Item")
        except ClientError as e:
            logger.error(f"Error reading first-boot status: {e}")
            raise

    async def mark_first_boot_completed(
        self,
        user_id: str,
        username: str,
        email: str,
    ) -> None:
        """
        Mark first-boot as completed with an atomic conditional write.

        Uses ``attribute_not_exists(PK)`` so that exactly one concurrent
        caller succeeds. All others receive a
        ``ConditionalCheckFailedException`` which the caller should
        translate to HTTP 409 Conflict.

        Args:
            user_id: The Cognito user ID of the admin user.
            username: The admin username.
            email: The admin email address.

        Raises:
            ClientError: With code ``ConditionalCheckFailedException``
                when first-boot has already been completed.
        """
        if not self._enabled:
            raise RuntimeError("System settings repository is not enabled")

        now = datetime.now(timezone.utc).isoformat()

        self._table.put_item(
            Item={
                "PK": FIRST_BOOT_PK,
                "SK": FIRST_BOOT_SK,
                "completed": True,
                "completedAt": now,
                "completedBy": user_id,
                "adminUsername": username,
                "adminEmail": email,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )

        logger.info(f"First-boot completed by user_id={user_id}")


# Singleton instance
_repository: Optional[SystemSettingsRepository] = None


def get_system_settings_repository() -> SystemSettingsRepository:
    """Get the system settings repository singleton."""
    global _repository
    if _repository is None:
        _repository = SystemSettingsRepository()
    return _repository
