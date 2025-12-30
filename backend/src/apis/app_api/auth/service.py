"""OIDC authentication service for Entra ID."""

import base64
import hashlib
import logging
import os
import secrets
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from apis.shared.auth.state_store import OIDCStateData, StateStore, create_state_store

logger = logging.getLogger(__name__)


def generate_pkce_pair() -> Tuple[str, str]:
    """
    Generate PKCE code verifier and challenge (S256).

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    # Generate 32 bytes of random data for code_verifier (43-128 chars when base64 encoded)
    code_verifier = secrets.token_urlsafe(32)

    # Create code_challenge using S256: BASE64URL(SHA256(code_verifier))
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    return code_verifier, code_challenge


class OIDCAuthService:
    """Service for handling OIDC authentication with Entra ID."""
    
    def __init__(self):
        """Initialize OIDC service with configuration from environment."""
        self.tenant_id = os.getenv('ENTRA_TENANT_ID')
        self.client_id = os.getenv('ENTRA_CLIENT_ID')
        self.client_secret = os.getenv('ENTRA_CLIENT_SECRET')
        self.redirect_uri = os.getenv('ENTRA_REDIRECT_URI')
        
        if not all([self.tenant_id, self.client_id, self.client_secret, self.redirect_uri]):
            raise ValueError(
                "ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, and ENTRA_REDIRECT_URI "
                "environment variables are required"
            )
        
        # Use OIDC v2.0 endpoints (Microsoft Identity Platform)
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.authorization_endpoint = f"{self.authority}/oauth2/v2.0/authorize"
        self.token_endpoint = f"{self.authority}/oauth2/v2.0/token"
        
        # Build scope string with API scope (matches frontend format)
        # Format: openid profile email api://{client_id}/Read offline_access
        self.scope = f"openid profile email api://{self.client_id}/Read offline_access"
        
        # Distributed state storage (DynamoDB in production, in-memory for local dev)
        self.state_store: StateStore = create_state_store()
        
        # State TTL in seconds (10 minutes)
        self._state_ttl = 600
    
    def generate_state(
        self,
        redirect_uri: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """
        Generate secure state, PKCE verifier/challenge, and nonce for OAuth flow.

        Args:
            redirect_uri: Optional redirect URI to store with state

        Returns:
            Tuple of (state, code_challenge, nonce)
        """
        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = generate_pkce_pair()
        nonce = secrets.token_urlsafe(32)

        # Store state with PKCE verifier and nonce for validation during callback
        self.state_store.store_state(
            state=state,
            data=OIDCStateData(
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                nonce=nonce,
            ),
            ttl_seconds=self._state_ttl
        )
        return state, code_challenge, nonce

    def validate_state(self, state: str) -> Tuple[bool, Optional[OIDCStateData]]:
        """
        Validate state token and return associated OIDC data.

        Uses distributed state store to ensure state validation works
        across multiple instances in a distributed system.

        Args:
            state: State token to validate

        Returns:
            Tuple of (is_valid, OIDCStateData or None)
        """
        return self.state_store.get_and_delete_state(state)
    
    def build_authorization_url(
        self,
        state: str,
        code_challenge: str,
        nonce: str,
        redirect_uri: Optional[str] = None,
        prompt: str = "select_account"
    ) -> str:
        """
        Build Entra ID authorization URL with PKCE and nonce.

        Args:
            state: CSRF protection state token
            code_challenge: PKCE code challenge (S256)
            nonce: ID token nonce binding
            redirect_uri: Optional redirect URI (defaults to configured value)
            prompt: Prompt parameter (select_account, login, consent, etc.)

        Returns:
            Complete authorization URL with PKCE parameters
        """
        redirect = redirect_uri or self.redirect_uri

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "response_mode": "query",
            "scope": self.scope,  # Includes API scope: api://{client_id}/Read
            "state": state,
            "nonce": nonce,  # ID token binding
            "code_challenge": code_challenge,  # PKCE
            "code_challenge_method": "S256",  # PKCE method
            "prompt": prompt,
        }

        # Returns OIDC v2.0 authorization URL format:
        # https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?...
        return f"{self.authorization_endpoint}?{urlencode(params)}"
    
    async def exchange_code_for_tokens(
        self,
        code: str,
        state: str,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Exchange authorization code for access and refresh tokens.

        Validates state, retrieves stored PKCE code_verifier, and exchanges
        the authorization code for tokens.

        Args:
            code: Authorization code from Entra ID
            state: State token for CSRF protection
            redirect_uri: Optional redirect URI (must match authorization request)

        Returns:
            Dictionary containing access_token, refresh_token, id_token, and expires_in

        Raises:
            HTTPException: If token exchange fails or state/nonce is invalid
        """
        # Validate state and retrieve stored OIDC data (code_verifier, nonce)
        logger.debug(f"Validating state token: {state[:16]}..." if len(state) > 16 else f"Validating state token: {state}")
        is_valid, state_data = self.validate_state(state)
        if not is_valid or state_data is None:
            logger.warning(f"Invalid or expired state token: {state[:16]}..." if len(state) > 16 else f"Invalid or expired state token: {state}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired state parameter. Please initiate login again."
            )

        # Use stored redirect URI if available, otherwise use provided/default
        redirect = state_data.redirect_uri or redirect_uri or self.redirect_uri

        # Prepare token request with OIDC v2.0 token endpoint
        # Include PKCE code_verifier for validation
        token_data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
            "scope": self.scope,  # Must match authorization request scope
            "code_verifier": state_data.code_verifier,  # PKCE verification
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.token_endpoint,
                    data=token_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10.0
                )
                response.raise_for_status()
                token_response = response.json()

                # Validate nonce in ID token if present
                id_token = token_response.get("id_token")
                if id_token and state_data.nonce:
                    import jwt
                    try:
                        # Decode without verification to check nonce
                        # (Signature is validated by Entra ID during token exchange)
                        id_claims = jwt.decode(id_token, options={"verify_signature": False})
                        token_nonce = id_claims.get("nonce")
                        if token_nonce != state_data.nonce:
                            logger.error(
                                f"Nonce mismatch: expected={state_data.nonce[:8]}..., "
                                f"got={token_nonce[:8] if token_nonce else 'None'}..."
                            )
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail="ID token nonce validation failed. Please try again."
                            )
                    except jwt.DecodeError as e:
                        logger.error(f"Failed to decode ID token for nonce validation: {e}")
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid ID token received. Please try again."
                        )

                logger.info("Successfully exchanged authorization code for tokens")

                return {
                    "access_token": token_response.get("access_token"),
                    "refresh_token": token_response.get("refresh_token"),
                    "id_token": token_response.get("id_token"),
                    "token_type": token_response.get("token_type", "Bearer"),
                    "expires_in": token_response.get("expires_in", 3600),
                    "scope": token_response.get("scope", ""),
                }

        except httpx.HTTPStatusError as e:
            logger.error(f"Token exchange failed with status {e.response.status_code}: {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to exchange authorization code for tokens. Please try again."
            )
        except httpx.RequestError as e:
            logger.error(f"Token exchange request failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable. Please try again later."
            )
    
    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """
        Refresh access token using refresh token.
        
        Args:
            refresh_token: Refresh token from previous authentication
            
        Returns:
            Dictionary containing new access_token, refresh_token, id_token, and expires_in
            
        Raises:
            HTTPException: If token refresh fails
        """
        # Refresh token request with OIDC v2.0 token endpoint
        token_data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": self.scope,  # Includes API scope: api://{client_id}/Read
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.token_endpoint,
                    data=token_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10.0
                )
                response.raise_for_status()
                token_response = response.json()
                
                logger.info("Successfully refreshed access token")
                
                return {
                    "access_token": token_response.get("access_token"),
                    "refresh_token": token_response.get("refresh_token") or refresh_token,
                    "id_token": token_response.get("id_token"),
                    "token_type": token_response.get("token_type", "Bearer"),
                    "expires_in": token_response.get("expires_in", 3600),
                    "scope": token_response.get("scope", ""),
                }
                
        except httpx.HTTPStatusError as e:
            logger.error(f"Token refresh failed with status {e.response.status_code}: {e.response.text}")
            if e.response.status_code == 400:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired refresh token. Please login again."
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to refresh access token. Please try again."
            )
        except httpx.RequestError as e:
            logger.error(f"Token refresh request failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable. Please try again later."
            )


# Global service instance
_service: Optional[OIDCAuthService] = None


def _check_auth_config() -> None:
    """Check if required authentication environment variables are set."""
    required_vars = [
        'ENTRA_TENANT_ID',
        'ENTRA_CLIENT_ID',
        'ENTRA_CLIENT_SECRET',
        'ENTRA_REDIRECT_URI'
    ]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise ValueError(
            f"Missing required environment variables for authentication: {', '.join(missing_vars)}. "
            f"Please set these variables to enable OIDC authentication with Entra ID."
        )


def get_auth_service() -> OIDCAuthService:
    """Get or create the global OIDC auth service instance."""
    global _service
    if _service is None:
        _check_auth_config()
        _service = OIDCAuthService()
    return _service

