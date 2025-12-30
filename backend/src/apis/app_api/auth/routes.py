"""OIDC authentication routes for Entra ID."""

import logging
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from .models import (
    LoginResponse,
    TokenExchangeRequest,
    TokenExchangeResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
)
from .service import get_auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get(
    "/login",
    summary="Initiate OIDC login",
    description="""
    Initiates the OIDC authentication flow by redirecting the user to Entra ID login.

    This endpoint generates secure tokens and returns the authorization URL
    that the client should redirect the user to.

    ## Security Features

    - **State Parameter**: A cryptographically secure random token is generated
      and must be validated when the authorization code is exchanged for tokens.
    - **PKCE (S256)**: Proof Key for Code Exchange prevents authorization code
      interception attacks. The code_verifier is stored server-side and sent
      during token exchange.
    - **Nonce Binding**: A cryptographic nonce is included in the ID token to
      prevent token replay attacks.
    - **Redirect URI Validation**: The redirect URI is validated to match the
      configured value or the provided value.

    ## Usage Flow

    1. Client calls `/auth/login` to get the authorization URL and state token
    2. Client redirects user to the authorization URL
    3. User authenticates with Entra ID
    4. Entra ID redirects back to the configured redirect URI with an authorization code
    5. Client calls `/auth/token` with the code and state to exchange for tokens

    ## Response

    Returns a JSON object with:
    - `authorization_url`: The URL to redirect the user to (includes PKCE challenge)
    - `state`: The state token that must be included in the token exchange request

    ## Example Response

    ```json
    {
        "authorization_url": "https://login.microsoftonline.com/.../oauth2/v2.0/authorize?...",
        "state": "abc123..."
    }
    ```
    """,
    response_model=LoginResponse,
    responses={
        200: {
            "description": "Authorization URL generated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "authorization_url": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?client_id=...",
                        "state": "random-state-token"
                    }
                }
            }
        },
        500: {
            "description": "Internal server error",
        }
    }
)
async def login(
    redirect_uri: str = Query(None, description="Optional redirect URI override"),
    prompt: str = Query("select_account", description="Prompt type (select_account, login, consent)")
) -> LoginResponse:
    """
    Generate authorization URL for Entra ID login.
    
    Creates a secure state token and builds the Entra ID authorization URL
    with proper security parameters.
    
    Args:
        redirect_uri: Optional redirect URI (defaults to configured value)
        prompt: Prompt parameter for Entra ID (defaults to "select_account")
        
    Returns:
        LoginResponse with authorization URL and state token
    """
    try:
        auth_service = get_auth_service()

        # Generate secure state token with PKCE and nonce
        state, code_challenge, nonce = auth_service.generate_state(redirect_uri=redirect_uri)

        # Build authorization URL with PKCE parameters
        authorization_url = auth_service.build_authorization_url(
            state=state,
            code_challenge=code_challenge,
            nonce=nonce,
            redirect_uri=redirect_uri,
            prompt=prompt
        )

        logger.info("Generated authorization URL for OIDC login with PKCE")

        return LoginResponse(
            authorization_url=authorization_url,
            state=state
        )
        
    except ValueError as e:
        # Missing configuration - return 503 Service Unavailable
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error generating authorization URL: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate authorization URL"
        )


