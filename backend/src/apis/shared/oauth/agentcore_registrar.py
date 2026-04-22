"""AgentCore Identity credential-provider registrar.

Wraps `bedrock-agentcore-control` for managing OAuth2 credential providers
owned by AgentCore Identity. Callers upsert one of our `OAuthProvider`
records by first registering the client_id + client_secret here, then
storing the returned `callbackUrl` and `credentialProviderArn` on the
DynamoDB record.

Division of authority:

- AgentCore Identity: clientId, clientSecret, vendor-specific endpoint
  config, callback URL. Returns a `credentialProviderArn` that identifies
  the provider within the default token vault.
- Our DynamoDB: displayName, scopes, allowedRoles, iconName, enabled flag.

Update semantics matter: `UpdateOauth2CredentialProvider` is NOT a partial
update — the full `oauth2ProviderConfigInput` (including clientId and
clientSecret) must be re-submitted on every call. Because
`GetOauth2CredentialProvider` returns only `clientSecretArn` (not the
secret value), credential rotation always requires the admin to re-enter
both fields. `update_credential_provider` enforces this.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

from .models import OAuthProviderType

logger = logging.getLogger(__name__)


# Mapping from our OAuthProviderType to AgentCore's credentialProviderVendor
# and the corresponding vendor-specific config key inside oauth2ProviderConfigInput.
# CANVAS routes through CustomOauth2 because AgentCore Identity does not ship
# a first-class Canvas vendor.
_VENDOR_BY_TYPE: Dict[OAuthProviderType, str] = {
    OAuthProviderType.GOOGLE: "GoogleOauth2",
    OAuthProviderType.MICROSOFT: "MicrosoftOauth2",
    OAuthProviderType.GITHUB: "GithubOauth2",
    OAuthProviderType.CANVAS: "CustomOauth2",
    OAuthProviderType.CUSTOM: "CustomOauth2",
}

_CONFIG_KEY_BY_TYPE: Dict[OAuthProviderType, str] = {
    OAuthProviderType.GOOGLE: "googleOauth2ProviderConfig",
    OAuthProviderType.MICROSOFT: "microsoftOauth2ProviderConfig",
    OAuthProviderType.GITHUB: "githubOauth2ProviderConfig",
    OAuthProviderType.CANVAS: "customOauth2ProviderConfig",
    OAuthProviderType.CUSTOM: "customOauth2ProviderConfig",
}


@dataclass(frozen=True)
class CredentialProviderInfo:
    """AgentCore Identity record for one OAuth2 credential provider.

    `client_id` is populated on `get_credential_provider`; Create/Update
    responses include it in `oauth2ProviderConfigOutput` when the vendor
    echoes it back, and we surface it when present. `client_secret` is
    never returned by AgentCore — only `client_secret_arn`.
    """

    provider_id: str
    vendor: str
    credential_provider_arn: str
    client_secret_arn: str
    callback_url: str
    client_id: Optional[str] = None


class CredentialProviderNotFoundError(LookupError):
    """Raised when an AgentCore credential provider does not exist."""


class CredentialProviderConflictError(RuntimeError):
    """Raised when creating a provider that already exists in AgentCore."""


class InvalidCustomProviderConfigError(ValueError):
    """Raised when Custom vendor is selected without exactly one discovery mode."""


class AgentCoreRegistrar:
    """Thin wrapper around `bedrock-agentcore-control` for OAuth2 providers.

    Stateless apart from the boto3 client. Safe to share across requests.
    """

    def __init__(
        self,
        *,
        region: Optional[str] = None,
        client: Any = None,
    ):
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._client = client or boto3.client(
            "bedrock-agentcore-control", region_name=self._region
        )

    # ------------------------------------------------------------------ create
    def create_credential_provider(
        self,
        *,
        provider_id: str,
        provider_type: OAuthProviderType,
        client_id: str,
        client_secret: str,
        discovery_url: Optional[str] = None,
        authorization_server_metadata: Optional[Dict[str, Any]] = None,
    ) -> CredentialProviderInfo:
        """Register a new OAuth2 credential provider in AgentCore Identity.

        Raises:
            CredentialProviderConflictError: A provider with `provider_id`
                already exists.
            InvalidCustomProviderConfigError: Custom/Canvas vendor was used
                without exactly one of `discovery_url` or
                `authorization_server_metadata`.
            botocore.exceptions.ClientError: Any other AWS error bubbles up.
        """
        vendor, config_input = self._build_config_input(
            provider_type=provider_type,
            client_id=client_id,
            client_secret=client_secret,
            discovery_url=discovery_url,
            authorization_server_metadata=authorization_server_metadata,
        )

        try:
            response = self._client.create_oauth2_credential_provider(
                name=provider_id,
                credentialProviderVendor=vendor,
                oauth2ProviderConfigInput=config_input,
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code in ("ConflictException", "ResourceAlreadyExistsException"):
                raise CredentialProviderConflictError(
                    f"AgentCore credential provider '{provider_id}' already exists"
                ) from err
            raise

        return self._info_from_response(
            provider_id=provider_id, vendor=vendor, response=response
        )

    # ------------------------------------------------------------------ update
    def update_credential_provider(
        self,
        *,
        provider_id: str,
        provider_type: OAuthProviderType,
        client_id: str,
        client_secret: str,
        discovery_url: Optional[str] = None,
        authorization_server_metadata: Optional[Dict[str, Any]] = None,
    ) -> CredentialProviderInfo:
        """Replace the AgentCore provider's full config.

        `UpdateOauth2CredentialProvider` requires the full
        `oauth2ProviderConfigInput`, so the caller must supply both
        `client_id` and `client_secret`. There is no "change only the
        secret" path — Get does not return the existing secret, and the API
        does not support partial updates.

        Raises:
            CredentialProviderNotFoundError: No such provider.
            InvalidCustomProviderConfigError: Custom/Canvas without exactly
                one discovery mode.
            botocore.exceptions.ClientError: Any other AWS error.
        """
        vendor, config_input = self._build_config_input(
            provider_type=provider_type,
            client_id=client_id,
            client_secret=client_secret,
            discovery_url=discovery_url,
            authorization_server_metadata=authorization_server_metadata,
        )

        try:
            response = self._client.update_oauth2_credential_provider(
                name=provider_id,
                credentialProviderVendor=vendor,
                oauth2ProviderConfigInput=config_input,
            )
        except ClientError as err:
            if self._is_not_found(err):
                raise CredentialProviderNotFoundError(provider_id) from err
            raise

        return self._info_from_response(
            provider_id=provider_id, vendor=vendor, response=response
        )

    # --------------------------------------------------------------------- get
    def get_credential_provider(self, provider_id: str) -> CredentialProviderInfo:
        """Fetch the AgentCore record for `provider_id`.

        Raises:
            CredentialProviderNotFoundError: No such provider.
        """
        try:
            response = self._client.get_oauth2_credential_provider(name=provider_id)
        except ClientError as err:
            if self._is_not_found(err):
                raise CredentialProviderNotFoundError(provider_id) from err
            raise

        return self._info_from_response(
            provider_id=provider_id,
            vendor=response["credentialProviderVendor"],
            response=response,
        )

    # ------------------------------------------------------------------ delete
    def delete_credential_provider(self, provider_id: str) -> None:
        """Delete the AgentCore provider. Missing providers are treated as success."""
        try:
            self._client.delete_oauth2_credential_provider(name=provider_id)
        except ClientError as err:
            if self._is_not_found(err):
                logger.info(
                    "AgentCore provider '%s' already absent; delete is a no-op",
                    provider_id,
                )
                return
            raise

    # ------------------------------------------------------------- build helper
    def _build_config_input(
        self,
        *,
        provider_type: OAuthProviderType,
        client_id: str,
        client_secret: str,
        discovery_url: Optional[str],
        authorization_server_metadata: Optional[Dict[str, Any]],
    ) -> tuple[str, Dict[str, Any]]:
        """Return `(vendor, oauth2ProviderConfigInput)` for AgentCore."""
        try:
            vendor = _VENDOR_BY_TYPE[provider_type]
            config_key = _CONFIG_KEY_BY_TYPE[provider_type]
        except KeyError as err:
            raise ValueError(f"Unsupported OAuth provider type: {provider_type}") from err

        vendor_config: Dict[str, Any] = {
            "clientId": client_id,
            "clientSecret": client_secret,
        }

        if config_key == "customOauth2ProviderConfig":
            vendor_config["oauthDiscovery"] = self._build_oauth_discovery(
                discovery_url=discovery_url,
                authorization_server_metadata=authorization_server_metadata,
            )
        elif discovery_url or authorization_server_metadata:
            raise ValueError(
                f"Discovery config is only valid for CustomOauth2; "
                f"provider_type={provider_type} ignores it"
            )

        return vendor, {config_key: vendor_config}

    @staticmethod
    def _build_oauth_discovery(
        *,
        discovery_url: Optional[str],
        authorization_server_metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if bool(discovery_url) == bool(authorization_server_metadata):
            raise InvalidCustomProviderConfigError(
                "CustomOauth2 requires exactly one of discovery_url or "
                "authorization_server_metadata"
            )
        if discovery_url:
            return {"discoveryUrl": discovery_url}
        return {"authorizationServerMetadata": authorization_server_metadata}

    # ----------------------------------------------------------- parse helpers
    @staticmethod
    def _info_from_response(
        *, provider_id: str, vendor: str, response: Dict[str, Any]
    ) -> CredentialProviderInfo:
        client_secret = response.get("clientSecretArn") or {}
        output_config = response.get("oauth2ProviderConfigOutput") or {}
        # Each vendor variant nests its own output object; the clientId lives
        # one level deeper when present. We tolerate its absence.
        client_id: Optional[str] = None
        for nested in output_config.values():
            if isinstance(nested, dict) and "clientId" in nested:
                client_id = nested["clientId"]
                break

        return CredentialProviderInfo(
            provider_id=provider_id,
            vendor=vendor,
            credential_provider_arn=response["credentialProviderArn"],
            client_secret_arn=client_secret.get("secretArn", ""),
            callback_url=response.get("callbackUrl", ""),
            client_id=client_id,
        )

    @staticmethod
    def _is_not_found(err: ClientError) -> bool:
        code = err.response.get("Error", {}).get("Code")
        return code in ("ResourceNotFoundException", "NotFoundException")


_default_registrar: Optional[AgentCoreRegistrar] = None


def get_agentcore_registrar() -> AgentCoreRegistrar:
    """Return the process-wide `AgentCoreRegistrar` singleton."""
    global _default_registrar
    if _default_registrar is None:
        _default_registrar = AgentCoreRegistrar()
    return _default_registrar
