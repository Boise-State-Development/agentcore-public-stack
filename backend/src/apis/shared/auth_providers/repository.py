"""DynamoDB repository for OIDC authentication provider configurations."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import AuthProvider, AuthProviderCreate, AuthProviderUpdate

logger = logging.getLogger(__name__)


class AuthProviderRepository:
    """
    Repository for OIDC authentication provider CRUD operations in DynamoDB.

    Uses single-table design with GSI for querying enabled providers.
    Client secrets are stored separately in AWS Secrets Manager.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        secrets_arn: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self._table_name = table_name or os.getenv("DYNAMODB_AUTH_PROVIDERS_TABLE_NAME")
        self._secrets_arn = secrets_arn or os.getenv("AUTH_PROVIDER_SECRETS_ARN")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_AUTH_PROVIDERS_TABLE_NAME not set. "
                "Auth provider repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
            self._secrets_client = session.client("secretsmanager", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
            self._secrets_client = boto3.client("secretsmanager", region_name=self._region)

        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized auth provider repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # =========================================================================
    # Provider CRUD
    # =========================================================================

    async def get_provider(self, provider_id: str) -> Optional[AuthProvider]:
        """Get a provider by ID."""
        if not self._enabled:
            return None

        try:
            response = self._table.get_item(
                Key={
                    "PK": f"AUTH_PROVIDER#{provider_id}",
                    "SK": f"AUTH_PROVIDER#{provider_id}",
                }
            )
            item = response.get("Item")
            if not item:
                return None
            return AuthProvider.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting auth provider {provider_id}: {e}")
            raise

    async def list_providers(self, enabled_only: bool = False) -> List[AuthProvider]:
        """List all auth providers, optionally filtered to enabled only."""
        if not self._enabled:
            return []

        try:
            if enabled_only:
                response = self._table.query(
                    IndexName="EnabledProvidersIndex",
                    KeyConditionExpression="GSI1PK = :pk",
                    ExpressionAttributeValues={":pk": "ENABLED#true"},
                )
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.query(
                        IndexName="EnabledProvidersIndex",
                        KeyConditionExpression="GSI1PK = :pk",
                        ExpressionAttributeValues={":pk": "ENABLED#true"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))
            else:
                response = self._table.scan(
                    FilterExpression="begins_with(SK, :prefix)",
                    ExpressionAttributeValues={":prefix": "AUTH_PROVIDER#"},
                )
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.scan(
                        FilterExpression="begins_with(SK, :prefix)",
                        ExpressionAttributeValues={":prefix": "AUTH_PROVIDER#"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))

            providers = [AuthProvider.from_dynamo_item(item) for item in items]
            providers.sort(key=lambda p: p.display_name.lower())
            return providers

        except ClientError as e:
            logger.error(f"Error listing auth providers: {e}")
            raise

    async def create_provider(self, data: AuthProviderCreate, created_by: Optional[str] = None, cognito_provider_name: Optional[str] = None) -> AuthProvider:
        """Create a new auth provider. Stores client secret in Secrets Manager."""
        if not self._enabled:
            raise RuntimeError("Auth provider repository is not enabled")

        existing = await self.get_provider(data.provider_id)
        if existing:
            raise ValueError(f"Auth provider '{data.provider_id}' already exists")

        try:
            now = datetime.now(timezone.utc).isoformat() + "Z"

            provider = AuthProvider(
                provider_id=data.provider_id,
                display_name=data.display_name,
                provider_type=data.provider_type,
                enabled=data.enabled,
                issuer_url=data.issuer_url,
                client_id=data.client_id,
                authorization_endpoint=data.authorization_endpoint,
                token_endpoint=data.token_endpoint,
                jwks_uri=data.jwks_uri,
                userinfo_endpoint=data.userinfo_endpoint,
                end_session_endpoint=data.end_session_endpoint,
                scopes=data.scopes,
                response_type=data.response_type,
                pkce_enabled=data.pkce_enabled,
                redirect_uri=data.redirect_uri,
                user_id_claim=data.user_id_claim,
                email_claim=data.email_claim,
                name_claim=data.name_claim,
                roles_claim=data.roles_claim,
                picture_claim=data.picture_claim,
                first_name_claim=data.first_name_claim,
                last_name_claim=data.last_name_claim,
                user_id_pattern=data.user_id_pattern,
                required_scopes=data.required_scopes,
                allowed_audiences=data.allowed_audiences,
                logo_url=data.logo_url,
                button_color=data.button_color,
                created_at=now,
                updated_at=now,
                created_by=created_by,
                cognito_provider_name=cognito_provider_name,
            )

            # Store client secret in Secrets Manager
            await self._store_client_secret(data.provider_id, data.client_secret)

            # Store provider config in DynamoDB
            self._table.put_item(
                Item=provider.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )

            logger.info(f"Created auth provider: {provider.provider_id}")
            return provider

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Auth provider '{data.provider_id}' already exists")
            logger.error(f"Error creating auth provider: {e}")
            raise

    async def update_provider(
        self, provider_id: str, updates: AuthProviderUpdate
    ) -> Optional[AuthProvider]:
        """Update an existing auth provider. Only non-None fields are applied."""
        if not self._enabled:
            return None

        existing = await self.get_provider(provider_id)
        if not existing:
            return None

        try:
            update_fields = updates.model_dump(exclude_none=True)

            # Handle client_secret separately (Secrets Manager)
            client_secret = update_fields.pop("client_secret", None)
            if client_secret:
                await self._store_client_secret(provider_id, client_secret)

            # Apply remaining updates to the provider object
            for field_name, value in update_fields.items():
                if hasattr(existing, field_name):
                    setattr(existing, field_name, value)

            existing.updated_at = datetime.now(timezone.utc).isoformat() + "Z"

            # Store updated provider
            self._table.put_item(Item=existing.to_dynamo_item())

            logger.info(f"Updated auth provider: {provider_id}")
            return existing

        except ClientError as e:
            logger.error(f"Error updating auth provider {provider_id}: {e}")
            raise

    async def delete_provider(self, provider_id: str) -> bool:
        """Delete an auth provider and its client secret."""
        if not self._enabled:
            return False

        existing = await self.get_provider(provider_id)
        if not existing:
            return False

        try:
            self._table.delete_item(
                Key={
                    "PK": f"AUTH_PROVIDER#{provider_id}",
                    "SK": f"AUTH_PROVIDER#{provider_id}",
                }
            )

            await self._delete_client_secret(provider_id)

            logger.info(f"Deleted auth provider: {provider_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting auth provider {provider_id}: {e}")
            raise

    # =========================================================================
    # Client Secret Management (Secrets Manager)
    # =========================================================================

    async def get_client_secret(self, provider_id: str) -> Optional[str]:
        """Get client secret for a provider from Secrets Manager."""
        if not self._secrets_arn:
            logger.warning("Auth provider secrets ARN not configured")
            return None

        try:
            response = self._secrets_client.get_secret_value(
                SecretId=self._secrets_arn
            )
            try:
                secrets = json.loads(response["SecretString"])
            except (json.JSONDecodeError, KeyError):
                secrets = {}
            return secrets.get(provider_id)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.warning("Auth provider secrets not found in Secrets Manager")
                return None
            logger.error(f"Error getting client secret for {provider_id}: {e}")
            raise

    async def _store_client_secret(self, provider_id: str, client_secret: str) -> None:
        """Store client secret in Secrets Manager."""
        if not self._secrets_arn:
            logger.warning(
                "Auth provider secrets ARN not configured, cannot store client secret. "
                "This is only acceptable in development."
            )
            return

        try:
            try:
                response = self._secrets_client.get_secret_value(
                    SecretId=self._secrets_arn
                )
                try:
                    secrets = json.loads(response["SecretString"])
                except (json.JSONDecodeError, KeyError):
                    secrets = {}
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    secrets = {}
                else:
                    raise

            secrets[provider_id] = client_secret
            self._secrets_client.put_secret_value(
                SecretId=self._secrets_arn,
                SecretString=json.dumps(secrets),
            )
            logger.info(f"Stored client secret for auth provider: {provider_id}")

        except ClientError as e:
            logger.error(f"Error storing client secret for {provider_id}: {e}")
            raise

    async def _delete_client_secret(self, provider_id: str) -> None:
        """Remove client secret from Secrets Manager."""
        if not self._secrets_arn:
            return

        try:
            response = self._secrets_client.get_secret_value(
                SecretId=self._secrets_arn
            )
            try:
                secrets = json.loads(response["SecretString"])
            except (json.JSONDecodeError, KeyError):
                secrets = {}

            if provider_id in secrets:
                del secrets[provider_id]
                self._secrets_client.put_secret_value(
                    SecretId=self._secrets_arn,
                    SecretString=json.dumps(secrets),
                )
                logger.info(f"Removed client secret for auth provider: {provider_id}")

        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.error(f"Error deleting client secret for {provider_id}: {e}")
                raise


# Singleton instance
_repository: Optional[AuthProviderRepository] = None


def get_auth_provider_repository() -> AuthProviderRepository:
    """Get the auth provider repository singleton."""
    global _repository
    if _repository is None:
        _repository = AuthProviderRepository()
    return _repository
