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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from .gateway_identity import resolve_gateway_id
from .gateway_lambda_grant import (
    grant_gateway_invoke,
    is_lambda_function_url,
    revoke_gateway_invoke,
)
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
    # Human-readable explanations from the gateway when a target is unhealthy
    # (e.g. ["Failed to connect and fetch tools from the provided MCP target
    # server. Error - Authorization error when sending message"]). Empty for a
    # healthy target. Surfaced to admins so a FAILED sync isn't invisible.
    status_reasons: List[str] = field(default_factory=list)


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
        lambda_client: Any = None,
    ):
        self._region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
        )
        self._client = client or boto3.client(
            "bedrock-agentcore-control", region_name=self._region
        )
        # Lazily-created `lambda` client for the per-target invoke grant; may be
        # injected for tests.
        self._lambda_client = lambda_client
        # `gateway_id` may be supplied directly (tests / callers that already
        # know it); otherwise it is resolved from SSM on first use and cached.
        self._gateway_id = gateway_id
        # Gateway execution role ARN, resolved from GetGateway on first use and
        # cached — the principal granted InvokeFunctionUrl on IAM Lambda targets.
        self._gateway_role_arn: Optional[str] = None
        self._ssm_client = ssm_client
        self._project_prefix = project_prefix or os.environ.get(
            "PROJECT_PREFIX", "agentcore"
        )

    # ----------------------------------------------------- gateway id (SSM)
    def _resolve_gateway_id(self) -> str:
        """Return the gateway identifier (resolved once and cached).

        Delegates to the shared `resolve_gateway_id` so the admin-side service
        and the agent-side gateway client resolve the SAME gateway — the env
        override → SSM `/{prefix}/gateway/id` order lives in one place.
        """
        if self._gateway_id:
            return self._gateway_id

        if self._ssm_client is None:
            self._ssm_client = boto3.client("ssm", region_name=self._region)

        self._gateway_id = resolve_gateway_id(
            project_prefix=self._project_prefix,
            region=self._region,
            ssm_client=self._ssm_client,
        )
        return self._gateway_id

    # ----------------------------------------------- gateway role + Lambda grant
    def _resolve_gateway_role_arn(self) -> str:
        """Return the gateway execution role ARN (GetGateway, cached)."""
        if self._gateway_role_arn:
            return self._gateway_role_arn
        gateway_id = self._resolve_gateway_id()
        response = self._client.get_gateway(gatewayIdentifier=gateway_id)
        role_arn = response.get("roleArn")
        if not role_arn:
            raise RuntimeError(
                f"Gateway '{gateway_id}' has no roleArn; cannot authorize Lambda "
                "targets for the gateway execution role."
            )
        self._gateway_role_arn = role_arn
        return role_arn

    @staticmethod
    def _needs_lambda_grant(config: MCPGatewayConfig) -> bool:
        """True for a GATEWAY_IAM_ROLE target on a Lambda Function URL with a
        named function — the only case the per-target grant applies to."""
        cred = (
            config.credential_type
            if isinstance(config.credential_type, str)
            else config.credential_type.value
        )
        return (
            cred == GatewayCredentialType.GATEWAY_IAM_ROLE.value
            and is_lambda_function_url(config.endpoint_url)
            and bool(config.lambda_function_name)
        )

    def _grant_lambda_if_needed(self, config: MCPGatewayConfig) -> None:
        """Authorize the gateway role to invoke an IAM Lambda-URL target.

        Called *before* Create/UpdateGatewayTarget so the permission exists
        before the gateway's async connect (no race). No-op for non-Lambda /
        non-IAM targets. Raises GatewayLambdaGrantError (→ 400 at the route) for
        a cross-account or unresolvable function.
        """
        if not self._needs_lambda_grant(config):
            return
        if self._lambda_client is None:
            self._lambda_client = boto3.client("lambda", region_name=self._region)
        grant_gateway_invoke(
            function_name=config.lambda_function_name,
            endpoint_url=config.endpoint_url,
            gateway_role_arn=self._resolve_gateway_role_arn(),
            statement_seed=config.target_name,
            region=self._region,
            lambda_client=self._lambda_client,
        )

    def _revoke_lambda_if_needed(self, config: Optional[MCPGatewayConfig]) -> None:
        """Best-effort removal of a target's gateway-role grant (idempotent)."""
        if (
            config is None
            or not config.lambda_function_name
            or not is_lambda_function_url(config.endpoint_url)
        ):
            return
        if self._lambda_client is None:
            self._lambda_client = boto3.client("lambda", region_name=self._region)
        revoke_gateway_invoke(
            function_name=config.lambda_function_name,
            statement_seed=config.target_name,
            region=self._region,
            lambda_client=self._lambda_client,
        )

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
        # Authorize the gateway role to invoke the target BEFORE creating it, so
        # the gateway's async connect+list never races a missing permission.
        # A cross-account / unresolvable function raises here, before any target
        # is created (no orphan).
        self._grant_lambda_if_needed(config)
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
        # Re-grant before the update re-validates the target (idempotent).
        self._grant_lambda_if_needed(config)
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
    def delete_target(
        self, *, target_id: str, config: Optional[MCPGatewayConfig] = None
    ) -> None:
        """Delete the target. A missing target is treated as success.

        The route deletes the AWS target before the catalog row, so a 404 here
        means the row's reconciliation is already done — log loudly (this is the
        manual-repair signal in v1) and return.

        When `config` is supplied (the stored gateway config), the per-target
        Lambda invoke grant is revoked too, so the gateway role's authorization
        on that function is torn down with the target it was created for.
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
            else:
                raise
        # Revoke the gateway-role grant regardless of whether the target existed
        # (idempotent) so we never leave a dangling resource-policy statement.
        self._revoke_lambda_if_needed(config)

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
            # `statusReasons` is present on get/update responses for an
            # unhealthy target; the list-summary shape may omit it.
            status_reasons=list(response.get("statusReasons") or []),
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
