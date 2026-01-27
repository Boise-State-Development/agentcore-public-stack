"""OAuth models for provider configuration and user tokens."""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class OAuthProviderType(str, Enum):
    """Supported OAuth provider types."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    GITHUB = "github"
    CANVAS = "canvas"
    CUSTOM = "custom"


class OAuthConnectionStatus(str, Enum):
    """Connection status for user OAuth tokens."""

    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    NEEDS_REAUTH = "needs_reauth"


def compute_scopes_hash(scopes: List[str]) -> str:
    """
    Compute a hash of the scopes list for change detection.

    Used to detect when provider scopes change and user needs to re-authenticate.

    Args:
        scopes: List of OAuth scopes

    Returns:
        SHA-256 hash of sorted scopes
    """
    sorted_scopes = sorted(scopes)
    scopes_str = ",".join(sorted_scopes)
    return hashlib.sha256(scopes_str.encode()).hexdigest()[:16]


@dataclass
class OAuthProvider:
    """OAuth provider configuration stored in DynamoDB."""

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    scopes: List[str]
    allowed_roles: List[str]  # AppRole IDs that can use this provider
    enabled: bool = True
    icon_name: str = "heroLink"  # Default icon
    userinfo_endpoint: Optional[str] = None  # Optional userinfo endpoint
    revocation_endpoint: Optional[str] = None  # Optional token revocation endpoint
    pkce_required: bool = True  # PKCE is required by default for security
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    @property
    def scopes_hash(self) -> str:
        """Get the scopes hash for this provider."""
        return compute_scopes_hash(self.scopes)

    def to_dynamo_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format."""
        return {
            "PK": f"PROVIDER#{self.provider_id}",
            "SK": "CONFIG",
            # GSI for enabled providers
            "GSI1PK": f"ENABLED#{str(self.enabled).lower()}",
            "GSI1SK": f"PROVIDER#{self.provider_id}",
            # Main attributes
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "providerType": self.provider_type.value,
            "authorizationEndpoint": self.authorization_endpoint,
            "tokenEndpoint": self.token_endpoint,
            "clientId": self.client_id,
            "scopes": self.scopes,
            "scopesHash": self.scopes_hash,
            "allowedRoles": self.allowed_roles,
            "enabled": self.enabled,
            "iconName": self.icon_name,
            "userinfoEndpoint": self.userinfo_endpoint,
            "revocationEndpoint": self.revocation_endpoint,
            "pkceRequired": self.pkce_required,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "OAuthProvider":
        """Create from DynamoDB item."""
        return cls(
            provider_id=item["providerId"],
            display_name=item["displayName"],
            provider_type=OAuthProviderType(item["providerType"]),
            authorization_endpoint=item["authorizationEndpoint"],
            token_endpoint=item["tokenEndpoint"],
            client_id=item["clientId"],
            scopes=item.get("scopes", []),
            allowed_roles=item.get("allowedRoles", []),
            enabled=item.get("enabled", True),
            icon_name=item.get("iconName", "heroLink"),
            userinfo_endpoint=item.get("userinfoEndpoint"),
            revocation_endpoint=item.get("revocationEndpoint"),
            pkce_required=item.get("pkceRequired", True),
            created_at=item.get("createdAt", datetime.utcnow().isoformat() + "Z"),
            updated_at=item.get("updatedAt", datetime.utcnow().isoformat() + "Z"),
        )


@dataclass
class OAuthUserToken:
    """User's OAuth token stored in DynamoDB (encrypted)."""

    user_id: str
    provider_id: str
    access_token_encrypted: str  # KMS-encrypted access token
    refresh_token_encrypted: Optional[str] = None  # KMS-encrypted refresh token
    token_type: str = "Bearer"
    expires_at: Optional[int] = None  # Unix timestamp
    scopes_hash: str = ""  # Hash of scopes at time of authorization
    status: OAuthConnectionStatus = OAuthConnectionStatus.CONNECTED
    connected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    @property
    def is_expired(self) -> bool:
        """Check if token has expired."""
        if not self.expires_at:
            return False
        import time

        return time.time() > self.expires_at

    def to_dynamo_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format."""
        item = {
            "PK": f"USER#{self.user_id}",
            "SK": f"PROVIDER#{self.provider_id}",
            # GSI for listing users by provider
            "GSI1PK": f"PROVIDER#{self.provider_id}",
            "GSI1SK": f"USER#{self.user_id}",
            # Main attributes
            "userId": self.user_id,
            "providerId": self.provider_id,
            "accessTokenEncrypted": self.access_token_encrypted,
            "tokenType": self.token_type,
            "scopesHash": self.scopes_hash,
            "status": self.status.value,
            "connectedAt": self.connected_at,
            "updatedAt": self.updated_at,
        }

        if self.refresh_token_encrypted:
            item["refreshTokenEncrypted"] = self.refresh_token_encrypted

        if self.expires_at:
            item["expiresAt"] = self.expires_at

        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "OAuthUserToken":
        """Create from DynamoDB item."""
        return cls(
            user_id=item["userId"],
            provider_id=item["providerId"],
            access_token_encrypted=item["accessTokenEncrypted"],
            refresh_token_encrypted=item.get("refreshTokenEncrypted"),
            token_type=item.get("tokenType", "Bearer"),
            expires_at=item.get("expiresAt"),
            scopes_hash=item.get("scopesHash", ""),
            status=OAuthConnectionStatus(item.get("status", "connected")),
            connected_at=item.get("connectedAt", datetime.utcnow().isoformat() + "Z"),
            updated_at=item.get("updatedAt", datetime.utcnow().isoformat() + "Z"),
        )


# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


class OAuthProviderCreate(BaseModel):
    """Request model for creating an OAuth provider."""

    provider_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    display_name: str = Field(..., min_length=1, max_length=128)
    provider_type: OAuthProviderType
    authorization_endpoint: str = Field(..., min_length=1)
    token_endpoint: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)  # Will be stored in Secrets Manager
    scopes: List[str] = Field(default_factory=list)
    allowed_roles: List[str] = Field(default_factory=list)
    enabled: bool = True
    icon_name: str = "heroLink"
    userinfo_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    pkce_required: bool = True

    class Config:
        json_schema_extra = {
            "example": {
                "provider_id": "google-workspace",
                "display_name": "Google Workspace",
                "provider_type": "google",
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "client_id": "your-client-id.apps.googleusercontent.com",
                "client_secret": "your-client-secret",
                "scopes": ["openid", "email", "profile", "https://www.googleapis.com/auth/drive.readonly"],
                "allowed_roles": ["admin", "user"],
                "enabled": True,
                "icon_name": "heroCloud",
                "pkce_required": True,
            }
        }


class OAuthProviderUpdate(BaseModel):
    """Request model for updating an OAuth provider."""

    display_name: Optional[str] = Field(None, min_length=1, max_length=128)
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # Only if rotating secret
    scopes: Optional[List[str]] = None
    allowed_roles: Optional[List[str]] = None
    enabled: Optional[bool] = None
    icon_name: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    pkce_required: Optional[bool] = None


class OAuthProviderResponse(BaseModel):
    """Response model for an OAuth provider (excludes secrets)."""

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    scopes: List[str]
    allowed_roles: List[str]
    enabled: bool
    icon_name: str
    userinfo_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    pkce_required: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_provider(cls, provider: OAuthProvider) -> "OAuthProviderResponse":
        """Create from OAuthProvider dataclass."""
        return cls(
            provider_id=provider.provider_id,
            display_name=provider.display_name,
            provider_type=provider.provider_type,
            authorization_endpoint=provider.authorization_endpoint,
            token_endpoint=provider.token_endpoint,
            client_id=provider.client_id,
            scopes=provider.scopes,
            allowed_roles=provider.allowed_roles,
            enabled=provider.enabled,
            icon_name=provider.icon_name,
            userinfo_endpoint=provider.userinfo_endpoint,
            revocation_endpoint=provider.revocation_endpoint,
            pkce_required=provider.pkce_required,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )


class OAuthProviderListResponse(BaseModel):
    """Response model for listing OAuth providers."""

    providers: List[OAuthProviderResponse]
    total: int


class OAuthConnectionResponse(BaseModel):
    """Response model for a user's OAuth connection."""

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    icon_name: str
    status: OAuthConnectionStatus
    connected_at: Optional[str] = None
    needs_reauth: bool = False


class OAuthConnectionListResponse(BaseModel):
    """Response model for listing user's OAuth connections."""

    connections: List[OAuthConnectionResponse]


class OAuthConnectResponse(BaseModel):
    """Response model for initiating OAuth connection."""

    authorization_url: str


class OAuthCallbackResult(BaseModel):
    """Internal model for OAuth callback result."""

    success: bool
    provider_id: Optional[str] = None
    error: Optional[str] = None
    error_description: Optional[str] = None
