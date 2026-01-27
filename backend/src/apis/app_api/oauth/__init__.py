"""OAuth provider management module.

This module provides OAuth connection management for third-party integrations.
Admins can configure OAuth providers (Google, Microsoft, Canvas, etc.) and
users can connect their accounts for MCP tool requests.
"""

from .models import (
    OAuthProviderType,
    OAuthConnectionStatus,
    OAuthProvider,
    OAuthUserToken,
    OAuthProviderCreate,
    OAuthProviderUpdate,
    OAuthProviderResponse,
    OAuthProviderListResponse,
    OAuthConnectionResponse,
    OAuthConnectionListResponse,
    OAuthConnectResponse,
)
from .encryption import TokenEncryptionService, get_token_encryption_service
from .token_cache import TokenCache, get_token_cache
from .provider_repository import OAuthProviderRepository, get_provider_repository
from .token_repository import OAuthTokenRepository, get_token_repository
from .service import OAuthService, get_oauth_service

__all__ = [
    # Enums
    "OAuthProviderType",
    "OAuthConnectionStatus",
    # Models
    "OAuthProvider",
    "OAuthUserToken",
    "OAuthProviderCreate",
    "OAuthProviderUpdate",
    "OAuthProviderResponse",
    "OAuthProviderListResponse",
    "OAuthConnectionResponse",
    "OAuthConnectionListResponse",
    "OAuthConnectResponse",
    # Services
    "TokenEncryptionService",
    "get_token_encryption_service",
    "TokenCache",
    "get_token_cache",
    "OAuthProviderRepository",
    "get_provider_repository",
    "OAuthTokenRepository",
    "get_token_repository",
    "OAuthService",
    "get_oauth_service",
]
