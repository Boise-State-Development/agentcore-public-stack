"""Models for OIDC authentication provider configuration."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


@dataclass
class AuthProvider:
    """OIDC authentication provider configuration stored in DynamoDB."""

    provider_id: str
    display_name: str
    provider_type: str  # "oidc" (extensible to "saml" later)
    enabled: bool
    issuer_url: str
    client_id: str
    # Discovered/overridable endpoints
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    # OAuth config
    scopes: str = "openid profile email"
    response_type: str = "code"
    pkce_enabled: bool = True
    redirect_uri: Optional[str] = None
    # Claim mappings
    user_id_claim: str = "sub"
    email_claim: str = "email"
    name_claim: str = "name"
    roles_claim: str = "roles"
    picture_claim: Optional[str] = "picture"
    first_name_claim: Optional[str] = "given_name"
    last_name_claim: Optional[str] = "family_name"
    # Validation rules
    user_id_pattern: Optional[str] = None
    required_scopes: Optional[List[str]] = None
    allowed_audiences: Optional[List[str]] = None
    # Appearance
    logo_url: Optional[str] = None
    button_color: Optional[str] = None
    # Metadata
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")
    created_by: Optional[str] = None
    # Cognito federated identity provider name
    cognito_provider_name: Optional[str] = None
    # AgentCore Runtime tracking
    agentcore_runtime_arn: Optional[str] = None
    agentcore_runtime_id: Optional[str] = None
    agentcore_runtime_endpoint_url: Optional[str] = None
    agentcore_runtime_status: str = "PENDING"
    agentcore_runtime_error: Optional[str] = None

    def to_dynamo_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format."""
        item: Dict[str, Any] = {
            "PK": f"AUTH_PROVIDER#{self.provider_id}",
            "SK": f"AUTH_PROVIDER#{self.provider_id}",
            # GSI for enabled providers
            "GSI1PK": f"ENABLED#{str(self.enabled).lower()}",
            "GSI1SK": f"AUTH_PROVIDER#{self.provider_id}",
            # Main attributes
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "providerType": self.provider_type,
            "enabled": self.enabled,
            "issuerUrl": self.issuer_url,
            "clientId": self.client_id,
            "scopes": self.scopes,
            "responseType": self.response_type,
            "pkceEnabled": self.pkce_enabled,
            # Claim mappings
            "userIdClaim": self.user_id_claim,
            "emailClaim": self.email_claim,
            "nameClaim": self.name_claim,
            "rolesClaim": self.roles_claim,
            # Metadata
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

        # Optional endpoint fields
        if self.authorization_endpoint:
            item["authorizationEndpoint"] = self.authorization_endpoint
        if self.token_endpoint:
            item["tokenEndpoint"] = self.token_endpoint
        if self.jwks_uri:
            item["jwksUri"] = self.jwks_uri
        if self.userinfo_endpoint:
            item["userinfoEndpoint"] = self.userinfo_endpoint
        if self.end_session_endpoint:
            item["endSessionEndpoint"] = self.end_session_endpoint
        if self.redirect_uri:
            item["redirectUri"] = self.redirect_uri

        # Optional claim fields
        if self.picture_claim:
            item["pictureClaim"] = self.picture_claim
        if self.first_name_claim:
            item["firstNameClaim"] = self.first_name_claim
        if self.last_name_claim:
            item["lastNameClaim"] = self.last_name_claim

        # Optional validation fields
        if self.user_id_pattern:
            item["userIdPattern"] = self.user_id_pattern
        if self.required_scopes:
            item["requiredScopes"] = self.required_scopes
        if self.allowed_audiences:
            item["allowedAudiences"] = self.allowed_audiences

        # Optional appearance fields
        if self.logo_url:
            item["logoUrl"] = self.logo_url
        if self.button_color:
            item["buttonColor"] = self.button_color
        if self.created_by:
            item["createdBy"] = self.created_by

        # Cognito federated identity provider name
        if self.cognito_provider_name:
            item["cognitoProviderName"] = self.cognito_provider_name

        # AgentCore Runtime tracking fields
        if self.agentcore_runtime_arn:
            item["agentcoreRuntimeArn"] = self.agentcore_runtime_arn
        if self.agentcore_runtime_id:
            item["agentcoreRuntimeId"] = self.agentcore_runtime_id
        if self.agentcore_runtime_endpoint_url:
            item["agentcoreRuntimeEndpointUrl"] = self.agentcore_runtime_endpoint_url
        item["agentcoreRuntimeStatus"] = self.agentcore_runtime_status
        if self.agentcore_runtime_error:
            item["agentcoreRuntimeError"] = self.agentcore_runtime_error

        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "AuthProvider":
        """Create from DynamoDB item."""
        return cls(
            provider_id=item["providerId"],
            display_name=item["displayName"],
            provider_type=item.get("providerType", "oidc"),
            enabled=item.get("enabled", True),
            issuer_url=item["issuerUrl"],
            client_id=item["clientId"],
            authorization_endpoint=item.get("authorizationEndpoint"),
            token_endpoint=item.get("tokenEndpoint"),
            jwks_uri=item.get("jwksUri"),
            userinfo_endpoint=item.get("userinfoEndpoint"),
            end_session_endpoint=item.get("endSessionEndpoint"),
            scopes=item.get("scopes", "openid profile email"),
            response_type=item.get("responseType", "code"),
            pkce_enabled=item.get("pkceEnabled", True),
            redirect_uri=item.get("redirectUri"),
            user_id_claim=item.get("userIdClaim", "sub"),
            email_claim=item.get("emailClaim", "email"),
            name_claim=item.get("nameClaim", "name"),
            roles_claim=item.get("rolesClaim", "roles"),
            picture_claim=item.get("pictureClaim", "picture"),
            first_name_claim=item.get("firstNameClaim", "given_name"),
            last_name_claim=item.get("lastNameClaim", "family_name"),
            user_id_pattern=item.get("userIdPattern"),
            required_scopes=item.get("requiredScopes"),
            allowed_audiences=item.get("allowedAudiences"),
            logo_url=item.get("logoUrl"),
            button_color=item.get("buttonColor"),
            created_at=item.get("createdAt", datetime.now(timezone.utc).isoformat() + "Z"),
            updated_at=item.get("updatedAt", datetime.now(timezone.utc).isoformat() + "Z"),
            created_by=item.get("createdBy"),
            cognito_provider_name=item.get("cognitoProviderName"),
            agentcore_runtime_arn=item.get("agentcoreRuntimeArn"),
            agentcore_runtime_id=item.get("agentcoreRuntimeId"),
            agentcore_runtime_endpoint_url=item.get("agentcoreRuntimeEndpointUrl"),
            agentcore_runtime_status=item.get("agentcoreRuntimeStatus", "PENDING"),
            agentcore_runtime_error=item.get("agentcoreRuntimeError"),
        )


# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


class AuthProviderCreate(BaseModel):
    """Request model for creating an OIDC authentication provider."""

    provider_id: str = Field(
        ..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Unique provider slug (e.g., 'entra-id', 'okta-prod')"
    )
    display_name: str = Field(..., min_length=1, max_length=128)
    provider_type: str = Field(default="oidc", pattern=r"^(oidc)$")
    enabled: bool = True
    issuer_url: str = Field(
        ..., min_length=1,
        description="OIDC issuer URL (e.g., 'https://login.microsoftonline.com/{tenant}/v2.0')"
    )
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1, description="Will be stored in Secrets Manager")
    # Endpoints (auto-discovered if not provided)
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    # OAuth config
    scopes: str = Field(default="openid profile email", description="Space-separated scopes")
    response_type: str = "code"
    pkce_enabled: bool = True
    redirect_uri: Optional[str] = None
    # Claim mappings
    user_id_claim: str = Field(default="sub", description="JWT claim for user ID")
    email_claim: str = Field(default="email", description="JWT claim for email")
    name_claim: str = Field(default="name", description="JWT claim for display name")
    roles_claim: str = Field(default="roles", description="JWT claim for roles array")
    picture_claim: Optional[str] = "picture"
    first_name_claim: Optional[str] = "given_name"
    last_name_claim: Optional[str] = "family_name"
    # Validation rules
    user_id_pattern: Optional[str] = Field(
        None, description="Regex pattern for user ID validation (e.g., '^\\d{9}$')"
    )
    required_scopes: Optional[List[str]] = None
    allowed_audiences: Optional[List[str]] = None
    # Discovery
    auto_discover: bool = Field(
        default=False,
        description="When True, fetch .well-known/openid-configuration from issuer URL to auto-populate missing endpoints",
    )
    # Appearance
    logo_url: Optional[str] = None
    button_color: Optional[str] = Field(
        None, pattern=r"^#[0-9a-fA-F]{6}$", description="Hex color for login button"
    )


