"""Admin API routes for OAuth provider management."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User, require_admin
from apis.app_api.oauth.models import (
    OAuthProviderCreate,
    OAuthProviderListResponse,
    OAuthProviderResponse,
    OAuthProviderUpdate,
)
from apis.app_api.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.app_api.oauth.token_repository import (
    OAuthTokenRepository,
    get_token_repository,
)
from apis.app_api.oauth.token_cache import get_token_cache

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
    """
    List all OAuth providers.

    Requires admin access.

    Args:
        enabled_only: If True, only return enabled providers
        admin: Authenticated admin user (injected)

    Returns:
        OAuthProviderListResponse with all providers
    """
    logger.info(f"Admin {admin.email} listing OAuth providers")

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
    """
    Get a provider by ID.

    Requires admin access.

    Args:
        provider_id: Provider identifier
        admin: Authenticated admin user (injected)

    Returns:
        OAuthProviderResponse with provider details

    Raises:
        HTTPException: 404 if provider not found
    """
    logger.info(f"Admin {admin.email} getting OAuth provider: {provider_id}")

    provider = await provider_repo.get_provider(provider_id)

    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    return OAuthProviderResponse.from_provider(provider)


@router.post("/", response_model=OAuthProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    provider_data: OAuthProviderCreate,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
):
    """
    Create a new OAuth provider.

    Requires admin access.

    Args:
        provider_data: Provider creation data
        admin: Authenticated admin user (injected)

    Returns:
        Created OAuthProviderResponse

    Raises:
        HTTPException: 400 if provider already exists or validation fails
    """
    logger.info(f"Admin {admin.email} creating OAuth provider: {provider_data.provider_id}")

    try:
        provider = await provider_repo.create_provider(provider_data)
        return OAuthProviderResponse.from_provider(provider)

    except ValueError as e:
        logger.warning(f"Provider creation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.patch("/{provider_id}", response_model=OAuthProviderResponse)
async def update_provider(
    provider_id: str,
    updates: OAuthProviderUpdate,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
):
    """
    Update an OAuth provider.

    Requires admin access.

    Note: If scopes are updated, existing user connections may need to re-authenticate.
    The system tracks scope changes via hash and will prompt users to re-auth.

    Args:
        provider_id: Provider identifier
        updates: Fields to update
        admin: Authenticated admin user (injected)

    Returns:
        Updated OAuthProviderResponse

    Raises:
        HTTPException:
            - 400 if validation fails
            - 404 if provider not found
    """
    logger.info(f"Admin {admin.email} updating OAuth provider: {provider_id}")

    # Track if scopes changed (will invalidate cached tokens)
    old_provider = await provider_repo.get_provider(provider_id)
    scopes_changed = (
        old_provider
        and updates.scopes is not None
        and set(updates.scopes) != set(old_provider.scopes)
    )

    provider = await provider_repo.update_provider(provider_id, updates)

    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    # Invalidate cached tokens for this provider if scopes changed
    if scopes_changed:
        cache = get_token_cache()
        evicted = cache.delete_for_provider(provider_id)
        logger.info(
            f"Scopes changed for provider {provider_id}, "
            f"evicted {evicted} cached tokens"
        )

    return OAuthProviderResponse.from_provider(provider)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    force: bool = Query(
        False,
        description="Force delete even if users are connected (will delete their tokens)",
    ),
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    token_repo: OAuthTokenRepository = Depends(get_token_repository),
):
    """
    Delete an OAuth provider.

    Requires admin access.

    Warning: If users are connected to this provider, their tokens will be deleted
    unless force=False (default), in which case the deletion will fail.

    Args:
        provider_id: Provider identifier
        force: If True, delete even if users are connected
        admin: Authenticated admin user (injected)

    Raises:
        HTTPException:
            - 400 if users are connected and force=False
            - 404 if provider not found
    """
    logger.info(f"Admin {admin.email} deleting OAuth provider: {provider_id}")

    # Check for connected users
    connected_tokens = await token_repo.list_provider_tokens(provider_id)

    if connected_tokens and not force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete provider with {len(connected_tokens)} connected users. "
                "Use force=true to delete anyway (will remove user connections)."
            ),
        )

    # Delete user tokens if any
    if connected_tokens:
        deleted_count = await token_repo.delete_provider_tokens(provider_id)
        logger.info(f"Deleted {deleted_count} user tokens for provider {provider_id}")

    # Delete provider
    deleted = await provider_repo.delete_provider(provider_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    # Clear cached tokens
    cache = get_token_cache()
    cache.delete_for_provider(provider_id)

    return None


# =============================================================================
# Provider Statistics
# =============================================================================


@router.get("/{provider_id}/connections/count")
async def get_provider_connection_count(
    provider_id: str,
    admin: User = Depends(require_admin),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    token_repo: OAuthTokenRepository = Depends(get_token_repository),
):
    """
    Get the number of users connected to a provider.

    Requires admin access.

    Args:
        provider_id: Provider identifier
        admin: Authenticated admin user (injected)

    Returns:
        Dict with provider_id and connection_count

    Raises:
        HTTPException: 404 if provider not found
    """
    logger.info(f"Admin {admin.email} getting connection count for: {provider_id}")

    # Verify provider exists
    provider = await provider_repo.get_provider(provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_id}' not found",
        )

    tokens = await token_repo.list_provider_tokens(provider_id)

    return {
        "provider_id": provider_id,
        "connection_count": len(tokens),
    }
