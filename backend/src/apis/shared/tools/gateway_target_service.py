"""AgentCore Gateway target lifecycle service.

Wraps `bedrock-agentcore-control` for managing MCP targets on the centralized
AgentCore Gateway. An admin registers an externally deployed MCP server as a
Gateway *target* (issue #419); this service is the thin AWS boundary that turns
an `MCPGatewayConfig` into a live target and reconciles update/delete.

Lives in `apis.shared` (not inference-api) — the admin route on app-api owns the
lifecycle orchestration (create AWS target first, persist the catalog row only
on success). This service is intentionally stateless apart from the boto3 client
and the lazily-resolved gateway id, so it is safe to share across requests.

Modeled on `apis.shared.oauth.agentcore_registrar.AgentCoreRegistrar`, which
wraps the same control-plane service for OAuth2 credential providers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import (
    GatewayCredentialType,
    GatewayListingMode,
    GatewayOAuthGrantType,
    MCPGatewayConfig,
)

logger = logging.getLogger(__name__)


# Model enum string values → the `bedrock-agentcore-control` API enums (which
# are uppercase). Keyed by the model's lowercase `.value` so the lookup works
# whether the field holds the enum or its value (use_enum_values=True).
_LISTING_MODE_TO_AWS: Dict[str, str] = {
    GatewayListingMode.DEFAULT.value: "DEFAULT",
    GatewayListingMode.DYNAMIC.value: "DYNAMIC",
}

_CREDENTIAL_TYPE_TO_AWS: Dict[str, str] = {
    GatewayCredentialType.GATEWAY_IAM_ROLE.value: "GATEWAY_IAM_ROLE",
    GatewayCredentialType.OAUTH.value: "OAUTH",
    GatewayCredentialType.API_KEY.value: "API_KEY",
}

_GRANT_TYPE_TO_AWS: Dict[str, str] = {
    GatewayOAuthGrantType.AUTHORIZATION_CODE.value: "AUTHORIZATION_CODE",
    GatewayOAuthGrantType.CLIENT_CREDENTIALS.value: "CLIENT_CREDENTIALS",
    GatewayOAuthGrantType.TOKEN_EXCHANGE.value: "TOKEN_EXCHANGE",
}


@dataclass(frozen=True)
class GatewayTargetInfo:
    """AgentCore Gateway record for one MCP target.

    `gateway_arn` is empty for entries surfaced by `list_targets` (the list
    summary shape does not echo it back).
    """

    target_id: str
    gateway_arn: str
    status: str
    name: str


class GatewayTargetNotFoundError(LookupError):
    """Raised when a Gateway target does not exist."""


class GatewayTargetConflictError(RuntimeError):
    """Raised when creating a target whose name already exists on the gateway."""


class GatewayTargetService:
    """Thin wrapper around `bedrock-agentcore-control` for Gateway targets."""

    def __init__(
        self,
        *,
        region: Optional[str] = None,
        gateway_id: Optional[str] = None,
        project_prefix: Optional[str] = None,
        client: Any = None,
        ssm_client: Any = None,
    ):
        self._region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
        )
        self._client = client or boto3.client(
            "bedrock-agentcore-control", region_name=self._region
        )
        # `gateway_id` may be supplied directly (tests / callers that already
        # know it); otherwise it is resolved from SSM on first use and cached.
        self._gateway_id = gateway_id
        self._ssm_client = ssm_client
        self._project_prefix = project_prefix or os.environ.get(
            "PROJECT_PREFIX", "agentcore"
        )

    # ----------------------------------------------------- gateway id (SSM)
    def _resolve_gateway_id(self) -> str:
        """Return the gateway identifier.

        Resolution order: an explicit `gateway_id` (constructor) → the
        `AGENTCORE_GATEWAY_ID` env override → the SSM parameter
        `/{prefix}/gateway/id` (read once and cached). The construct publishes
        the SSM parameter and app-api reads it at runtime, which sidesteps the
        same-stack deploy-time ordering deadlock; the env override exists for
        local dev / CI, where the SSM parameter may not be present (or the
        `PROJECT_PREFIX` may differ) — set it to the gateway identifier from
        the deployed stack's `GatewayId` output or `list-gateways`.
        """
        if self._gateway_id:
            return self._gateway_id

        env_override = os.environ.get("AGENTCORE_GATEWAY_ID")
        if env_override:
            self._gateway_id = env_override
            return env_override

        if self._ssm_client is None:
            self._ssm_client = boto3.client("ssm", region_name=self._region)

        param_name = f"/{self._project_prefix}/gateway/id"
        try:
            response = self._ssm_client.get_parameter(Name=param_name)
        except ClientError as err:
            raise RuntimeError(
                f"Gateway id SSM parameter '{param_name}' is unavailable; is the "
                f"gateway construct deployed (and PROJECT_PREFIX correct)? Set "
                f"AGENTCORE_GATEWAY_ID to override for local/CI. ({err})"
            ) from err

        self._gateway_id = response["Parameter"]["Value"]
        return self._gateway_id

    # ------------------------------------------------------------------ create
    def create_target(
        self, config: MCPGatewayConfig, *, description: str = ""
    ) -> GatewayTargetInfo:
        """Register `config` as a live target on the gateway.

        Raises:
            GatewayTargetConflictError: A target named `config.target_name`
                already exists on the gateway.
            botocore.exceptions.ClientError: Any other AWS error bubbles up so
                the route can surface a 502 and log the failure.
        """
        gateway_id = self._resolve_gateway_id()
        kwargs: Dict[str, Any] = {
            "gatewayIdentifier": gateway_id,
            "name": config.target_name,
            "description": description or f"MCP gateway target {config.target_name}",
            "targetConfiguration": self._build_target_configuration(config),
        }
        creds = self._build_credential_configs(config)
        if creds is not None:
            kwargs["credentialProviderConfigurations"] = creds
        try:
            response = self._client.create_gateway_target(**kwargs)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code in ("ConflictException", "ResourceAlreadyExistsException"):
                raise GatewayTargetConflictError(
                    f"Gateway target '{config.target_name}' already exists"
                ) from err
            raise

        return self._info_from_response(response, fallback_name=config.target_name)

    # ------------------------------------------------------------------ update
    def update_target(
        self, *, target_id: str, config: MCPGatewayConfig, description: str = ""
    ) -> GatewayTargetInfo:
        """Replace the target's full configuration.

        Like the OAuth provider update, `UpdateGatewayTarget` is not a partial
        update — name and targetConfiguration are re-submitted in full.

        Raises:
            GatewayTargetNotFoundError: No such target.
        """
        gateway_id = self._resolve_gateway_id()
        kwargs: Dict[str, Any] = {
            "gatewayIdentifier": gateway_id,
            "targetId": target_id,
            "name": config.target_name,
            "description": description or f"MCP gateway target {config.target_name}",
            "targetConfiguration": self._build_target_configuration(config),
        }
        creds = self._build_credential_configs(config)
        if creds is not None:
            kwargs["credentialProviderConfigurations"] = creds
        try:
            response = self._client.update_gateway_target(**kwargs)
        except ClientError as err:
            if self._is_not_found(err):
                raise GatewayTargetNotFoundError(target_id) from err
            raise

        return self._info_from_response(
            response, fallback_name=config.target_name, fallback_target_id=target_id
        )

    # --------------------------------------------------------------------- get
    def get_target(self, *, target_id: str) -> GatewayTargetInfo:
        """Fetch one target by id.

        Raises:
            GatewayTargetNotFoundError: No such target.
        """
        gateway_id = self._resolve_gateway_id()
        try:
            response = self._client.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
        except ClientError as err:
            if self._is_not_found(err):
                raise GatewayTargetNotFoundError(target_id) from err
            raise

        return self._info_from_response(response, fallback_target_id=target_id)

    # ------------------------------------------------------------------ delete
    def delete_target(self, *, target_id: str) -> None:
        """Delete the target. A missing target is treated as success.

        The route deletes the AWS target before the catalog row, so a 404 here
        means the row's reconciliation is already done — log loudly (this is the
        manual-repair signal in v1) and return.
        """
        gateway_id = self._resolve_gateway_id()
        try:
            self._client.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
        except ClientError as err:
            if self._is_not_found(err):
                logger.warning(
                    "Gateway target '%s' already absent on gateway '%s'; "
                    "delete is a no-op",
                    target_id,
                    gateway_id,
                )
                return
            raise

    # -------------------------------------------------------------------- list
    def list_targets(self) -> List[GatewayTargetInfo]:
        """List every target on the gateway (paginates internally)."""
        gateway_id = self._resolve_gateway_id()
        targets: List[GatewayTargetInfo] = []
        next_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"gatewayIdentifier": gateway_id}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self._client.list_gateway_targets(**kwargs)
            for item in response.get("items", []):
                targets.append(self._info_from_response(item))
            next_token = response.get("nextToken")
            if not next_token:
                break
        return targets

    # ------------------------------------------------------------- build helpers
    @staticmethod
    def _build_target_configuration(config: MCPGatewayConfig) -> Dict[str, Any]:
        """Build `targetConfiguration.mcp.mcpServer` for an external endpoint."""
        listing_value = (
            config.listing_mode
            if isinstance(config.listing_mode, str)
            else config.listing_mode.value
        )
        return {
            "mcp": {
                "mcpServer": {
                    "endpoint": config.endpoint_url,
                    "listingMode": _LISTING_MODE_TO_AWS[listing_value],
                }
            }
        }

    @staticmethod
    def _build_credential_configs(
        config: MCPGatewayConfig,
    ) -> Optional[List[Dict[str, Any]]]:
        """Build `credentialProviderConfigurations` from the credential type.

        Returns None for a public (NONE) endpoint so the caller omits the
        parameter entirely. The `MCPGatewayConfig` validator already guarantees
        the ARN / aws_service / listing invariants per credential type, so this
        only shapes the payload.
        """
        cred_value = (
            config.credential_type
            if isinstance(config.credential_type, str)
            else config.credential_type.value
        )

        if cred_value == GatewayCredentialType.NONE.value:
            return None

        aws_type = _CREDENTIAL_TYPE_TO_AWS[cred_value]

        if cred_value == GatewayCredentialType.OAUTH.value:
            grant_value = (
                config.grant_type
                if isinstance(config.grant_type, str)
                else config.grant_type.value
            )
            oauth_provider: Dict[str, Any] = {
                "providerArn": config.credential_provider_arn,
                "scopes": list(config.oauth_scopes),
                "grantType": _GRANT_TYPE_TO_AWS[grant_value],
            }
            # customParameters are part of the token-vault key — only send them
            # when set, and exactly as configured so target registration and
            # token retrieval agree.
            if config.custom_parameters:
                oauth_provider["customParameters"] = dict(config.custom_parameters)
            return [
                {
                    "credentialProviderType": aws_type,
                    "credentialProvider": {"oauthCredentialProvider": oauth_provider},
                }
            ]
        if cred_value == GatewayCredentialType.API_KEY.value:
            return [
                {
                    "credentialProviderType": aws_type,
                    "credentialProvider": {
                        "apiKeyCredentialProvider": {
                            "providerArn": config.credential_provider_arn,
                        }
                    },
                }
            ]
        # GATEWAY_IAM_ROLE — the gateway signs with its own execution role.
        # mcpServer targets require an explicit iamCredentialProvider naming the
        # AWS service to sign for (unlike OpenAPI/Lambda targets, which accept a
        # bare GATEWAY_IAM_ROLE). region is optional — AWS defaults it to the
        # gateway's region.
        iam_provider: Dict[str, Any] = {"service": config.aws_service}
        if config.aws_region:
            iam_provider["region"] = config.aws_region
        return [
            {
                "credentialProviderType": aws_type,
                "credentialProvider": {"iamCredentialProvider": iam_provider},
            }
        ]

    # ----------------------------------------------------------- parse helpers
    @staticmethod
    def _info_from_response(
        response: Dict[str, Any],
        *,
        fallback_name: str = "",
        fallback_target_id: str = "",
        fallback_gateway_arn: str = "",
    ) -> GatewayTargetInfo:
        return GatewayTargetInfo(
            target_id=response.get("targetId") or fallback_target_id,
            gateway_arn=response.get("gatewayArn") or fallback_gateway_arn,
            status=response.get("status", ""),
            name=response.get("name") or fallback_name,
        )

    @staticmethod
    def _is_not_found(err: ClientError) -> bool:
        code = err.response.get("Error", {}).get("Code")
        return code in ("ResourceNotFoundException", "NotFoundException")


_default_service: Optional[GatewayTargetService] = None


def get_gateway_target_service() -> GatewayTargetService:
    """Return the process-wide `GatewayTargetService` singleton."""
    global _default_service
    if _default_service is None:
        _default_service = GatewayTargetService()
    return _default_service
