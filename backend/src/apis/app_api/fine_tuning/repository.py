"""DynamoDB repository for fine-tuning access control table."""

import os
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class FineTuningAccessRepository:
    """Repository for the fine-tuning-access DynamoDB table.

    Table schema:
        PK: EMAIL#{email}  (lowercase)
        SK: ACCESS          (fixed literal)

    Attributes:
        email, granted_by, granted_at, monthly_quota_hours,
        current_month_usage_hours, quota_period (YYYY-MM)
    """

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_FINE_TUNING_ACCESS_TABLE_NAME", "fine-tuning-access"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    @staticmethod
    def _make_pk(email: str) -> str:
        return f"EMAIL#{email.lower()}"

    @staticmethod
    def _current_period() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _item_to_dict(self, item: dict) -> dict:
        """Convert DynamoDB item to a plain dict, converting Decimals to float."""
        return {
            "email": item["email"],
            "granted_by": item.get("granted_by", ""),
            "granted_at": item.get("granted_at", ""),
            "monthly_quota_hours": float(item.get("monthly_quota_hours", 10)),
            "current_month_usage_hours": float(item.get("current_month_usage_hours", 0)),
            "quota_period": item.get("quota_period", ""),
        }

    def get_access(self, email: str) -> Optional[dict]:
        """Get access grant for an email. Returns None if not found."""
        try:
            response = self._table.get_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"}
            )
            item = response.get("Item")
            if not item:
                return None
            return self._item_to_dict(item)
        except ClientError as e:
            logger.error(f"Error getting access for {email}: {e}")
            raise

    def list_access(self) -> List[dict]:
        """List all access grants."""
        try:
            response = self._table.scan(
                FilterExpression="SK = :sk",
                ExpressionAttributeValues={":sk": "ACCESS"},
            )
            items = response.get("Items", [])

            while "LastEvaluatedKey" in response:
                response = self._table.scan(
                    FilterExpression="SK = :sk",
                    ExpressionAttributeValues={":sk": "ACCESS"},
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            return [self._item_to_dict(item) for item in items]
        except ClientError as e:
            logger.error(f"Error listing access grants: {e}")
            raise

    def grant_access(
        self,
        email: str,
        granted_by: str,
        monthly_quota_hours: float = 10.0,
    ) -> dict:
        """Grant fine-tuning access to an email.

        Raises ValueError if access already exists.
        """
        pk = self._make_pk(email)
        now = datetime.now(timezone.utc).isoformat()
        period = self._current_period()

        item = {
            "PK": pk,
            "SK": "ACCESS",
            "email": email.lower(),
            "granted_by": granted_by,
            "granted_at": now,
            "monthly_quota_hours": Decimal(str(monthly_quota_hours)),
            "current_month_usage_hours": Decimal("0"),
            "quota_period": period,
        }

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            logger.info(f"Granted fine-tuning access to {email.lower()} by {granted_by}")
            return self._item_to_dict(item)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Access already granted to {email.lower()}")
            raise

    def update_quota(self, email: str, monthly_quota_hours: float) -> Optional[dict]:
        """Update the monthly quota for a user. Returns None if not found."""
        try:
            response = self._table.update_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                UpdateExpression="SET monthly_quota_hours = :mq",
                ExpressionAttributeValues={
                    ":mq": Decimal(str(monthly_quota_hours)),
                },
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="ALL_NEW",
            )
            return self._item_to_dict(response["Attributes"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def revoke_access(self, email: str) -> bool:
        """Revoke access for an email. Returns False if not found."""
        try:
            self._table.delete_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                ConditionExpression="attribute_exists(PK)",
            )
            logger.info(f"Revoked fine-tuning access for {email.lower()}")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def check_and_reset_quota(self, email: str) -> Optional[dict]:
        """Check quota and lazily reset if a new month has started.

        Returns the (possibly updated) access grant, or None if not found.
        """
        item = self.get_access(email)
        if item is None:
            return None

        current_period = self._current_period()
        if item["quota_period"] != current_period:
            try:
                response = self._table.update_item(
                    Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                    UpdateExpression="SET current_month_usage_hours = :zero, quota_period = :period",
                    ExpressionAttributeValues={
                        ":zero": Decimal("0"),
                        ":period": current_period,
                    },
                    ReturnValues="ALL_NEW",
                )
                logger.info(f"Reset quota for {email.lower()} to period {current_period}")
                return self._item_to_dict(response["Attributes"])
            except ClientError as e:
                logger.error(f"Error resetting quota for {email}: {e}")
                raise

        return item

    def increment_usage(self, email: str, hours: float) -> Optional[dict]:
        """Atomically increment current_month_usage_hours."""
        try:
            response = self._table.update_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                UpdateExpression="ADD current_month_usage_hours :hours",
                ExpressionAttributeValues={
                    ":hours": Decimal(str(hours)),
                },
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="ALL_NEW",
            )
            return self._item_to_dict(response["Attributes"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise


# Singleton access
_repository_instance: Optional[FineTuningAccessRepository] = None


def get_fine_tuning_access_repository() -> FineTuningAccessRepository:
    """Get or create the global FineTuningAccessRepository instance."""
    global _repository_instance
    if _repository_instance is None:
        _repository_instance = FineTuningAccessRepository()
    return _repository_instance
