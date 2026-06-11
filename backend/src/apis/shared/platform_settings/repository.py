"""DynamoDB repository for platform-wide chat-mode settings.

Stores a single sentinel item in the auth-providers table, following the
``SYSTEM_SETTINGS#first-boot`` convention (`app_api/system/repository.py`).
Both app-api and the inference-api runtime already have this table's name
in their environment (``DYNAMODB_AUTH_PROVIDERS_TABLE_NAME``) and IAM read
access, so no new infrastructure is required.
"""

import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .models import ChatModeSettings

logger = logging.getLogger(__name__)

CHAT_MODE_PK = "SYSTEM_SETTINGS#chat-mode"
CHAT_MODE_SK = "SYSTEM_SETTINGS#chat-mode"


class PlatformSettingsRepository:
    """Repository for platform-settings sentinel items."""

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
                "Platform settings repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)

        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized platform settings repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get_chat_mode_settings(self) -> Optional[ChatModeSettings]:
        """Read the stored chat-mode settings, or None if never written."""
        if not self._enabled:
            return None

        try:
            response = self._table.get_item(
                Key={"PK": CHAT_MODE_PK, "SK": CHAT_MODE_SK}
            )
            item = response.get("Item")
            if item is None:
                return None
            return ChatModeSettings.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error reading chat-mode settings: {e}")
            raise

    async def put_chat_mode_settings(self, settings: ChatModeSettings) -> None:
        """Write the chat-mode settings item (full replace)."""
        if not self._enabled:
            raise RuntimeError("Platform settings repository is not enabled")

        item = {
            "PK": CHAT_MODE_PK,
            "SK": CHAT_MODE_SK,
            **settings.to_dynamo_item(),
        }
        self._table.put_item(Item=item)
        logger.info(
            f"Chat-mode settings updated: default_mode={settings.default_mode}, "
            f"allow_mode_toggle={settings.allow_mode_toggle}"
        )


# Singleton instance
_repository: Optional[PlatformSettingsRepository] = None


def get_platform_settings_repository() -> PlatformSettingsRepository:
    """Get the platform settings repository singleton."""
    global _repository
    if _repository is None:
        _repository = PlatformSettingsRepository()
    return _repository
