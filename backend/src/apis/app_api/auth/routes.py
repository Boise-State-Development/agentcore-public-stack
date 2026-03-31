"""OIDC authentication routes with multi-provider support."""

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .models import (
    LoginResponse,
    LogoutResponse,
    RuntimeEndpointResponse,
    TokenExchangeRequest,
    TokenExchangeResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
)
from .service import get_generic_auth_service
from apis.shared.auth_providers.models import (
    AuthProviderPublicInfo,
    AuthProviderPublicListResponse,
)
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get(
    "/providers",
    response_model=AuthProviderPublicListResponse,
    summary="List enabled authentication providers",
)
async def list_auth_providers() -> AuthProviderPublicListResponse:
    """
    Public endpoint (no authentication required).

    Returns enabled auth providers for the login page to display
    provider selection buttons.
    """
    try:
        from apis.shared.auth_providers.repository import get_auth_provider_repository

        repo = get_auth_provider_repository()
        if not repo.enabled:
            return AuthProviderPublicListResponse(providers=[])

        providers = await repo.list_providers(enabled_only=True)
        return AuthProviderPublicListResponse(
            providers=[
                AuthProviderPublicInfo(
                    provider_id=p.provider_id,
                    display_name=p.display_name,
                    logo_url=p.logo_url,
                    button_color=p.button_color,
                )
                for p in providers
            ]
        )
    except Exception as e:
        logger.debug(f"Error listing auth providers (may not be configured): {e}")
        return AuthProviderPublicListResponse(providers=[])


@router.get(
    "/login",
    response_model=LoginResponse,
    summary="Initiate OIDC login",
)
async def login(
    request: Request,
    provider_id: str = Query(..., description="Auth provider ID"),
    redirect_uri: str = Query(None, description="Optional redirect URI override"),
    prompt: str = Query("select_account", description="Prompt type (select_account, login, consent)")
) -> LoginResponse:
    """
    Generate authorization URL for OIDC login.

    Requires a provider_id that references an enabled auth provider
    configured via the admin OIDC provider setup.

    When no redirect_uri is configured (neither in the query param nor on the
    provider), one is auto-derived from the request's Origin or Referer header
    by appending /auth/callback.
    """
    try:
        auth_service = await get_generic_auth_service(provider_id)

        # Auto-derive redirect_uri from request origin when not configured
        effective_redirect_uri = redirect_uri
        if not effective_redirect_uri and not auth_service.redirect_uri:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                # Strip path from referer to get just the origin
                from urllib.parse import urlparse
                parsed = urlparse(origin)
                base = f"{parsed.scheme}://{parsed.netloc}"
                effective_redirect_uri = f"{base}/auth/callback"
                logger.info(f"Auto-derived redirect_uri from request origin: {effective_redirect_uri}")

        state, code_challenge, nonce = auth_service.generate_state(redirect_uri=effective_redirect_uri)

        authorization_url = auth_service.build_authorization_url(
            state=state,
            code_challenge=code_challenge,
            nonce=nonce,
            redirect_uri=effective_redirect_uri,
            prompt=prompt
        )

        logger.info("Generated authorization URL for OIDC login")

        return LoginResponse(
            authorization_url=authorization_url,
            state=state
        )

    except ValueError as e:
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating authorization URL: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate authorization URL"
        )


@router.post(
    "/token",
    response_model=TokenExchangeResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange authorization code for tokens",
)
async def exchange_token(request: TokenExchangeRequest) -> TokenExchangeResponse:
    """
    Exchange authorization code for access and refresh tokens.

    Resolves the auth provider from the stored state's provider_id.
    """
    try:
        # Peek at the state to determine provider (without consuming it)
        # The actual state validation/consumption happens inside exchange_code_for_tokens
        provider_id = _peek_provider_from_state(request.state)

        if not provider_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not resolve auth provider from state. Please initiate login again."
            )

        auth_service = await get_generic_auth_service(provider_id)

        tokens = await auth_service.exchange_code_for_tokens(
            code=request.code,
            state=request.state,
            redirect_uri=request.redirect_uri
        )

        return TokenExchangeResponse(**tokens)
    except ValueError as e:
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exchanging token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token exchange failed."
        )


@router.post(
    "/refresh",
    response_model=TokenRefreshResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh access token",
)
async def refresh_token(
    request: TokenRefreshRequest,
    provider_id: str = Query(..., description="Auth provider ID"),
) -> TokenRefreshResponse:
    """
    Refresh access token using refresh token.

    Requires a provider_id to route to the correct provider's token endpoint.
    """
    try:
        auth_service = await get_generic_auth_service(provider_id)

        tokens = await auth_service.refresh_access_token(request.refresh_token)

        return TokenRefreshResponse(**tokens)
    except ValueError as e:
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token refresh failed."
        )


