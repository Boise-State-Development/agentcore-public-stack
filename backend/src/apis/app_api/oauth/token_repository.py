"""DynamoDB repository for OAuth user tokens."""

import logging
import os
from datetime import datetime
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import OAuthUserToken, OAuthConnectionStatus

logger = logging.getLogger(__name__)


class OAuthTokenRepository:
    """
    Repository for OAuth user token CRUD operations in DynamoDB.

    Handles encrypted token storage with KMS.
    Uses single-table design with GSI for querying by provider.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize repository.

        Args:
            table_name: DynamoDB table name (defaults to env var)
            region: AWS region (defaults to env var)
        """
        self._table_name = table_name or os.getenv("DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME not set. "
                "OAuth token repository is disabled."
            )
            return

        # Initialize client
        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)

        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized OAuth token repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        """Check if repository is enabled."""
        return self._enabled

    # =========================================================================
    # Token CRUD
    # =========================================================================

    async def get_token(
        self, user_id: str, provider_id: str
    ) -> Optional[OAuthUserToken]:
        """
        Get a user's token for a provider.

        Args:
            user_id: User identifier
            provider_id: Provider identifier

        Returns:
            OAuthUserToken if found, None otherwise
        """
        if not self._enabled:
            return None

        try:
            response = self._table.get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PROVIDER#{provider_id}",
                }
            )
            item = response.get("Item")
            if not item:
                return None
            return OAuthUserToken.from_dynamo_item(item)

        except ClientError as e:
            logger.error(f"Error getting token for user {user_id}, provider {provider_id}: {e}")
            raise

    async def list_user_tokens(self, user_id: str) -> List[OAuthUserToken]:
        """
        List all tokens for a user.

        Args:
            user_id: User identifier

        Returns:
            List of OAuthUserToken objects
        """
        if not self._enabled:
            return []

        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_id}",
                    ":sk_prefix": "PROVIDER#",
                },
            )
            items = response.get("Items", [])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                    ExpressionAttributeValues={
                        ":pk": f"USER#{user_id}",
                        ":sk_prefix": "PROVIDER#",
                    },
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            return [OAuthUserToken.from_dynamo_item(item) for item in items]

        except ClientError as e:
            logger.error(f"Error listing tokens for user {user_id}: {e}")
            raise

    async def list_provider_tokens(self, provider_id: str) -> List[OAuthUserToken]:
        """
        List all user tokens for a provider (admin view).

        Uses GSI for efficient lookup.

        Args:
            provider_id: Provider identifier

        Returns:
            List of OAuthUserToken objects
        """
        if not self._enabled:
            return []

        try:
            response = self._table.query(
                IndexName="ProviderUsersIndex",
                KeyConditionExpression="GSI1PK = :pk",
                ExpressionAttributeValues={":pk": f"PROVIDER#{provider_id}"},
            )
            items = response.get("Items", [])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    IndexName="ProviderUsersIndex",
                    KeyConditionExpression="GSI1PK = :pk",
                    ExpressionAttributeValues={":pk": f"PROVIDER#{provider_id}"},
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            return [OAuthUserToken.from_dynamo_item(item) for item in items]

        except ClientError as e:
            logger.error(f"Error listing tokens for provider {provider_id}: {e}")
            raise

    async def save_token(self, token: OAuthUserToken) -> OAuthUserToken:
        """
        Save or update a user token.

        Args:
            token: Token to save

        Returns:
            Saved token
        """
        if not self._enabled:
            raise RuntimeError("OAuth token repository is not enabled")

        try:
            token.updated_at = datetime.utcnow().isoformat() + "Z"
            self._table.put_item(Item=token.to_dynamo_item())
            logger.info(f"Saved token for user {token.user_id}, provider {token.provider_id}")
            return token

        except ClientError as e:
            logger.error(f"Error saving token: {e}")
            raise

    async def update_token_status(
        self,
        user_id: str,
        provider_id: str,
        status: OAuthConnectionStatus,
    ) -> Optional[OAuthUserToken]:
        """
        Update token status.

        Args:
            user_id: User identifier
            provider_id: Provider identifier
            status: New status

        Returns:
            Updated token, or None if not found
        """
        if not self._enabled:
            return None

        token = await self.get_token(user_id, provider_id)
        if not token:
            return None

        token.status = status
        return await self.save_token(token)

    async def delete_token(self, user_id: str, provider_id: str) -> bool:
        """
        Delete a user's token for a provider.

        Args:
            user_id: User identifier
            provider_id: Provider identifier

        Returns:
            True if deleted, False if not found
        """
        if not self._enabled:
            return False

        try:
            # Check if exists first
            existing = await self.get_token(user_id, provider_id)
            if not existing:
                return False

            self._table.delete_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PROVIDER#{provider_id}",
                }
            )

            logger.info(f"Deleted token for user {user_id}, provider {provider_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting token: {e}")
            raise

    async def delete_user_tokens(self, user_id: str) -> int:
        """
        Delete all tokens for a user.

        Args:
            user_id: User identifier

        Returns:
            Number of tokens deleted
        """
        if not self._enabled:
            return 0

        try:
            tokens = await self.list_user_tokens(user_id)

            with self._table.batch_writer() as batch:
                for token in tokens:
                    batch.delete_item(
                        Key={
                            "PK": f"USER#{user_id}",
                            "SK": f"PROVIDER#{token.provider_id}",
                        }
                    )

            logger.info(f"Deleted {len(tokens)} tokens for user {user_id}")
            return len(tokens)

        except ClientError as e:
            logger.error(f"Error deleting tokens for user {user_id}: {e}")
            raise

    async def delete_provider_tokens(self, provider_id: str) -> int:
        """
        Delete all tokens for a provider (when provider is deleted).

        Args:
            provider_id: Provider identifier

        Returns:
            Number of tokens deleted
        """
        if not self._enabled:
            return 0

        try:
            tokens = await self.list_provider_tokens(provider_id)

            with self._table.batch_writer() as batch:
                for token in tokens:
                    batch.delete_item(
                        Key={
                            "PK": f"USER#{token.user_id}",
                            "SK": f"PROVIDER#{provider_id}",
                        }
                    )

            logger.info(f"Deleted {len(tokens)} tokens for provider {provider_id}")
            return len(tokens)

        except ClientError as e:
            logger.error(f"Error deleting tokens for provider {provider_id}: {e}")
            raise


# Singleton instance
_token_repository: Optional[OAuthTokenRepository] = None


def get_token_repository() -> OAuthTokenRepository:
    """Get the token repository singleton."""
    global _token_repository
    if _token_repository is None:
        _token_repository = OAuthTokenRepository()
    return _token_repository
