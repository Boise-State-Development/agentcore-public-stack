"""OIDC authentication service for Entra ID."""

import logging
import os
import secrets
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from apis.shared.auth.state_store import StateStore, create_state_store

logger = logging.getLogger(__name__)


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
    
    def generate_state(self, redirect_uri: Optional[str] = None) -> str:
        """
        Generate a secure random state token for CSRF protection.
        
        Args:
            redirect_uri: Optional redirect URI to store with state
            
        Returns:
            Secure random state token
        """
        state = secrets.token_urlsafe(32)
        self.state_store.store_state(
            state=state,
            redirect_uri=redirect_uri,
            ttl_seconds=self._state_ttl
        )
        return state
    
    def validate_state(self, state: str) -> Tuple[bool, Optional[str]]:
        """
        Validate state token and return associated redirect URI.
        
        Uses distributed state store to ensure state validation works
        across multiple instances in a distributed system.
        
        Args:
            state: State token to validate
            
        Returns:
            Tuple of (is_valid, redirect_uri)
        """
        return self.state_store.get_and_delete_state(state)
    
    def build_authorization_url(
        self,
        state: str,
        redirect_uri: Optional[str] = None,
        prompt: str = "select_account"
    ) -> str:
        """
        Build Entra ID authorization URL with proper parameters.
        
        Args:
            state: CSRF protection state token
            redirect_uri: Optional redirect URI (defaults to configured value)
            prompt: Prompt parameter (select_account, login, consent, etc.)
            
        Returns:
            Complete authorization URL
        """
        redirect = redirect_uri or self.redirect_uri
        
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "response_mode": "query",
            "scope": self.scope,  # Includes API scope: api://{client_id}/Read
            "state": state,
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
        
        Args:
            code: Authorization code from Entra ID
            state: State token for CSRF protection
            redirect_uri: Optional redirect URI (must match authorization request)
            
        Returns:
            Dictionary containing access_token, refresh_token, id_token, and expires_in
            
        Raises:
            HTTPException: If token exchange fails
        """
        # Validate state
        logger.debug(f"Validating state token: {state[:16]}..." if len(state) > 16 else f"Validating state token: {state}")
        is_valid, stored_redirect = self.validate_state(state)
        if not is_valid:
            logger.warning(f"Invalid or expired state token: {state[:16]}..." if len(state) > 16 else f"Invalid or expired state token: {state}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired state parameter. Please initiate login again."
            )
        
        # Use stored redirect URI if available, otherwise use provided/default
        redirect = stored_redirect or redirect_uri or self.redirect_uri
        
        # Prepare token request with OIDC v2.0 token endpoint
        # Must include same scope as authorization request
        token_data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
            "scope": self.scope,  # Must match authorization request scope
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

