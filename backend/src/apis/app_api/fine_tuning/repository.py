"""DynamoDB repository for the fine-tuning access control table."""

import os
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List

import boto3
from botocore.exceptions import ClientError

from . import pricing

logger = logging.getLogger(__name__)

#: Default dollar quota for a new grant.  Approximately what the previous
#: 10 GPU-hour default bought on the ml.g5.xlarge every job actually ran on.
DEFAULT_QUOTA_USD = 15.0

#: Rate used to convert a legacy hours-denominated grant to dollars.  Every
#: grant written before the dollar quota existed was spent on ml.g5.xlarge —
#: it was the only default — so its training rate is the faithful conversion.
LEGACY_HOURS_TO_USD = pricing.training_rate("ml.g5.xlarge") or 1.408


class FineTuningAccessRepository:
    """Repository for the fine-tuning-access DynamoDB table.

    Table schema:
        PK: EMAIL#{email}  (lowercase)
        SK: ACCESS          (fixed literal)

    Attributes:
        email, granted_by, granted_at, monthly_quota_usd,
        current_month_usage_usd, quota_period (YYYY-MM)

    **Legacy grants.**  Records written before the quota moved to dollars
    carry ``monthly_quota_hours``/``current_month_usage_hours`` instead.  They
    are converted on read and written back on the next
    :meth:`check_and_reset_quota`, so the migration is lazy and self-healing
    rather than a backfill script that has to be run in every environment.
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

    @staticmethod
    def _needs_migration(item: dict) -> bool:
        """True when a record predates the dollar quota."""
        return "monthly_quota_usd" not in item and "monthly_quota_hours" in item

    def _item_to_dict(self, item: dict) -> dict:
        """Convert a DynamoDB item to a plain dict, Decimals to float.

        Converts a legacy hours-denominated grant to dollars on the way out so
        callers only ever see one shape.
        """
        if self._needs_migration(item):
            quota = float(item.get("monthly_quota_hours", 10)) * LEGACY_HOURS_TO_USD
            usage = float(item.get("current_month_usage_hours", 0)) * LEGACY_HOURS_TO_USD
        else:
            quota = float(item.get("monthly_quota_usd", DEFAULT_QUOTA_USD))
            usage = float(item.get("current_month_usage_usd", 0))

        return {
            "email": item["email"],
            "granted_by": item.get("granted_by", ""),
            "granted_at": item.get("granted_at", ""),
            "monthly_quota_usd": round(quota, 4),
            "current_month_usage_usd": round(usage, 4),
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
        monthly_quota_usd: float = DEFAULT_QUOTA_USD,
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
            "monthly_quota_usd": Decimal(str(monthly_quota_usd)),
            "current_month_usage_usd": Decimal("0"),
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

    def update_quota(self, email: str, monthly_quota_usd: float) -> Optional[dict]:
        """Update the monthly dollar quota for a user. Returns None if not found."""
        try:
            response = self._table.update_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                UpdateExpression=(
                    "SET monthly_quota_usd = :mq REMOVE monthly_quota_hours"
                ),
                ExpressionAttributeValues={
                    ":mq": Decimal(str(monthly_quota_usd)),
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
        """Check quota, lazily resetting it when a new month has started.

        Also completes the hours-to-dollars migration for a legacy record, so
        a grant is rewritten in the new shape the first time its owner is seen
        rather than by a backfill script run per environment.

        Returns the (possibly updated) access grant, or None if not found.
        """
        try:
            response = self._table.get_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"}
            )
            raw = response.get("Item")
        except ClientError as e:
            logger.error(f"Error getting access for {email}: {e}")
            raise

        if raw is None:
            return None

        item = self._item_to_dict(raw)
        current_period = self._current_period()
        needs_migration = self._needs_migration(raw)
        new_period = item["quota_period"] != current_period

        if not needs_migration and not new_period:
            return item

        # A new month zeroes usage; a migration carries the converted usage
        # across so a user cannot reset their own spend by being migrated.
        usage = Decimal("0") if new_period else Decimal(str(item["current_month_usage_usd"]))

        update = (
            "SET monthly_quota_usd = :quota, "
            "current_month_usage_usd = :usage, "
            "quota_period = :period "
            "REMOVE monthly_quota_hours, current_month_usage_hours"
        )

        try:
            response = self._table.update_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                UpdateExpression=update,
                ExpressionAttributeValues={
                    ":quota": Decimal(str(item["monthly_quota_usd"])),
                    ":usage": usage,
                    ":period": current_period,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            logger.error(f"Error resetting quota for {email}: {e}")
            raise

        if needs_migration:
            logger.info(
                f"Migrated {email.lower()} to a dollar quota: "
                f"${item['monthly_quota_usd']:.2f}/month"
            )
        if new_period:
            logger.info(f"Reset quota for {email.lower()} to period {current_period}")

        return self._item_to_dict(response["Attributes"])

    def increment_usage(self, email: str, usd: float) -> Optional[dict]:
        """Atomically add spend to current_month_usage_usd."""
        try:
            response = self._table.update_item(
                Key={"PK": self._make_pk(email), "SK": "ACCESS"},
                UpdateExpression="ADD current_month_usage_usd :usd",
                ExpressionAttributeValues={
                    ":usd": Decimal(str(usd)),
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
