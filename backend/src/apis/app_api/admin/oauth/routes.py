"""Admin API routes for OAuth provider management.

Registration flows through AWS Bedrock AgentCore Identity. Our DynamoDB
record holds display metadata, scopes, and role gates; the AgentCore
credential provider owns `clientId`, `clientSecret`, endpoint config, and
the callback URL that the admin must register with the vendor.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import boto3
from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.app_api.file_sources.registry import registry
from apis.shared.auth import User, require_admin
from apis.shared.oauth.agentcore_registrar import (
    AgentCoreRegistrar,
    CredentialProviderConflictError,
    CredentialProviderInfo,
    CredentialProviderNotFoundError,
    InvalidCustomProviderConfigError,
    get_agentcore_registrar,
)
from apis.shared.oauth.models import (
    OAuthProvider,
    OAuthProviderCreate,
    OAuthProviderListResponse,
    OAuthProviderResponse,
    OAuthProviderType,
    OAuthProviderUpdate,
)
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth-providers", tags=["admin-oauth"])

# Rollback backoff schedule. Two retries after the initial attempt, ~2.5s
# total worst case — short enough to keep the create request responsive,
# long enough to absorb the common class of transient AWS errors.
_ROLLBACK_RETRY_DELAYS_SECONDS = (0.5, 2.0)

# CloudWatch namespace for orphan telemetry. Kept distinct so ops can
# scope alarms without catching unrelated Bedrock metrics.
_ORPHAN_METRIC_NAMESPACE = "Agentcore/OAuth"
_ORPHAN_METRIC_NAME = "ProviderOrphaned"


@lru_cache(maxsize=1)
def _cloudwatch_client():
    region = os.environ.get("AWS_REGION", "us-west-2")
    return boto3.client("cloudwatch", region_name=region)


async def _rollback_orphaned_provider(
    registrar: AgentCoreRegistrar, provider_id: str
) -> None:
    """Best-effort delete of an AgentCore provider after a DB write failed.

    Retries on transient AWS errors. If every attempt fails we emit a
    CloudWatch `ProviderOrphaned` metric and log at ERROR — the AgentCore
    record is now orphaned (no DynamoDB row) and needs manual cleanup,
    but the admin's original 5xx still propagates so they know the
    create didn't land.
    """
    last_err: Exception | None = None
    for attempt in range(1 + len(_ROLLBACK_RETRY_DELAYS_SECONDS)):
        try:
            # Registrar is sync; off-thread it so we don't block the event loop.
            await asyncio.to_thread(registrar.delete_credential_provider, provider_id)
            logger.info(
                "Rolled back orphaned AgentCore provider %s (attempt %d)",
                provider_id,
                attempt + 1,
            )
            return
        except Exception as err:
            last_err = err
            if attempt < len(_ROLLBACK_RETRY_DELAYS_SECONDS):
                delay = _ROLLBACK_RETRY_DELAYS_SECONDS[attempt]
                logger.warning(
                    "Rollback attempt %d for %s failed (%s); retrying in %.1fs",
                    attempt + 1,
                    provider_id,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)

    logger.error(
        "Rollback delete exhausted for %s after %d attempts; emitting "
        "orphan metric. Last error: %s",
        provider_id,
        1 + len(_ROLLBACK_RETRY_DELAYS_SECONDS),
        last_err,
        exc_info=last_err,
    )
    _emit_orphan_metric(provider_id)


def _emit_orphan_metric(provider_id: str) -> None:
    """Fire-and-forget CloudWatch metric for an orphaned credential provider.

    Failures here are swallowed — we're already inside a rollback path
    and a secondary error would just shadow the admin-facing one.
    """
    try:
        _cloudwatch_client().put_metric_data(
            Namespace=_ORPHAN_METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": _ORPHAN_METRIC_NAME,
                    "Dimensions": [{"Name": "ProviderId", "Value": provider_id}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:
        logger.exception(
            "Failed to emit CloudWatch orphan metric for %s", provider_id
        )


# =============================================================================
# Provider CRUD
# =============================================================================


@router.get("/", response_model=OAuthProviderListResponse)
async def list_providers(
    enabled_only: bool = Query(False, description="Only return enabled providers"),
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
):
    """List all OAuth providers. Admin only."""
    logger.info("Admin listing OAuth providers")
    providers = await provider_repo.list_providers(enabled_only=enabled_only)
    return OAuthProviderListResponse(
        providers=[OAuthProviderResponse.from_provider(p) for p in providers],
        total=len(providers),
    )


@router.get("/{provider_id}", response_model=OAuthProviderResponse)
async def get_provider(
    provider_id: str,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
):
    """Get a provider by ID. Admin only."""
    provider = await provider_repo.get_provider(provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )
    return OAuthProviderResponse.from_provider(provider)


@router.post(
    "/", response_model=OAuthProviderResponse, status_code=status.HTTP_201_CREATED
)
async def create_provider(
    provider_data: OAuthProviderCreate,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    registrar: AgentCoreRegistrar = Depends(get_agentcore_registrar),
):
    """Register a new OAuth provider.

    Registers credentials with AgentCore Identity first; on success, writes
    the metadata record to DynamoDB. If the DB write fails after AgentCore
    has accepted the credentials, best-effort rolls back the AgentCore
    provider so the two stores stay in sync.
    """
    logger.info("Admin creating OAuth provider %s", provider_data.provider_id)

    existing = await provider_repo.get_provider(provider_data.provider_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Provider '{provider_data.provider_id}' already exists",
        )

    # Fail fast on a bad file-source mapping, before any AgentCore side-effect.
    _validate_file_source_adapter(
        provider_data.file_source_adapter_id, provider_data.provider_type
    )

    try:
        credential_info = registrar.create_credential_provider(
            provider_id=provider_data.provider_id,
            provider_type=provider_data.provider_type,
            client_id=provider_data.client_id,
            client_secret=provider_data.client_secret,
            discovery_url=provider_data.oauth_discovery_url,
            authorization_server_metadata=provider_data.authorization_server_metadata,
        )
    except CredentialProviderConflictError:
        # We already verified the DB has no record for this provider_id
        # above, so a conflict from AgentCore means its vault carries an
        # orphaned record — almost always from a prior failed rollback.
        # Give the admin a cleanup pointer instead of a bare 409.
        logger.error(
            "Orphan detected for %s: DB empty but AgentCore has a credential "
            "provider. Previous rollback likely failed.",
            provider_data.provider_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An AgentCore credential provider named "
                f"'{provider_data.provider_id}' already exists but has no "
                "matching database record (likely from a previous failed "
                "rollback). Delete it via the AWS CLI and retry: "
                "`aws bedrock-agentcore-control delete-oauth2-credential-provider "
                f"--name {provider_data.provider_id}`."
            ),
        )
    except InvalidCustomProviderConfigError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    try:
        provider = _build_provider_from_create(provider_data, credential_info)
        await provider_repo.put_provider(provider)
    except Exception:
        logger.exception(
            "DB write failed for %s; rolling back AgentCore credential provider",
            provider_data.provider_id,
        )
        await _rollback_orphaned_provider(registrar, provider_data.provider_id)
        raise

    return OAuthProviderResponse.from_provider(provider)


@router.patch("/{provider_id}", response_model=OAuthProviderResponse)
async def update_provider(
    provider_id: str,
    updates: OAuthProviderUpdate,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    registrar: AgentCoreRegistrar = Depends(get_agentcore_registrar),
):
    """Update a provider's metadata, and optionally rotate credentials.

    Metadata edits (display name, scopes, roles, icon, enabled) write
    straight to DynamoDB. Credential or discovery-config changes require
    a corresponding AgentCore update — this is done first, and only if it
    succeeds do we persist the new metadata and cached pointers.
    """
    existing = await provider_repo.get_provider(provider_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    # A populated adapter key must be valid for this connector's type; an
    # empty string (clear the mapping) and None (unchanged) skip validation.
    _validate_file_source_adapter(
        updates.file_source_adapter_id, existing.provider_type
    )

    rotating_credentials = bool(updates.client_id and updates.client_secret)
    changing_discovery = (
        updates.oauth_discovery_url is not None
        or updates.authorization_server_metadata is not None
    )

    credential_info: CredentialProviderInfo | None = None
    if rotating_credentials or changing_discovery:
        discovery_url = (
            updates.oauth_discovery_url
            if updates.oauth_discovery_url is not None
            else existing.oauth_discovery_url
        )
        authorization_server_metadata = (
            updates.authorization_server_metadata
            if updates.authorization_server_metadata is not None
            else existing.authorization_server_metadata
        )
        if not rotating_credentials:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Discovery config can only be updated together with a "
                    "credential rotation (client_id + client_secret)."
                ),
            )
        try:
            credential_info = registrar.update_credential_provider(
                provider_id=provider_id,
                provider_type=existing.provider_type,
                client_id=updates.client_id,
                client_secret=updates.client_secret,
                discovery_url=discovery_url,
                authorization_server_metadata=authorization_server_metadata,
                fallback_arn=existing.credential_provider_arn,
            )
        except CredentialProviderNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"AgentCore credential provider for '{provider_id}' "
                    "was not found. The DynamoDB record may be stale."
                ),
            )
        except InvalidCustomProviderConfigError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)
            )

    provider = await provider_repo.apply_metadata_update(provider_id, updates)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    if credential_info is not None:
        provider.credential_provider_arn = credential_info.credential_provider_arn
        provider.callback_url = credential_info.callback_url
        provider.updated_at = datetime.now(timezone.utc).isoformat() + "Z"
        await provider_repo.put_provider(provider)

    return OAuthProviderResponse.from_provider(provider)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    registrar: AgentCoreRegistrar = Depends(get_agentcore_registrar),
):
    """Delete a provider from AgentCore and DynamoDB.

    AgentCore's deletion also removes every user token stored in its vault
    for this provider, so connected users must reconnect the next time
    they invoke a tool that needs it.
    """
    existing = await provider_repo.get_provider(provider_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    registrar.delete_credential_provider(provider_id)
    await provider_repo.delete_provider(provider_id)
    return None


# =============================================================================
# Helpers
# =============================================================================


def _validate_file_source_adapter(
    adapter_id: Optional[str], provider_type: OAuthProviderType
) -> None:
    """Reject a file-source adapter mapping the registry cannot honor.

    An empty/None value is a no-op — the connector simply isn't a file
    source. A populated value must name an adapter shipped in the registry
    whose `compatible_provider_types` includes this connector's type.
    Raises `HTTPException` 400 on a bad mapping.
    """
    if not adapter_id:
        return
    adapter = registry.get(adapter_id)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown file-source adapter '{adapter_id}'",
        )
    if provider_type not in adapter.metadata.compatible_provider_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File-source adapter '{adapter_id}' is not compatible with "
                f"provider type '{provider_type.value}'"
            ),
        )


def _build_provider_from_create(
    data: OAuthProviderCreate, credential_info: CredentialProviderInfo
) -> OAuthProvider:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return OAuthProvider(
        provider_id=data.provider_id,
        display_name=data.display_name,
        provider_type=data.provider_type,
        scopes=data.scopes,
        allowed_roles=data.allowed_roles,
        enabled=data.enabled,
        icon_name=data.icon_name,
        # `""` from the form means "no uploaded icon" — store as None so
        # absent and explicitly-cleared round-trip identically.
        icon_data=data.icon_data or None,
        credential_provider_arn=credential_info.credential_provider_arn,
        callback_url=credential_info.callback_url,
        oauth_discovery_url=data.oauth_discovery_url,
        authorization_server_metadata=data.authorization_server_metadata,
        # `{}` from the form means "explicitly no extras" — store as None
        # so absent/empty are indistinguishable in DynamoDB and `from_*`
        # lookups round-trip identically.
        custom_parameters=data.custom_parameters or None,
        # `""` from the form means "not a file source" — store as None.
        file_source_adapter_id=data.file_source_adapter_id or None,
        created_at=now,
        updated_at=now,
    )
