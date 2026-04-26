"""DynamoDB repository for OAuth disconnect intent.

Records that a user has explicitly disconnected from a provider, or that a
tool call surfaced a 401 against AgentCore Identity's vault token. Both
conditions mean the next consent flow must use `force_authentication=True`
so AgentCore replaces the vault entry rather than reusing it.

Lives in the same `oauth-user-tokens` table as user OAuth tokens (currently
unused by backend code) — the table already has `PK`/`SK` keys, KMS
encryption, and R/W IAM for the inference API. Items use a `DISCONNECT#`
sort-key prefix so they cannot collide with future per-user token storage.

The flag is durable so the disconnect intent survives across replicas: a
disconnect on one inference-API replica is visible to the next request,
which may land on a different replica.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class OAuthDisconnectRepository:
    """Per-(user, provider) disconnect flag backed by DynamoDB."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self._table_name = table_name or os.getenv("DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME not set. "
                "OAuth disconnect repository is disabled — disconnect intent "
                "will not be durable across replicas."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        session = boto3.Session(profile_name=profile) if profile else boto3
        self._dynamodb = session.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self._table_name)
        logger.info("Initialized OAuth disconnect repository: table=%s", self._table_name)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _key(user_id: str, provider_id: str) -> dict:
        return {
            "PK": f"USER#{user_id}",
            "SK": f"DISCONNECT#{provider_id}",
        }

    async def is_disconnected(self, user_id: str, provider_id: str) -> bool:
        """Return True if the user has been marked disconnected from `provider_id`.

        Failure-mode policy: if the read fails, return False. Treating an
        unreachable DDB as "disconnected" would lock every user out of every
        connector during a transient outage; treating it as "not disconnected"
        falls back to the AgentCore vault state — the prior, less-correct
        behavior, but still safe.
        """
        if not self._enabled:
            return False
        try:
            response = self._table.get_item(Key=self._key(user_id, provider_id))
            return "Item" in response
        except ClientError as e:
            logger.error(
                "Disconnect lookup failed for user=%s provider=%s: %s",
                user_id,
                provider_id,
                e,
            )
            return False

    async def mark_disconnected(self, user_id: str, provider_id: str) -> None:
        """Record that `(user_id, provider_id)` requires fresh consent.

        Idempotent — overwrites any prior `disconnected_at` if called twice.
        """
        if not self._enabled:
            return
        item = {
            **self._key(user_id, provider_id),
            "disconnected_at": datetime.now(timezone.utc).isoformat() + "Z",
        }
        try:
            self._table.put_item(Item=item)
        except ClientError as e:
            logger.error(
                "Failed to mark disconnect for user=%s provider=%s: %s",
                user_id,
                provider_id,
                e,
            )
            raise

    async def clear_disconnected(self, user_id: str, provider_id: str) -> None:
        """Remove the disconnect flag — called after a successful re-consent."""
        if not self._enabled:
            return
        try:
            self._table.delete_item(Key=self._key(user_id, provider_id))
        except ClientError as e:
            logger.error(
                "Failed to clear disconnect for user=%s provider=%s: %s",
                user_id,
                provider_id,
                e,
            )
            raise


_disconnect_repository: Optional[OAuthDisconnectRepository] = None


def get_disconnect_repository() -> OAuthDisconnectRepository:
    """Get the process-wide disconnect repository singleton."""
    global _disconnect_repository
    if _disconnect_repository is None:
        _disconnect_repository = OAuthDisconnectRepository()
    return _disconnect_repository
