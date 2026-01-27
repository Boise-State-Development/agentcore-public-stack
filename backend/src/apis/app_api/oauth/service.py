"""OAuth service for managing provider connections and token exchange."""

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import HTTPException, status

from apis.shared.auth.state_store import StateStore, create_state_store

from .encryption import TokenEncryptionService, get_token_encryption_service
from .models import (
    OAuthConnectionResponse,
    OAuthConnectionStatus,
    OAuthProvider,
    OAuthUserToken,
    compute_scopes_hash,
)
from .provider_repository import OAuthProviderRepository, get_provider_repository
from .token_cache import TokenCache, get_token_cache
from .token_repository import OAuthTokenRepository, get_token_repository

logger = logging.getLogger(__name__)


@dataclass
class OAuthStateData:
    """Data stored with OAuth state for security validation."""

    provider_id: str
    user_id: str
    code_verifier: Optional[str] = None  # PKCE code verifier (S256)
    redirect_uri: Optional[str] = None  # Frontend redirect after callback


def generate_pkce_pair() -> Tuple[str, str]:
    """
    Generate PKCE code verifier and challenge (S256).

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    # Generate 32 bytes of random data for code_verifier
    code_verifier = secrets.token_urlsafe(32)

    # Create code_challenge using S256: BASE64URL(SHA256(code_verifier))
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    return code_verifier, code_challenge


class OAuthService:
    """
    Service for OAuth flow management.

    Handles:
    - Initiating OAuth connection flows
    - Processing OAuth callbacks and token exchange
    - Token refresh and decryption
    - Connection status management
    """

    def __init__(
        self,
        provider_repo: Optional[OAuthProviderRepository] = None,
        token_repo: Optional[OAuthTokenRepository] = None,
        encryption_service: Optional[TokenEncryptionService] = None,
        token_cache: Optional[TokenCache] = None,
        state_store: Optional[StateStore] = None,
    ):
        """
        Initialize OAuth service.

        Args:
            provider_repo: Provider repository (defaults to singleton)
            token_repo: Token repository (defaults to singleton)
            encryption_service: Token encryption service (defaults to singleton)
            token_cache: Token cache (defaults to singleton)
            state_store: State store for OAuth state (defaults to create_state_store)
        """
        self._provider_repo = provider_repo or get_provider_repository()
        self._token_repo = token_repo or get_token_repository()
        self._encryption = encryption_service or get_token_encryption_service()
        self._cache = token_cache or get_token_cache()
        self._state_store = state_store or create_state_store()

        # OAuth callback URL (configured in environment)
        self._callback_url = os.getenv("OAUTH_CALLBACK_URL", "")
        if not self._callback_url:
            logger.warning(
                "OAUTH_CALLBACK_URL not set. OAuth flows will fail. "
                "Set to e.g. https://your-app.com/oauth/callback"
            )

        # State TTL in seconds (10 minutes)
        self._state_ttl = 600

    @property
    def enabled(self) -> bool:
        """Check if OAuth service is enabled."""
        return self._provider_repo.enabled and self._token_repo.enabled

    # =========================================================================
    # Connection Flow
    # =========================================================================

    async def initiate_connect(
        self,
        provider_id: str,
        user_id: str,
        user_roles: List[str],
        frontend_redirect: Optional[str] = None,
    ) -> str:
        """
        Initiate OAuth connection flow.

        Generates authorization URL for user to visit.

        Args:
            provider_id: Provider to connect to
            user_id: User initiating connection
            user_roles: User's roles for access check
            frontend_redirect: URL to redirect after callback

        Returns:
            Authorization URL

        Raises:
            HTTPException: If provider not found or user not authorized
        """
        # Get provider
        provider = await self._provider_repo.get_provider(provider_id)
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Provider '{provider_id}' not found",
            )

        if not provider.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider '{provider_id}' is not enabled",
            )

        # Check role access
        if provider.allowed_roles and not any(
            role in provider.allowed_roles for role in user_roles
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this provider",
            )

        # Generate state and PKCE
        state = secrets.token_urlsafe(32)

        code_verifier = None
        code_challenge = None
        if provider.pkce_required:
            code_verifier, code_challenge = generate_pkce_pair()

        # Store state data
        state_data = OAuthStateData(
            provider_id=provider_id,
            user_id=user_id,
            code_verifier=code_verifier,
            redirect_uri=frontend_redirect,
        )
        self._store_state(state, state_data)

        # Build authorization URL
        params = {
            "client_id": provider.client_id,
            "redirect_uri": self._callback_url,
            "response_type": "code",
            "scope": " ".join(provider.scopes),
            "state": state,
        }

        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        # Build URL
        auth_url = f"{provider.authorization_endpoint}?"
        auth_url += "&".join(f"{k}={v}" for k, v in params.items())

        logger.info(f"Initiated OAuth flow for user {user_id}, provider {provider_id}")
        return auth_url

    async def handle_callback(
        self,
        code: str,
        state: str,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Handle OAuth callback after user authorization.

        Exchanges code for tokens and stores encrypted.

        Args:
            code: Authorization code from provider
            state: State parameter for validation

        Returns:
            Tuple of (provider_id, frontend_redirect, error)
        """
        # Validate and retrieve state data
        valid, state_data = self._get_and_delete_state(state)
        if not valid or not state_data:
            logger.warning(f"Invalid or expired OAuth state: {state[:16]}...")
            return "", None, "invalid_state"

        provider_id = state_data.provider_id
        user_id = state_data.user_id
        code_verifier = state_data.code_verifier
        frontend_redirect = state_data.redirect_uri

        try:
            # Get provider
            provider = await self._provider_repo.get_provider(provider_id)
            if not provider:
                logger.error(f"Provider not found during callback: {provider_id}")
                return provider_id, frontend_redirect, "provider_not_found"

            # Get client secret
            client_secret = await self._provider_repo.get_client_secret(provider_id)
            if not client_secret:
                logger.error(f"Client secret not found for provider: {provider_id}")
                return provider_id, frontend_redirect, "configuration_error"

            # Exchange code for tokens
            token_data = await self._exchange_code(
                provider=provider,
                client_secret=client_secret,
                code=code,
                code_verifier=code_verifier,
            )

            if not token_data:
                return provider_id, frontend_redirect, "token_exchange_failed"

            # Encrypt and store tokens
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in")
            token_type = token_data.get("token_type", "Bearer")

            if not access_token:
                logger.error("No access token in response")
                return provider_id, frontend_redirect, "no_access_token"

            # Calculate expiration
            expires_at = None
            if expires_in:
                expires_at = int(time.time()) + int(expires_in)

            # Encrypt tokens
            encrypted_access = self._encryption.encrypt(access_token)
            encrypted_refresh = None
            if refresh_token:
                encrypted_refresh = self._encryption.encrypt(refresh_token)

            # Create token record
            now = datetime.utcnow().isoformat() + "Z"
            user_token = OAuthUserToken(
                user_id=user_id,
                provider_id=provider_id,
                access_token_encrypted=encrypted_access,
                refresh_token_encrypted=encrypted_refresh,
                token_type=token_type,
                expires_at=expires_at,
                scopes_hash=provider.scopes_hash,
                status=OAuthConnectionStatus.CONNECTED,
                connected_at=now,
                updated_at=now,
            )

            await self._token_repo.save_token(user_token)

            # Cache the decrypted token
            self._cache.set(user_id, provider_id, access_token)

            logger.info(f"Successfully connected user {user_id} to provider {provider_id}")
            return provider_id, frontend_redirect, None

        except Exception as e:
            logger.error(f"Error handling OAuth callback: {e}", exc_info=True)
            return provider_id, frontend_redirect, str(e)

    async def _exchange_code(
        self,
        provider: OAuthProvider,
        client_secret: str,
        code: str,
        code_verifier: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Exchange authorization code for tokens.

        Args:
            provider: OAuth provider
            client_secret: Provider client secret
            code: Authorization code
            code_verifier: PKCE code verifier (if used)

        Returns:
            Token response dict or None on error
        """
        try:
            async with AsyncOAuth2Client(
                client_id=provider.client_id,
                client_secret=client_secret,
                token_endpoint=provider.token_endpoint,
            ) as client:
                token = await client.fetch_token(
                    url=provider.token_endpoint,
                    grant_type="authorization_code",
                    code=code,
                    redirect_uri=self._callback_url,
                    code_verifier=code_verifier,
                )
                return dict(token)

        except Exception as e:
            logger.error(f"Token exchange failed: {e}")
            return None

    # =========================================================================
    # Token Access
    # =========================================================================

    async def get_decrypted_token(
        self,
        user_id: str,
        provider_id: str,
    ) -> Optional[str]:
        """
        Get decrypted access token for a user's provider connection.

        Checks cache first, then decrypts from storage.
        Handles token refresh if needed.

        Args:
            user_id: User identifier
            provider_id: Provider identifier

        Returns:
            Decrypted access token, or None if not connected
        """
        # Check cache first
        cached = self._cache.get(user_id, provider_id)
        if cached:
            return cached

        # Get token from storage
        token = await self._token_repo.get_token(user_id, provider_id)
        if not token:
            return None

        if token.status == OAuthConnectionStatus.REVOKED:
            return None

        # Check if expired and needs refresh
        if token.is_expired:
            if token.refresh_token_encrypted:
                refreshed = await self._refresh_token(user_id, provider_id)
                if refreshed:
                    return refreshed
            # Mark as expired
            await self._token_repo.update_token_status(
                user_id, provider_id, OAuthConnectionStatus.EXPIRED
            )
            return None

        # Decrypt and cache
        try:
            access_token = self._encryption.decrypt(token.access_token_encrypted)
            self._cache.set(user_id, provider_id, access_token)
            return access_token
        except Exception as e:
            logger.error(f"Failed to decrypt token: {e}")
            return None

    async def _refresh_token(
        self,
        user_id: str,
        provider_id: str,
    ) -> Optional[str]:
        """
        Refresh an expired token.

        Args:
            user_id: User identifier
            provider_id: Provider identifier

        Returns:
            New access token, or None on failure
        """
        token = await self._token_repo.get_token(user_id, provider_id)
        if not token or not token.refresh_token_encrypted:
            return None

        provider = await self._provider_repo.get_provider(provider_id)
        if not provider:
            return None

        client_secret = await self._provider_repo.get_client_secret(provider_id)
        if not client_secret:
            return None

        try:
            # Decrypt refresh token
            refresh_token = self._encryption.decrypt(token.refresh_token_encrypted)

            # Request new tokens
            async with AsyncOAuth2Client(
                client_id=provider.client_id,
                client_secret=client_secret,
                token_endpoint=provider.token_endpoint,
            ) as client:
                new_token = await client.refresh_token(
                    url=provider.token_endpoint,
                    refresh_token=refresh_token,
                )

            if not new_token or "access_token" not in new_token:
                logger.warning(f"Token refresh failed for {user_id}/{provider_id}")
                return None

            # Update stored token
            token.access_token_encrypted = self._encryption.encrypt(
                new_token["access_token"]
            )
            if "refresh_token" in new_token:
                token.refresh_token_encrypted = self._encryption.encrypt(
                    new_token["refresh_token"]
                )
            if "expires_in" in new_token:
                token.expires_at = int(time.time()) + int(new_token["expires_in"])
            token.status = OAuthConnectionStatus.CONNECTED

            await self._token_repo.save_token(token)

            # Update cache
            self._cache.set(user_id, provider_id, new_token["access_token"])

            logger.info(f"Refreshed token for user {user_id}, provider {provider_id}")
            return new_token["access_token"]

        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return None

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def get_user_connections(
        self,
        user_id: str,
        user_roles: List[str],
    ) -> List[OAuthConnectionResponse]:
        """
        Get user's OAuth connections with status.

        Returns all available providers with connection status.

        Args:
            user_id: User identifier
            user_roles: User's roles for filtering available providers

        Returns:
            List of connection responses
        """
        # Get available providers for user's roles
        providers = await self._provider_repo.list_providers(enabled_only=True)
        available_providers = [
            p for p in providers
            if not p.allowed_roles or any(r in p.allowed_roles for r in user_roles)
        ]

        # Get user's tokens
        tokens = await self._token_repo.list_user_tokens(user_id)
        token_map = {t.provider_id: t for t in tokens}

        # Build connection responses
        connections = []
        for provider in available_providers:
            token = token_map.get(provider.provider_id)

            if token:
                # Check if needs re-auth (scope changes)
                needs_reauth = token.scopes_hash != provider.scopes_hash

                # Update status based on token state
                if token.is_expired:
                    status = OAuthConnectionStatus.EXPIRED
                elif needs_reauth:
                    status = OAuthConnectionStatus.NEEDS_REAUTH
                else:
                    status = token.status

                connections.append(
                    OAuthConnectionResponse(
                        provider_id=provider.provider_id,
                        display_name=provider.display_name,
                        provider_type=provider.provider_type,
                        icon_name=provider.icon_name,
                        status=status,
                        connected_at=token.connected_at,
                        needs_reauth=needs_reauth,
                    )
                )
            else:
                # Not connected
                connections.append(
                    OAuthConnectionResponse(
                        provider_id=provider.provider_id,
                        display_name=provider.display_name,
                        provider_type=provider.provider_type,
                        icon_name=provider.icon_name,
                        status=OAuthConnectionStatus.REVOKED,  # Use REVOKED as "not connected"
                        connected_at=None,
                        needs_reauth=False,
                    )
                )

        return connections

    async def disconnect(
        self,
        user_id: str,
        provider_id: str,
    ) -> bool:
        """
        Disconnect user from a provider.

        Revokes token if possible and deletes from storage.

        Args:
            user_id: User identifier
            provider_id: Provider identifier

        Returns:
            True if disconnected, False if not connected
        """
        # Get token
        token = await self._token_repo.get_token(user_id, provider_id)
        if not token:
            return False

        # Try to revoke token at provider
        provider = await self._provider_repo.get_provider(provider_id)
        if provider and provider.revocation_endpoint:
            try:
                access_token = self._encryption.decrypt(token.access_token_encrypted)
                await self._revoke_token(provider, access_token)
            except Exception as e:
                logger.warning(f"Failed to revoke token at provider: {e}")
                # Continue with deletion anyway

        # Delete from storage
        deleted = await self._token_repo.delete_token(user_id, provider_id)

        # Clear cache
        self._cache.delete(user_id, provider_id)

        logger.info(f"Disconnected user {user_id} from provider {provider_id}")
        return deleted

    async def _revoke_token(
        self,
        provider: OAuthProvider,
        access_token: str,
    ) -> None:
        """
        Revoke token at the provider's revocation endpoint.

        Args:
            provider: OAuth provider
            access_token: Token to revoke
        """
        if not provider.revocation_endpoint:
            return

        client_secret = await self._provider_repo.get_client_secret(provider.provider_id)
        if not client_secret:
            return

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    provider.revocation_endpoint,
                    data={
                        "token": access_token,
                        "client_id": provider.client_id,
                        "client_secret": client_secret,
                    },
                    timeout=10.0,
                )
        except Exception as e:
            logger.warning(f"Token revocation request failed: {e}")

    # =========================================================================
    # State Management (reuses OIDC state store pattern)
    # =========================================================================

    def _store_state(self, state: str, data: OAuthStateData) -> None:
        """Store OAuth state data."""
        # Convert to dict for storage
        from apis.shared.auth.state_store import OIDCStateData

        oidc_data = OIDCStateData(
            redirect_uri=data.redirect_uri,
            code_verifier=data.code_verifier,
            nonce=f"{data.provider_id}|{data.user_id}",  # Encode provider/user in nonce
        )
        self._state_store.store_state(state, oidc_data, self._state_ttl)

    def _get_and_delete_state(
        self, state: str
    ) -> Tuple[bool, Optional[OAuthStateData]]:
        """Retrieve and delete OAuth state data."""
        valid, oidc_data = self._state_store.get_and_delete_state(state)
        if not valid or not oidc_data:
            return False, None

        # Decode provider/user from nonce
        if not oidc_data.nonce or "|" not in oidc_data.nonce:
            return False, None

        provider_id, user_id = oidc_data.nonce.split("|", 1)

        return True, OAuthStateData(
            provider_id=provider_id,
            user_id=user_id,
            code_verifier=oidc_data.code_verifier,
            redirect_uri=oidc_data.redirect_uri,
        )


# Singleton instance
_oauth_service: Optional[OAuthService] = None


def get_oauth_service() -> OAuthService:
    """Get the OAuth service singleton."""
    global _oauth_service
    if _oauth_service is None:
        _oauth_service = OAuthService()
    return _oauth_service
