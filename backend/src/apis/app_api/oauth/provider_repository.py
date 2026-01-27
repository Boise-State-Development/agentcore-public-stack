"""DynamoDB repository for OAuth provider configurations."""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import OAuthProvider, OAuthProviderCreate, OAuthProviderUpdate

logger = logging.getLogger(__name__)


class OAuthProviderRepository:
    """
    Repository for OAuth provider CRUD operations in DynamoDB.

    Handles provider configurations and client secrets in Secrets Manager.
    Uses single-table design with GSI for querying enabled providers.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        secrets_arn: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize repository.

        Args:
            table_name: DynamoDB table name (defaults to env var)
            secrets_arn: Secrets Manager ARN for client secrets (defaults to env var)
            region: AWS region (defaults to env var)
        """
        self._table_name = table_name or os.getenv("DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME")
        self._secrets_arn = secrets_arn or os.getenv("OAUTH_CLIENT_SECRETS_ARN")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME not set. "
                "OAuth provider repository is disabled."
            )
            return

        # Initialize clients
        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
            self._secrets_client = session.client("secretsmanager", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
            self._secrets_client = boto3.client("secretsmanager", region_name=self._region)

        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized OAuth provider repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        """Check if repository is enabled."""
        return self._enabled

    # =========================================================================
    # Provider CRUD
    # =========================================================================

    async def get_provider(self, provider_id: str) -> Optional[OAuthProvider]:
        """
        Get a provider by ID.

        Args:
            provider_id: Provider identifier

        Returns:
            OAuthProvider if found, None otherwise
        """
        if not self._enabled:
            return None

        try:
            response = self._table.get_item(
                Key={"PK": f"PROVIDER#{provider_id}", "SK": "CONFIG"}
            )
            item = response.get("Item")
            if not item:
                return None
            return OAuthProvider.from_dynamo_item(item)

        except ClientError as e:
            logger.error(f"Error getting provider {provider_id}: {e}")
            raise

    async def list_providers(self, enabled_only: bool = False) -> List[OAuthProvider]:
        """
        List all providers.

        Args:
            enabled_only: If True, only return enabled providers

        Returns:
            List of OAuthProvider objects
        """
        if not self._enabled:
            return []

        try:
            if enabled_only:
                # Use GSI for efficient query
                response = self._table.query(
                    IndexName="EnabledProvidersIndex",
                    KeyConditionExpression="GSI1PK = :pk",
                    ExpressionAttributeValues={":pk": "ENABLED#true"},
                )
                items = response.get("Items", [])

                # Handle pagination
                while "LastEvaluatedKey" in response:
                    response = self._table.query(
                        IndexName="EnabledProvidersIndex",
                        KeyConditionExpression="GSI1PK = :pk",
                        ExpressionAttributeValues={":pk": "ENABLED#true"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))
            else:
                # Scan all providers
                response = self._table.scan(
                    FilterExpression="SK = :sk",
                    ExpressionAttributeValues={":sk": "CONFIG"},
                )
                items = response.get("Items", [])

                # Handle pagination
                while "LastEvaluatedKey" in response:
                    response = self._table.scan(
                        FilterExpression="SK = :sk",
                        ExpressionAttributeValues={":sk": "CONFIG"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))

            providers = [OAuthProvider.from_dynamo_item(item) for item in items]

            # Sort by display name
            providers.sort(key=lambda p: p.display_name.lower())

            return providers

        except ClientError as e:
            logger.error(f"Error listing providers: {e}")
            raise

    async def create_provider(
        self, create_request: OAuthProviderCreate
    ) -> OAuthProvider:
        """
        Create a new provider.

        Args:
            create_request: Provider creation data including client secret

        Returns:
            Created OAuthProvider

        Raises:
            ValueError: If provider already exists
        """
        if not self._enabled:
            raise RuntimeError("OAuth provider repository is not enabled")

        # Check if provider exists
        existing = await self.get_provider(create_request.provider_id)
        if existing:
            raise ValueError(f"Provider '{create_request.provider_id}' already exists")

        try:
            now = datetime.utcnow().isoformat() + "Z"

            # Create provider object
            provider = OAuthProvider(
                provider_id=create_request.provider_id,
                display_name=create_request.display_name,
                provider_type=create_request.provider_type,
                authorization_endpoint=create_request.authorization_endpoint,
                token_endpoint=create_request.token_endpoint,
                client_id=create_request.client_id,
                scopes=create_request.scopes,
                allowed_roles=create_request.allowed_roles,
                enabled=create_request.enabled,
                icon_name=create_request.icon_name,
                userinfo_endpoint=create_request.userinfo_endpoint,
                revocation_endpoint=create_request.revocation_endpoint,
                pkce_required=create_request.pkce_required,
                created_at=now,
                updated_at=now,
            )

            # Store client secret in Secrets Manager
            await self._store_client_secret(
                create_request.provider_id, create_request.client_secret
            )

            # Store provider in DynamoDB
            self._table.put_item(
                Item=provider.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )

            logger.info(f"Created OAuth provider: {provider.provider_id}")
            return provider

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(
                    f"Provider '{create_request.provider_id}' already exists"
                )
            logger.error(f"Error creating provider: {e}")
            raise

    async def update_provider(
        self, provider_id: str, updates: OAuthProviderUpdate
    ) -> Optional[OAuthProvider]:
        """
        Update an existing provider.

        Args:
            provider_id: Provider identifier
            updates: Fields to update

        Returns:
            Updated OAuthProvider, or None if not found
        """
        if not self._enabled:
            return None

        existing = await self.get_provider(provider_id)
        if not existing:
            return None

        try:
            # Apply updates
            if updates.display_name is not None:
                existing.display_name = updates.display_name
            if updates.authorization_endpoint is not None:
                existing.authorization_endpoint = updates.authorization_endpoint
            if updates.token_endpoint is not None:
                existing.token_endpoint = updates.token_endpoint
            if updates.client_id is not None:
                existing.client_id = updates.client_id
            if updates.scopes is not None:
                existing.scopes = updates.scopes
            if updates.allowed_roles is not None:
                existing.allowed_roles = updates.allowed_roles
            if updates.enabled is not None:
                existing.enabled = updates.enabled
            if updates.icon_name is not None:
                existing.icon_name = updates.icon_name
            if updates.userinfo_endpoint is not None:
                existing.userinfo_endpoint = updates.userinfo_endpoint
            if updates.revocation_endpoint is not None:
                existing.revocation_endpoint = updates.revocation_endpoint
            if updates.pkce_required is not None:
                existing.pkce_required = updates.pkce_required

            existing.updated_at = datetime.utcnow().isoformat() + "Z"

            # Update client secret if provided
            if updates.client_secret is not None:
                await self._store_client_secret(provider_id, updates.client_secret)

            # Store updated provider
            self._table.put_item(Item=existing.to_dynamo_item())

            logger.info(f"Updated OAuth provider: {provider_id}")
            return existing

        except ClientError as e:
            logger.error(f"Error updating provider {provider_id}: {e}")
            raise

    async def delete_provider(self, provider_id: str) -> bool:
        """
        Delete a provider.

        Args:
            provider_id: Provider identifier

        Returns:
            True if deleted, False if not found
        """
        if not self._enabled:
            return False

        existing = await self.get_provider(provider_id)
        if not existing:
            return False

        try:
            # Delete from DynamoDB
            self._table.delete_item(
                Key={"PK": f"PROVIDER#{provider_id}", "SK": "CONFIG"}
            )

            # Remove client secret from Secrets Manager
            await self._delete_client_secret(provider_id)

            logger.info(f"Deleted OAuth provider: {provider_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting provider {provider_id}: {e}")
            raise

    # =========================================================================
    # Client Secret Management (Secrets Manager)
    # =========================================================================

    async def get_client_secret(self, provider_id: str) -> Optional[str]:
        """
        Get client secret for a provider from Secrets Manager.

        Args:
            provider_id: Provider identifier

        Returns:
            Client secret string, or None if not found
        """
        if not self._secrets_arn:
            logger.warning("Secrets ARN not configured, cannot retrieve client secret")
            return None

        try:
            response = self._secrets_client.get_secret_value(
                SecretId=self._secrets_arn
            )
            secrets = json.loads(response["SecretString"])
            return secrets.get(provider_id)

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.warning("OAuth secrets not found in Secrets Manager")
                return None
            logger.error(f"Error getting client secret for {provider_id}: {e}")
            raise

    async def _store_client_secret(
        self, provider_id: str, client_secret: str
    ) -> None:
        """
        Store client secret in Secrets Manager.

        Args:
            provider_id: Provider identifier
            client_secret: Client secret to store
        """
        if not self._secrets_arn:
            logger.warning(
                "Secrets ARN not configured, cannot store client secret. "
                "This is only acceptable in development."
            )
            return

        try:
            # Get existing secrets
            try:
                response = self._secrets_client.get_secret_value(
                    SecretId=self._secrets_arn
                )
                secrets = json.loads(response["SecretString"])
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    secrets = {}
                else:
                    raise

            # Update with new secret
            secrets[provider_id] = client_secret

            # Store back
            self._secrets_client.put_secret_value(
                SecretId=self._secrets_arn,
                SecretString=json.dumps(secrets),
            )

            logger.info(f"Stored client secret for provider: {provider_id}")

        except ClientError as e:
            logger.error(f"Error storing client secret for {provider_id}: {e}")
            raise

    async def _delete_client_secret(self, provider_id: str) -> None:
        """
        Remove client secret from Secrets Manager.

        Args:
            provider_id: Provider identifier
        """
        if not self._secrets_arn:
            return

        try:
            # Get existing secrets
            response = self._secrets_client.get_secret_value(
                SecretId=self._secrets_arn
            )
            secrets = json.loads(response["SecretString"])

            # Remove provider's secret
            if provider_id in secrets:
                del secrets[provider_id]

                # Store back
                self._secrets_client.put_secret_value(
                    SecretId=self._secrets_arn,
                    SecretString=json.dumps(secrets),
                )

                logger.info(f"Removed client secret for provider: {provider_id}")

        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.error(f"Error deleting client secret for {provider_id}: {e}")
                raise


# Singleton instance
_provider_repository: Optional[OAuthProviderRepository] = None


def get_provider_repository() -> OAuthProviderRepository:
    """Get the provider repository singleton."""
    global _provider_repository
    if _provider_repository is None:
        _provider_repository = OAuthProviderRepository()
    return _provider_repository