@router.post(
    "/token",
    summary="Exchange authorization code for tokens",
    description="""
    Exchanges an authorization code for access and refresh tokens.

    This endpoint completes the OIDC authorization code flow by exchanging
    the authorization code received from Entra ID for access and refresh tokens.

    ## Security

    - **State Validation**: The state parameter is validated to prevent CSRF attacks.
      The state must match the one generated during the login request.
    - **PKCE Verification**: The code_verifier stored during login is sent to validate
      that the same client is completing the flow.
    - **Nonce Validation**: The nonce in the ID token is verified against the stored
      value to prevent token replay attacks.
    - **One-Time Use**: State tokens are single-use and expire after 10 minutes.
    - **Code Validation**: The authorization code is validated with Entra ID.

    ## Request Body

    - **code**: Authorization code from Entra ID callback
    - **state**: State token from login request (must match)
    - **redirect_uri**: Optional redirect URI (must match authorization request)

    ## Response

    Returns tokens including:
    - `access_token`: JWT access token for API authentication
    - `refresh_token`: Token for refreshing the access token
    - `id_token`: ID token containing user information
    - `expires_in`: Access token expiration time in seconds

    ## Error Responses

    - **400 Bad Request**: Invalid or expired state, PKCE/nonce mismatch, or token exchange failed
    - **503 Service Unavailable**: Entra ID service unavailable

    ## Example Request

    ```json
    {
        "code": "authorization-code-from-entraid",
        "state": "state-token-from-login",
        "redirect_uri": "https://example.com/callback"
    }
    ```
    """,
    response_model=TokenExchangeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Tokens exchanged successfully",
            "content": {
                "application/json": {
                    "example": {
                        "access_token": "eyJhbGciOiJSUzI1NiIs...",
                        "refresh_token": "refresh-token-value",
                        "id_token": "eyJhbGciOiJSUzI1NiIs...",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "openid profile email offline_access"
                    }
                }
            }
        },
        400: {
            "description": "Invalid request (invalid state, code, etc.)",
        },
        503: {
            "description": "Authentication service unavailable",
        }
    }
)
async def exchange_token(request: TokenExchangeRequest) -> TokenExchangeResponse:
    """
    Exchange authorization code for access and refresh tokens.
    
    Validates the state token and exchanges the authorization code
    with Entra ID for access and refresh tokens.
    
    Args:
        request: Token exchange request with code, state, and optional redirect_uri
        
    Returns:
        TokenExchangeResponse with access_token, refresh_token, and related information
    """
    try:
        auth_service = get_auth_service()
        
        tokens = await auth_service.exchange_code_for_tokens(
            code=request.code,
            state=request.state,
            redirect_uri=request.redirect_uri
        )
        
        return TokenExchangeResponse(**tokens)
    except ValueError as e:
        # Missing configuration
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )


@router.post(
    "/refresh",
    summary="Refresh access token",
    description="""
    Refreshes an access token using a refresh token.
    
    This endpoint allows clients to obtain a new access token without requiring
    the user to re-authenticate. The refresh token is exchanged for a new
    access token and optionally a new refresh token.
    
    ## Security
    
    - **Token Validation**: The refresh token is validated with Entra ID.
    - **Automatic Rotation**: Entra ID may issue a new refresh token, which
      should replace the old one.
    
    ## Request Body
    
    - **refresh_token**: Refresh token from previous authentication
    
    ## Response
    
    Returns new tokens including:
    - `access_token`: New JWT access token
    - `refresh_token`: New refresh token (may be same as input)
    - `id_token`: New ID token containing user information
    - `expires_in`: Access token expiration time in seconds
    
    ## Error Responses
    
    - **401 Unauthorized**: Invalid or expired refresh token
    - **400 Bad Request**: Token refresh failed
    - **503 Service Unavailable**: Entra ID service unavailable
    
    ## Example Request
    
    ```json
    {
        "refresh_token": "refresh-token-value"
    }
    ```
    """,
    response_model=TokenRefreshResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Token refreshed successfully",
            "content": {
                "application/json": {
                    "example": {
                        "access_token": "eyJhbGciOiJSUzI1NiIs...",
                        "refresh_token": "new-refresh-token-value",
                        "id_token": "eyJhbGciOiJSUzI1NiIs...",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "openid profile email offline_access"
                    }
                }
            }
        },
        401: {
            "description": "Invalid or expired refresh token",
        },
        400: {
            "description": "Token refresh failed",
        },
        503: {
            "description": "Authentication service unavailable",
        }
    }
)
async def refresh_token(request: TokenRefreshRequest) -> TokenRefreshResponse:
    """
    Refresh access token using refresh token.
    
    Exchanges a refresh token with Entra ID for a new access token
    and optionally a new refresh token.
    
    Args:
        request: Token refresh request with refresh_token
        
    Returns:
        TokenRefreshResponse with new access_token, refresh_token, and related information
    """
    try:
        auth_service = get_auth_service()
        
        tokens = await auth_service.refresh_access_token(request.refresh_token)
        
        return TokenRefreshResponse(**tokens)
    except ValueError as e:
        # Missing configuration
        logger.error(f"Authentication not configured: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )

