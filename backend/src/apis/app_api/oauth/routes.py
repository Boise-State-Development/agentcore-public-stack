"""User-facing OAuth routes for connection management."""

import logging
import os
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from apis.shared.auth import User, get_current_user
from apis.shared.rbac.service import AppRoleService, get_app_role_service

from .models import (
    OAuthConnectionListResponse,
    OAuthConnectResponse,
    OAuthProviderListResponse,
    OAuthProviderResponse,
)
from .provider_repository import OAuthProviderRepository, get_provider_repository
from .service import OAuthService, get_oauth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])


# =============================================================================
# Provider Discovery (filtered by user roles)
# =============================================================================


@router.get("/providers", response_model=OAuthProviderListResponse)
async def list_available_providers(
    current_user: User = Depends(get_current_user),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
):
    """
    List OAuth providers available to the current user.

    Filters providers based on user's application roles.

    Returns:
        OAuthProviderListResponse with available providers
    """
    logger.info(f"User {current_user.email} listing available OAuth providers")

    # Resolve user's application roles
    permissions = await role_service.resolve_user_permissions(current_user)
    user_roles = permissions.app_roles if permissions.app_roles else []

    # Get enabled providers
    providers = await provider_repo.list_providers(enabled_only=True)

    # Filter by user roles
    available = []
    for provider in providers:
        if not provider.allowed_roles or any(
            role in provider.allowed_roles for role in user_roles
        ):
            available.append(OAuthProviderResponse.from_provider(provider))

    return OAuthProviderListResponse(
        providers=available,
        total=len(available),
    )


# =============================================================================
# User Connections
# =============================================================================


@router.get("/connections", response_model=OAuthConnectionListResponse)
async def list_user_connections(
    current_user: User = Depends(get_current_user),
    oauth_service: OAuthService = Depends(get_oauth_service),
    role_service: AppRoleService = Depends(get_app_role_service),
):
    """
    List the current user's OAuth connections.

    Returns all available providers with connection status.

    Returns:
        OAuthConnectionListResponse with connection statuses
    """
    logger.info(f"User {current_user.email} listing OAuth connections")

    # Resolve user's application roles
    permissions = await role_service.resolve_user_permissions(current_user)
    user_roles = permissions.app_roles if permissions.app_roles else []

    connections = await oauth_service.get_user_connections(
        user_id=current_user.user_id,
        user_roles=user_roles,
    )

    return OAuthConnectionListResponse(connections=connections)


# =============================================================================
# OAuth Flow
# =============================================================================


@router.get("/connect/{provider_id}", response_model=OAuthConnectResponse)
async def initiate_connection(
    provider_id: str,
    redirect: Optional[str] = Query(
        None,
        description="Frontend URL to redirect after OAuth callback",
    ),
    current_user: User = Depends(get_current_user),
    oauth_service: OAuthService = Depends(get_oauth_service),
    role_service: AppRoleService = Depends(get_app_role_service),
):
    """
    Initiate OAuth connection flow for a provider.

    Returns an authorization URL that the frontend should redirect to.

    Args:
        provider_id: Provider to connect to
        redirect: Optional frontend redirect URL after completion

    Returns:
        OAuthConnectResponse with authorization URL

    Raises:
        HTTPException:
            - 404 if provider not found
            - 403 if user not authorized for provider
    """
    logger.info(
        f"User {current_user.email} initiating OAuth connection to {provider_id}"
    )

    # Resolve user's application roles
    permissions = await role_service.resolve_user_permissions(current_user)
    user_roles = permissions.app_roles if permissions.app_roles else []

    authorization_url = await oauth_service.initiate_connect(
        provider_id=provider_id,
        user_id=current_user.user_id,
        user_roles=user_roles,
        frontend_redirect=redirect,
    )

    return OAuthConnectResponse(authorization_url=authorization_url)


@router.get("/callback")
async def oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    oauth_service: OAuthService = Depends(get_oauth_service),
):
    """
    Handle OAuth callback from provider.

    This endpoint is called by the OAuth provider after user authorization.
    Exchanges the code for tokens and redirects to the frontend.

    Args:
        code: Authorization code from provider
        state: State parameter for validation
        error: Error code if authorization failed
        error_description: Error description if authorization failed

    Returns:
        Redirect to frontend with success/error query params
    """
    # Get frontend base URL from environment
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:4200")
    callback_path = "/settings/oauth/callback"

    # Handle error from provider
    if error:
        logger.warning(f"OAuth callback error: {error} - {error_description}")
        params = urlencode({"error": error, "error_description": error_description or ""})
        return RedirectResponse(
            url=f"{frontend_url}{callback_path}?{params}",
            status_code=status.HTTP_302_FOUND,
        )

    # Validate required params
    if not code or not state:
        logger.warning("OAuth callback missing code or state")
        params = urlencode({"error": "missing_params"})
        return RedirectResponse(
            url=f"{frontend_url}{callback_path}?{params}",
            status_code=status.HTTP_302_FOUND,
        )

    # Process callback
    provider_id, frontend_redirect, callback_error = await oauth_service.handle_callback(
        code=code,
        state=state,
    )

    # Build redirect URL
    redirect_base = frontend_redirect or f"{frontend_url}{callback_path}"

    if callback_error:
        params = urlencode({"error": callback_error, "provider": provider_id})
        return RedirectResponse(
            url=f"{redirect_base}?{params}",
            status_code=status.HTTP_302_FOUND,
        )

    # Success
    params = urlencode({"success": "true", "provider": provider_id})
    return RedirectResponse(
        url=f"{redirect_base}?{params}",
        status_code=status.HTTP_302_FOUND,
    )


# =============================================================================
# Disconnect
# =============================================================================


@router.delete("/connections/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_provider(
    provider_id: str,
    current_user: User = Depends(get_current_user),
    oauth_service: OAuthService = Depends(get_oauth_service),
):
    """
    Disconnect from an OAuth provider.

    Revokes tokens if possible and removes the connection.

    Args:
        provider_id: Provider to disconnect from

    Raises:
        HTTPException: 404 if not connected to provider
    """
    logger.info(f"User {current_user.email} disconnecting from {provider_id}")

    disconnected = await oauth_service.disconnect(
        user_id=current_user.user_id,
        provider_id=provider_id,
    )

    if not disconnected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Not connected to provider '{provider_id}'",
        )

    return None
