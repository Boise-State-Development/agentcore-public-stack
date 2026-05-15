"""OAuth provider models.

Providers are registered and administered through AWS Bedrock AgentCore
Identity — AgentCore owns `clientId`, `clientSecret`, endpoint config, and
the callback URL. Our DynamoDB record keeps the display metadata, scopes,
role gates, and cached pointers (ARN + callback URL) for convenience.
"""

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

# Inline-icon data URLs are persisted directly in the provider record so we
# don't have to stand up an S3 bucket / CDN just for connector icons. The
# 100KB cap (after base64 decode) keeps the DynamoDB item well under its
# 400KB limit and is generous for an icon — a tuned 64x64 PNG is < 10KB.
ICON_DATA_MAX_BYTES = 100 * 1024
_ICON_DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|gif|webp|svg\+xml);base64,([A-Za-z0-9+/=]+)$"
)


def validate_icon_data(value: Optional[str]) -> Optional[str]:
    """Validate an inline icon data URL.

    Returns the value unchanged when valid, raises `ValueError` otherwise.
    `None` is allowed (no icon set). Empty string is preserved by the caller
    as a "clear the icon" signal — handled at the repository layer.
    """
    if value is None or value == "":
        return value
    match = _ICON_DATA_URL_RE.match(value)
    if not match:
        raise ValueError(
            "icon_data must be a base64 data URL of the form "
            "data:image/<png|jpeg|gif|webp|svg+xml>;base64,<...>"
        )
    try:
        decoded = base64.b64decode(match.group(2), validate=True)
    except Exception as err:
        raise ValueError(f"icon_data base64 payload is invalid: {err}")
    if len(decoded) > ICON_DATA_MAX_BYTES:
        raise ValueError(
            f"icon_data exceeds {ICON_DATA_MAX_BYTES // 1024}KB "
            f"(got {len(decoded) // 1024}KB)"
        )
    return value


class OAuthProviderType(str, Enum):
    """Supported OAuth provider types.

    `CANVAS` routes through AgentCore's `CustomOauth2` vendor but is kept
    as a distinct type so the admin UI can surface Canvas-specific guidance
    if/when we add a preset. Today the admin form treats it as Custom.

    `SLACK`, `SALESFORCE`, and `ZOOM` are first-class AgentCore Identity
    vendors (per
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idps.html)
    — endpoints and provider-specific defaults are pre-configured by
    AgentCore, so admins only need to supply client credentials and scopes.
    """

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    GITHUB = "github"
    SLACK = "slack"
    SALESFORCE = "salesforce"
    ZOOM = "zoom"
    CANVAS = "canvas"
    CUSTOM = "custom"


def compute_scopes_hash(scopes: List[str]) -> str:
    """Return a short, order-independent hash of `scopes` for change detection."""
    sorted_scopes = sorted(scopes)
    scopes_str = ",".join(sorted_scopes)
    return hashlib.sha256(scopes_str.encode()).hexdigest()[:16]


