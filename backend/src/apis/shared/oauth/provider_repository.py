"""DynamoDB repository for OAuth provider configurations.

Only display metadata, scopes, and AgentCore-owned pointers (the credential
provider ARN and callback URL) live here. `clientId` / `clientSecret` are
registered directly with AgentCore Identity by the admin route.
"""

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import OAuthProvider, OAuthProviderUpdate

logger = logging.getLogger(__name__)


class OAuthProviderRepository:
    """CRUD over the oauth-providers DynamoDB table."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self._table_name = table_name or os.getenv("DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME not set. "
                "OAuth provider repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        session = boto3.Session(profile_name=profile) if profile else boto3
        self._dynamodb = session.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self._table_name)
        logger.info("Initialized OAuth provider repository: table=%s", self._table_name)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------- reads
    async def get_provider(self, provider_id: str) -> Optional[OAuthProvider]:
        if not self._enabled:
            return None

        try:
            response = self._table.get_item(
                Key={"PK": f"PROVIDER#{provider_id}", "SK": "CONFIG"}
            )
            item = response.get("Item")
            return OAuthProvider.from_dynamo_item(item) if item else None
        except ClientError as e:
            logger.error("Error getting provider %s: %s", provider_id, e)
            raise

    async def list_providers(self, enabled_only: bool = False) -> List[OAuthProvider]:
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
                    FilterExpression="SK = :sk",
                    ExpressionAttributeValues={":sk": "CONFIG"},
                )
                items = response.get("Items", [])
                while "LastEvaluatedKey" in response:
                    response = self._table.scan(
                        FilterExpression="SK = :sk",
                        ExpressionAttributeValues={":sk": "CONFIG"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))

            providers = [OAuthProvider.from_dynamo_item(item) for item in items]
            providers.sort(key=lambda p: p.display_name.lower())
            return providers
        except ClientError as e:
            logger.error("Error listing providers: %s", e)
            raise

    # ------------------------------------------------------------------ writes
    async def put_provider(self, provider: OAuthProvider) -> OAuthProvider:
        """Upsert a fully-formed provider record.

        The admin route is expected to build the `OAuthProvider` with all
        AgentCore-owned fields already populated from the registrar call.
        """
        if not self._enabled:
            raise RuntimeError("OAuth provider repository is not enabled")

        self._table.put_item(Item=provider.to_dynamo_item())
        logger.info("Upserted OAuth provider: %s", provider.provider_id)
        return provider

    async def apply_metadata_update(
        self, provider_id: str, updates: OAuthProviderUpdate
    ) -> Optional[OAuthProvider]:
        """Apply a metadata-only update to an existing provider record.

        Does not touch AgentCore — the admin route is responsible for
        calling the registrar first when credentials or the discovery
        config change, then passing the refreshed metadata through here.
        Fields left `None` on `updates` are preserved.
        """
        existing = await self.get_provider(provider_id)
        if not existing:
            return None

        if updates.display_name is not None:
            existing.display_name = updates.display_name
        if updates.scopes is not None:
            existing.scopes = updates.scopes
        if updates.allowed_roles is not None:
            existing.allowed_roles = updates.allowed_roles
        if updates.enabled is not None:
            existing.enabled = updates.enabled
        if updates.icon_name is not None:
            existing.icon_name = updates.icon_name
        if updates.icon_data is not None:
            # Empty string explicitly clears any uploaded icon (frontends
            # then fall back to `icon_name`); a populated data URL replaces
            # it. `None` on the update model leaves the existing value alone.
            existing.icon_data = updates.icon_data or None
        if updates.oauth_discovery_url is not None:
            existing.oauth_discovery_url = updates.oauth_discovery_url
        if updates.authorization_server_metadata is not None:
            existing.authorization_server_metadata = updates.authorization_server_metadata
        if updates.custom_parameters is not None:
            # Empty dict (`{}`) explicitly clears the field; pass None on the
            # update model to leave the existing value alone.
            existing.custom_parameters = updates.custom_parameters or None
        if updates.file_source_adapter_id is not None:
            # Empty string (`""`) clears the file-source mapping; a populated
            # adapter key sets it. `None` leaves the existing value alone.
            existing.file_source_adapter_id = updates.file_source_adapter_id or None

        existing.updated_at = datetime.now(timezone.utc).isoformat() + "Z"
        self._table.put_item(Item=existing.to_dynamo_item())
        logger.info("Updated OAuth provider metadata: %s", provider_id)
        return existing

    async def delete_provider(self, provider_id: str) -> bool:
        if not self._enabled:
            return False

        existing = await self.get_provider(provider_id)
        if not existing:
            return False

        try:
            self._table.delete_item(
                Key={"PK": f"PROVIDER#{provider_id}", "SK": "CONFIG"}
            )
            logger.info("Deleted OAuth provider: %s", provider_id)
            return True
        except ClientError as e:
            logger.error("Error deleting provider %s: %s", provider_id, e)
            raise


_provider_repository: Optional[OAuthProviderRepository] = None


def get_provider_repository() -> OAuthProviderRepository:
    """Get the process-wide provider repository singleton."""
    global _provider_repository
    if _provider_repository is None:
        _provider_repository = OAuthProviderRepository()
    return _provider_repository
