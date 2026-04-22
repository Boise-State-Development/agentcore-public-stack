"""Admin API routes for OAuth provider management.

Registration flows through AWS Bedrock AgentCore Identity. Our DynamoDB
record holds display metadata, scopes, and role gates; the AgentCore
credential provider owns `clientId`, `clientSecret`, endpoint config, and
the callback URL that the admin must register with the vendor.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

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
    OAuthProviderUpdate,
)
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth-providers", tags=["admin-oauth"])


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

    try:
        credential_info = registrar.create_credential_provider(
            provider_id=provider_data.provider_id,
            provider_type=provider_data.provider_type,
            client_id=provider_data.client_id,
            client_secret=provider_data.client_secret,
            discovery_url=provider_data.oauth_discovery_url,
            authorization_server_metadata=provider_data.authorization_server_metadata,
        )
    except CredentialProviderConflictError as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))
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
        try:
            registrar.delete_credential_provider(provider_data.provider_id)
        except Exception:
            logger.exception(
                "Rollback delete failed for %s; orphaned AgentCore provider may exist",
                provider_data.provider_id,
            )
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
        credential_provider_arn=credential_info.credential_provider_arn,
        callback_url=credential_info.callback_url,
        oauth_discovery_url=data.oauth_discovery_url,
        authorization_server_metadata=data.authorization_server_metadata,
        created_at=now,
        updated_at=now,
    )