@dataclass
class OAuthProvider:
    """OAuth provider record stored in DynamoDB.

    AgentCore-owned fields (`credential_provider_arn`, `callback_url`) are
    populated after a successful registration and kept in sync on update.
    They are cached for admin UX — the source of truth lives in AgentCore.
    """

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    scopes: List[str]
    allowed_roles: List[str]  # AppRole IDs that can use this provider
    enabled: bool = True
    icon_name: str = "heroLink"
    # Optional admin-uploaded icon as a base64 data URL. When present,
    # frontends prefer this over `icon_name`. See `validate_icon_data`
    # for the accepted shape and size cap.
    icon_data: Optional[str] = None
    credential_provider_arn: Optional[str] = None
    callback_url: Optional[str] = None
    # Custom vendor only — mirrors AgentCore's Oauth2Discovery union.
    # Exactly one of these is populated when `provider_type` is CUSTOM or
    # CANVAS; both are None for Google/Microsoft/GitHub.
    oauth_discovery_url: Optional[str] = None
    authorization_server_metadata: Optional[Dict[str, Any]] = None
    # Vendor-specific OAuth parameters merged into AgentCore Identity's
    # `customParameters` at request time. Examples: Google `hd=mycorp.com`
    # to restrict to a Workspace domain, `prompt=consent` to force the
    # consent screen. Hardcoded baselines (e.g. Google's
    # `access_type=offline`) win on conflict — admins cannot accidentally
    # turn off a documented requirement.
    custom_parameters: Optional[Dict[str, str]] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")

    @property
    def scopes_hash(self) -> str:
        return compute_scopes_hash(self.scopes)

    def to_dynamo_item(self) -> Dict[str, Any]:
        return {
            "PK": f"PROVIDER#{self.provider_id}",
            "SK": "CONFIG",
            "GSI1PK": f"ENABLED#{str(self.enabled).lower()}",
            "GSI1SK": f"PROVIDER#{self.provider_id}",
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "providerType": self.provider_type.value,
            "scopes": self.scopes,
            "scopesHash": self.scopes_hash,
            "allowedRoles": self.allowed_roles,
            "enabled": self.enabled,
            "iconName": self.icon_name,
            "iconData": self.icon_data,
            "credentialProviderArn": self.credential_provider_arn,
            "callbackUrl": self.callback_url,
            "oauthDiscoveryUrl": self.oauth_discovery_url,
            "authorizationServerMetadata": self.authorization_server_metadata,
            "customParameters": self.custom_parameters,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "OAuthProvider":
        return cls(
            provider_id=item["providerId"],
            display_name=item["displayName"],
            provider_type=OAuthProviderType(item["providerType"]),
            scopes=item.get("scopes", []),
            allowed_roles=item.get("allowedRoles", []),
            enabled=item.get("enabled", True),
            icon_name=item.get("iconName", "heroLink"),
            icon_data=item.get("iconData"),
            credential_provider_arn=item.get("credentialProviderArn"),
            callback_url=item.get("callbackUrl"),
            oauth_discovery_url=item.get("oauthDiscoveryUrl"),
            authorization_server_metadata=item.get("authorizationServerMetadata"),
            custom_parameters=item.get("customParameters"),
            created_at=item.get("createdAt", datetime.now(timezone.utc).isoformat() + "Z"),
            updated_at=item.get("updatedAt", datetime.now(timezone.utc).isoformat() + "Z"),
        )


# =============================================================================
# Pydantic request/response models
# =============================================================================


_CUSTOM_TYPES = {OAuthProviderType.CUSTOM, OAuthProviderType.CANVAS}


class OAuthProviderCreate(BaseModel):
    """Request model for creating an OAuth provider.

    `client_id` and `client_secret` are forwarded to AgentCore Identity and
    are never persisted in our DynamoDB table. For Custom/Canvas providers
    the caller must supply exactly one of `oauth_discovery_url` or
    `authorization_server_metadata`.
    """

    provider_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    display_name: str = Field(..., min_length=1, max_length=128)
    provider_type: OAuthProviderType
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    scopes: List[str] = Field(default_factory=list)
    allowed_roles: List[str] = Field(default_factory=list)
    enabled: bool = True
    icon_name: str = "heroLink"
    icon_data: Optional[str] = None
    oauth_discovery_url: Optional[str] = None
    authorization_server_metadata: Optional[Dict[str, Any]] = None
    custom_parameters: Optional[Dict[str, str]] = None

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "provider_id": "google-workspace",
            "display_name": "Google Workspace",
            "provider_type": "google",
            "client_id": "your-client-id.apps.googleusercontent.com",
            "client_secret": "your-client-secret",
            "scopes": ["openid", "email", "profile"],
            "allowed_roles": ["admin", "user"],
            "enabled": True,
            "icon_name": "heroCloud",
        }
    })

    @model_validator(mode="after")
    def _validate_discovery(self) -> "OAuthProviderCreate":
        if self.provider_type in _CUSTOM_TYPES:
            if bool(self.oauth_discovery_url) == bool(self.authorization_server_metadata):
                raise ValueError(
                    "Custom providers require exactly one of "
                    "oauth_discovery_url or authorization_server_metadata"
                )
        elif self.oauth_discovery_url or self.authorization_server_metadata:
            raise ValueError(
                f"Discovery config is only valid for custom/canvas providers; "
                f"provider_type={self.provider_type.value} does not accept it"
            )
        self.icon_data = validate_icon_data(self.icon_data)
        return self