class AuthProviderUpdate(BaseModel):
    """Request model for updating an OIDC authentication provider. All fields optional."""

    display_name: Optional[str] = Field(None, min_length=1, max_length=128)
    enabled: Optional[bool] = None
    issuer_url: Optional[str] = Field(None, min_length=1)
    client_id: Optional[str] = Field(None, min_length=1)
    client_secret: Optional[str] = Field(None, min_length=1, description="Only if rotating secret")
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    scopes: Optional[str] = None
    response_type: Optional[str] = None
    pkce_enabled: Optional[bool] = None
    redirect_uri: Optional[str] = None
    user_id_claim: Optional[str] = None
    email_claim: Optional[str] = None
    name_claim: Optional[str] = None
    roles_claim: Optional[str] = None
    picture_claim: Optional[str] = None
    first_name_claim: Optional[str] = None
    last_name_claim: Optional[str] = None
    user_id_pattern: Optional[str] = None
    required_scopes: Optional[List[str]] = None
    allowed_audiences: Optional[List[str]] = None
    logo_url: Optional[str] = None
    button_color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


class AuthProviderResponse(BaseModel):
    """Response model for an auth provider (excludes client secret)."""

    provider_id: str
    display_name: str
    provider_type: str
    enabled: bool
    issuer_url: str
    client_id: str
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    scopes: str
    response_type: str
    pkce_enabled: bool
    redirect_uri: Optional[str] = None
    user_id_claim: str
    email_claim: str
    name_claim: str
    roles_claim: str
    picture_claim: Optional[str] = None
    first_name_claim: Optional[str] = None
    last_name_claim: Optional[str] = None
    user_id_pattern: Optional[str] = None
    required_scopes: Optional[List[str]] = None
    allowed_audiences: Optional[List[str]] = None
    logo_url: Optional[str] = None
    button_color: Optional[str] = None
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    cognito_provider_name: Optional[str] = None
    agentcore_runtime_arn: Optional[str] = None
    agentcore_runtime_id: Optional[str] = None
    agentcore_runtime_endpoint_url: Optional[str] = None
    agentcore_runtime_status: str = "PENDING"
    agentcore_runtime_error: Optional[str] = None

    @classmethod
    def from_provider(cls, provider: AuthProvider) -> "AuthProviderResponse":
        """Create response from AuthProvider dataclass."""
        return cls(
            provider_id=provider.provider_id,
            display_name=provider.display_name,
            provider_type=provider.provider_type,
            enabled=provider.enabled,
            issuer_url=provider.issuer_url,
            client_id=provider.client_id,
            authorization_endpoint=provider.authorization_endpoint,
            token_endpoint=provider.token_endpoint,
            jwks_uri=provider.jwks_uri,
            userinfo_endpoint=provider.userinfo_endpoint,
            end_session_endpoint=provider.end_session_endpoint,
            scopes=provider.scopes,
            response_type=provider.response_type,
            pkce_enabled=provider.pkce_enabled,
            redirect_uri=provider.redirect_uri,
            user_id_claim=provider.user_id_claim,
            email_claim=provider.email_claim,
            name_claim=provider.name_claim,
            roles_claim=provider.roles_claim,
            picture_claim=provider.picture_claim,
            first_name_claim=provider.first_name_claim,
            last_name_claim=provider.last_name_claim,
            user_id_pattern=provider.user_id_pattern,
            required_scopes=provider.required_scopes,
            allowed_audiences=provider.allowed_audiences,
            logo_url=provider.logo_url,
            button_color=provider.button_color,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
            created_by=provider.created_by,
            cognito_provider_name=provider.cognito_provider_name,
            agentcore_runtime_arn=provider.agentcore_runtime_arn,
            agentcore_runtime_id=provider.agentcore_runtime_id,
            agentcore_runtime_endpoint_url=provider.agentcore_runtime_endpoint_url,
            agentcore_runtime_status=provider.agentcore_runtime_status,
            agentcore_runtime_error=provider.agentcore_runtime_error,
        )


class AuthProviderListResponse(BaseModel):
    """Response model for listing auth providers."""

    providers: List[AuthProviderResponse]
    total: int


class AuthProviderPublicInfo(BaseModel):
    """Minimal provider info for the login page (no auth required)."""

    provider_id: str
    display_name: str
    logo_url: Optional[str] = None
    button_color: Optional[str] = None


class AuthProviderPublicListResponse(BaseModel):
    """Response for the public providers endpoint used by the login page."""

    providers: List[AuthProviderPublicInfo]


class OIDCDiscoveryRequest(BaseModel):
    """Request model for OIDC endpoint discovery."""

    issuer_url: str = Field(
        ..., min_length=1,
        description="OIDC issuer URL to discover endpoints from"
    )


class OIDCDiscoveryResponse(BaseModel):
    """Response model with discovered OIDC endpoints."""

    issuer: str
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    scopes_supported: Optional[List[str]] = None
    response_types_supported: Optional[List[str]] = None
    claims_supported: Optional[List[str]] = None