@router.get(
    "/logout",
    response_model=LogoutResponse,
    summary="Get logout URL",
)
async def logout(
    provider_id: str = Query(..., description="Auth provider ID"),
    post_logout_redirect_uri: str = Query(
        None,
        description="URL to redirect to after logout"
    )
) -> LogoutResponse:
    """
    Get logout URL for ending the user's session.

    Requires a provider_id to return the correct provider's end session URL.
    """
    try:
        auth_service = await get_generic_auth_service(provider_id)

        logout_url = auth_service.build_logout_url(
            post_logout_redirect_uri=post_logout_redirect_uri
        )

        logger.info("Generated logout URL")

        return LogoutResponse(logout_url=logout_url)

    except ValueError as e:
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating logout URL: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate logout URL"
        )


def _peek_provider_from_state(state: str) -> Optional[str]:
    """
    Peek at the OIDC state to determine which provider initiated the flow.

    This reads the state from the store WITHOUT consuming it. The actual
    consumption happens inside the auth service's exchange_code_for_tokens.

    For the in-memory store we inspect the internal dict directly.
    For DynamoDB we do a GetItem without the atomic delete.
    """
    try:
        from apis.shared.auth.state_store import create_state_store

        store = create_state_store()

        # For InMemoryStateStore, peek at the internal dict
        if hasattr(store, '_store'):
            entry = store._store.get(state)
            if entry:
                _, data = entry
                return data.provider_id if data else None
            return None

        # For DynamoDBStateStore, do a non-destructive read
        if hasattr(store, 'table'):
            import time
            response = store.table.get_item(
                Key={
                    'PK': f'STATE#{state}',
                    'SK': f'STATE#{state}',
                },
                ConsistentRead=True,
            )
            item = response.get('Item')
            if item:
                expires_at = item.get('expiresAt', 0)
                if int(time.time()) <= expires_at:
                    return item.get('provider_id')
            return None

    except Exception as e:
        logger.debug(f"Could not peek provider from state: {e}")

    return None


# Redefine the endpoint with proper dependency injection
@router.get(
    "/runtime-endpoint",
    response_model=RuntimeEndpointResponse,
    summary="Get AgentCore Runtime endpoint URL for user's provider",
)
async def get_runtime_endpoint_impl(
    current_user: User = Depends(get_current_user)
) -> RuntimeEndpointResponse:
    """
    Get the AgentCore Runtime endpoint URL for the authenticated user's auth provider.

    This endpoint requires authentication. The provider ID is extracted from the
    user's JWT token by resolving the issuer to a configured auth provider.

    Returns:
        RuntimeEndpointResponse with the runtime endpoint URL and status

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if provider not found or runtime not ready
            - 500 if runtime endpoint not configured
    """
    from apis.shared.auth.generic_jwt_validator import GenericOIDCJWTValidator
    from apis.shared.auth_providers.repository import get_auth_provider_repository

    try:
        repo = get_auth_provider_repository()
        if not repo.enabled:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Auth provider repository not configured"
            )

        validator = GenericOIDCJWTValidator(repo)

        # Get the raw token from the user object
        token = getattr(current_user, 'raw_token', None)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token not available"
            )

        # Resolve provider from token
        provider = await validator.resolve_provider_from_token(token)
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Auth provider not found for this token"
            )

        # Check if runtime endpoint is configured
        if not provider.agentcore_runtime_endpoint_url:
            if provider.agentcore_runtime_status == "PENDING":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Runtime is being provisioned for provider '{provider.provider_id}'. Please try again in a few minutes."
                )
            elif provider.agentcore_runtime_status == "CREATING":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Runtime is currently being created for provider '{provider.provider_id}'. Please try again in a few minutes."
                )
            elif provider.agentcore_runtime_status == "FAILED":
                error_msg = provider.agentcore_runtime_error or "Unknown error"
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Runtime provisioning failed for provider '{provider.provider_id}': {error_msg}"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Runtime endpoint not configured for provider '{provider.provider_id}'"
                )

        logger.info(
            f"Resolved runtime endpoint for user {current_user.user_id} "
            f"(provider: {provider.provider_id}): {provider.agentcore_runtime_endpoint_url}"
        )

        # Allow local override for development (bypass cloud runtime)
        runtime_url = os.environ.get(
            "LOCAL_RUNTIME_ENDPOINT_URL",
            provider.agentcore_runtime_endpoint_url,
        )

        return RuntimeEndpointResponse(
            runtime_endpoint_url=runtime_url,
            provider_id=provider.provider_id,
            runtime_status=provider.agentcore_runtime_status,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving runtime endpoint: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve runtime endpoint"
        )