class OAuthProviderUpdate(BaseModel):
    """Request model for updating an OAuth provider.

    Credential rotation requires both `client_id` and `client_secret`
    because AgentCore's update API demands the full config and does not
    echo back the stored secret. Partial edits to metadata (display name,
    scopes, roles, icon, enabled) are allowed without touching credentials.
    """

    display_name: Optional[str] = Field(None, min_length=1, max_length=128)
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scopes: Optional[List[str]] = None
    allowed_roles: Optional[List[str]] = None
    enabled: Optional[bool] = None
    icon_name: Optional[str] = None
    # `""` clears any uploaded icon (falls back to `icon_name`); `None`
    # leaves the existing value alone. Validated for shape and size cap.
    icon_data: Optional[str] = None
    oauth_discovery_url: Optional[str] = None
    authorization_server_metadata: Optional[Dict[str, Any]] = None
    custom_parameters: Optional[Dict[str, str]] = None

    @model_validator(mode="after")
    def _validate_credential_pair(self) -> "OAuthProviderUpdate":
        if bool(self.client_id) != bool(self.client_secret):
            raise ValueError(
                "client_id and client_secret must be provided together for rotation"
            )
        if self.oauth_discovery_url and self.authorization_server_metadata:
            raise ValueError(
                "oauth_discovery_url and authorization_server_metadata are mutually exclusive"
            )
        if self.icon_data is not None:
            self.icon_data = validate_icon_data(self.icon_data)
        return self


class OAuthProviderResponse(BaseModel):
    """Response model for an OAuth provider."""

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    scopes: List[str]
    allowed_roles: List[str]
    enabled: bool
    icon_name: str
    icon_data: Optional[str] = None
    credential_provider_arn: Optional[str] = None
    callback_url: Optional[str] = None
    oauth_discovery_url: Optional[str] = None
    authorization_server_metadata: Optional[Dict[str, Any]] = None
    custom_parameters: Optional[Dict[str, str]] = None
    created_at: str
    updated_at: str

    @classmethod
    def from_provider(cls, provider: OAuthProvider) -> "OAuthProviderResponse":
        return cls(
            provider_id=provider.provider_id,
            display_name=provider.display_name,
            provider_type=provider.provider_type,
            scopes=provider.scopes,
            allowed_roles=provider.allowed_roles,
            enabled=provider.enabled,
            icon_name=provider.icon_name,
            icon_data=provider.icon_data,
            credential_provider_arn=provider.credential_provider_arn,
            callback_url=provider.callback_url,
            oauth_discovery_url=provider.oauth_discovery_url,
            authorization_server_metadata=provider.authorization_server_metadata,
            custom_parameters=provider.custom_parameters,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )


class OAuthProviderListResponse(BaseModel):
    providers: List[OAuthProviderResponse]
    total: int


class OAuthRequiredEvent(BaseModel):
    """SSE event signalling that a tool needs user consent before it can run.

    Emitted mid-turn when `OAuthConsentHook` raises a Strands interrupt: the
    agent's tool call is paused (its in-flight state is held in
    `_interrupt_state`), the frontend receives this event, opens the
    consent popup at `authorizationUrl`, and on completion POSTs an
    interrupt response carrying `interruptId` back to `/invocations`. The
    backend resumes the same turn — no retype, no replay.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = "oauth_required"
    provider_id: str = Field(..., alias="providerId")
    authorization_url: str = Field(..., alias="authorizationUrl")
    interrupt_id: str = Field(..., alias="interruptId")

    def to_sse_format(self) -> str:
        import json
        return (
            f"event: oauth_required\n"
            f"data: {json.dumps(self.model_dump(by_alias=True, exclude_none=True))}\n\n"
        )
