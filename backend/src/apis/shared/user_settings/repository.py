"""DynamoDB repository for user settings."""

import boto3
from botocore.exceptions import ClientError
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Default settings returned when no record exists
DEFAULT_SETTINGS = {
    "defaultModelId": None,
}


class UserSettingsRepository:
    """DynamoDB repository for user settings operations.

    Table Schema:
        PK: USER#<user_id>
        SK: SETTINGS
    """

    def __init__(self, table_name: Optional[str] = None):
        """Initialize repository with table name from env or parameter."""
        if table_name is None:
            table_name = os.getenv("DYNAMODB_USER_SETTINGS_TABLE_NAME", "")

        self._table_name = table_name
        self._enabled = bool(table_name)

        if self._enabled:
            self.dynamodb = boto3.resource("dynamodb")
            self.table = self.dynamodb.Table(table_name)
            logger.info(f"UserSettingsRepository initialized with table: {table_name}")
        else:
            self.dynamodb = None
            self.table = None
            logger.info("UserSettingsRepository disabled - no table configured")

    @property
    def enabled(self) -> bool:
        """Check if repository is enabled."""
        return self._enabled

    async def get_settings(self, user_id: str) -> dict:
        """Get user settings, returning defaults if none exist."""
        if not self._enabled:
            return dict(DEFAULT_SETTINGS)

        try:
            response = self.table.get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": "SETTINGS",
                }
            )

            if "Item" not in response:
                return dict(DEFAULT_SETTINGS)

            item = response["Item"]
            return {
                "defaultModelId": item.get("defaultModelId"),
            }
        except ClientError as e:
            logger.error(f"Error getting settings for user {user_id}: {e}")
            return dict(DEFAULT_SETTINGS)

    async def update_settings(self, user_id: str, settings: dict) -> dict:
        """Update user settings with read-modify-write merge semantics."""
        if not self._enabled:
            logger.warning("User settings table not configured - returning defaults")
            merged = dict(DEFAULT_SETTINGS)
            merged.update(settings)
            return merged

        current = await self.get_settings(user_id)
        current.update(settings)

        item = {
            "PK": f"USER#{user_id}",
            "SK": "SETTINGS",
        }
        for key, value in current.items():
            if value is not None:
                item[key] = value

        try:
            self.table.put_item(Item=item)

            # Remove attributes that were explicitly set to None
            remove_keys = [k for k, v in current.items() if v is None]
            if remove_keys:
                self.table.update_item(
                    Key={"PK": f"USER#{user_id}", "SK": "SETTINGS"},
                    UpdateExpression="REMOVE " + ", ".join(f"#{k}" for k in remove_keys),
                    ExpressionAttributeNames={f"#{k}": k for k in remove_keys},
                )

            logger.debug(f"Updated settings for user {user_id}")
            return current
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                logger.warning(f"User settings table does not exist yet - returning settings without persisting")
                return current
            logger.error(f"Error updating settings for user {user_id}: {e}")
            raise
