"""GatewayTargetService tests (issue #419, Phase 2).

Mocks the `bedrock-agentcore-control` boto3 client directly — these verify our
translation layer (MCPGatewayConfig → CreateGatewayTarget kwargs, listing-mode
and credential-type mapping, 404 → no-op delete, conflict mapping, SSM gateway-id
resolution), not AWS behaviour. Mirrors test_oauth_agentcore_registrar.py.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.shared.tools.gateway_target_service import (
    GatewayTargetConflictError,
    GatewayTargetNotFoundError,
    GatewayTargetService,
)
from apis.shared.tools.models import (
    GatewayCredentialType,
    GatewayListingMode,
    GatewayOAuthGrantType,
    MCPGatewayConfig,
    MCPToolEntry,
)


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": code}},
        operation_name="op",
    )


def _create_response(**overrides):
    base = {
        "targetId": "TGT1",
        "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:111:gateway/gw-test-id",
        "status": "CREATING",
        "name": "weather-target",
    }
    base.update(overrides)
    return base


def _iam_config(**overrides) -> MCPGatewayConfig:
    base = dict(
        target_name="weather-target",
        endpoint_url="https://example.com/mcp",
        listing_mode=GatewayListingMode.DEFAULT,
        credential_type=GatewayCredentialType.GATEWAY_IAM_ROLE,
        aws_service="lambda",
        tools=[MCPToolEntry(name="get_forecast")],
    )
    base.update(overrides)
    return MCPGatewayConfig(**base)


@pytest.fixture
def boto_client():
    return MagicMock()


@pytest.fixture
def ssm_client():
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "gw-test-id"}}
    return ssm


@pytest.fixture
def service(boto_client, ssm_client):
    return GatewayTargetService(
        client=boto_client,
        ssm_client=ssm_client,
        region="us-west-2",
        project_prefix="myproj",
    )


class TestCreateTarget:
    def test_iam_target_kwargs(self, service, boto_client, ssm_client):
        boto_client.create_gateway_target.return_value = _create_response()

        info = service.create_target(_iam_config())

        kwargs = boto_client.create_gateway_target.call_args.kwargs
        assert kwargs["gatewayIdentifier"] == "gw-test-id"
        assert kwargs["name"] == "weather-target"
        assert kwargs["targetConfiguration"] == {
            "mcp": {
                "mcpServer": {
                    "endpoint": "https://example.com/mcp",
                    "listingMode": "DEFAULT",
                }
            }
        }
        assert kwargs["credentialProviderConfigurations"] == [
            {
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {"iamCredentialProvider": {"service": "lambda"}},
            }
        ]
        # gateway id was read from /{prefix}/gateway/id
        ssm_client.get_parameter.assert_called_once_with(Name="/myproj/gateway/id")
        # response is surfaced
        assert info.target_id == "TGT1"
        assert info.gateway_arn.endswith("gateway/gw-test-id")
        assert info.status == "CREATING"

    def test_oauth_credential_config(self, service, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        config = MCPGatewayConfig(
            target_name="github-target",
            endpoint_url="https://gh/mcp",
            listing_mode=GatewayListingMode.DEFAULT,
            credential_type=GatewayCredentialType.OAUTH,
            credential_provider_arn="arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default/oauth2credentialprovider/gh",
            oauth_scopes=["repo", "read:user"],
        )

        service.create_target(config)

        creds = boto_client.create_gateway_target.call_args.kwargs[
            "credentialProviderConfigurations"
        ]
        assert creds == [
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": config.credential_provider_arn,
                        "scopes": ["repo", "read:user"],
                        # Defaults to the 3LO authorization-code grant.
                        "grantType": "AUTHORIZATION_CODE",
                    }
                },
            }
        ]

    def test_oauth_client_credentials_with_custom_parameters(self, service, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        config = MCPGatewayConfig(
            target_name="m2m-target",
            endpoint_url="https://m2m/mcp",
            credential_type=GatewayCredentialType.OAUTH,
            credential_provider_arn="arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default/oauth2credentialprovider/m2m",
            grant_type=GatewayOAuthGrantType.CLIENT_CREDENTIALS,
            custom_parameters={"audience": "https://api.example.com"},
        )

        service.create_target(config)

        oauth = boto_client.create_gateway_target.call_args.kwargs[
            "credentialProviderConfigurations"
        ][0]["credentialProvider"]["oauthCredentialProvider"]
        assert oauth["grantType"] == "CLIENT_CREDENTIALS"
        assert oauth["customParameters"] == {"audience": "https://api.example.com"}

    def test_api_key_credential_config(self, service, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        config = MCPGatewayConfig(
            target_name="weather-target",
            endpoint_url="https://example.com/mcp",
            credential_type=GatewayCredentialType.API_KEY,
            credential_provider_arn="arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default/apikeycredentialprovider/wx",
        )

        service.create_target(config)

        creds = boto_client.create_gateway_target.call_args.kwargs[
            "credentialProviderConfigurations"
        ]
        assert creds == [
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": config.credential_provider_arn,
                    }
                },
            }
        ]

    def test_iam_includes_region_when_set(self, service, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        service.create_target(_iam_config(aws_service="execute-api", aws_region="us-east-1"))
        iam = boto_client.create_gateway_target.call_args.kwargs[
            "credentialProviderConfigurations"
        ][0]["credentialProvider"]["iamCredentialProvider"]
        assert iam == {"service": "execute-api", "region": "us-east-1"}

    def test_none_credential_omits_configs(self, service, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        config = MCPGatewayConfig(
            target_name="public-target",
            endpoint_url="https://public.example.com/mcp",
            credential_type=GatewayCredentialType.NONE,
        )
        service.create_target(config)
        # A public endpoint omits credentialProviderConfigurations entirely.
        assert (
            "credentialProviderConfigurations"
            not in boto_client.create_gateway_target.call_args.kwargs
        )

    def test_dynamic_listing_maps_to_uppercase(self, service, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        service.create_target(_iam_config(listing_mode=GatewayListingMode.DYNAMIC))
        target_cfg = boto_client.create_gateway_target.call_args.kwargs[
            "targetConfiguration"
        ]
        assert target_cfg["mcp"]["mcpServer"]["listingMode"] == "DYNAMIC"

    def test_conflict_maps_to_conflict_error(self, service, boto_client):
        boto_client.create_gateway_target.side_effect = _client_error(
            "ConflictException"
        )
        with pytest.raises(GatewayTargetConflictError):
            service.create_target(_iam_config())

    def test_other_client_error_bubbles(self, service, boto_client):
        boto_client.create_gateway_target.side_effect = _client_error(
            "ValidationException"
        )
        with pytest.raises(ClientError):
            service.create_target(_iam_config())


class TestUpdateTarget:
    def test_update_kwargs(self, service, boto_client):
        boto_client.update_gateway_target.return_value = _create_response(
            status="UPDATING"
        )
        info = service.update_target(target_id="TGT1", config=_iam_config())

        kwargs = boto_client.update_gateway_target.call_args.kwargs
        assert kwargs["gatewayIdentifier"] == "gw-test-id"
        assert kwargs["targetId"] == "TGT1"
        assert kwargs["name"] == "weather-target"
        assert "targetConfiguration" in kwargs
        assert "credentialProviderConfigurations" in kwargs
        assert info.status == "UPDATING"

    def test_update_not_found_raises(self, service, boto_client):
        boto_client.update_gateway_target.side_effect = _client_error(
            "ResourceNotFoundException"
        )
        with pytest.raises(GatewayTargetNotFoundError):
            service.update_target(target_id="missing", config=_iam_config())


class TestGetTarget:
    def test_get_returns_info(self, service, boto_client):
        boto_client.get_gateway_target.return_value = _create_response(status="READY")
        info = service.get_target(target_id="TGT1")
        boto_client.get_gateway_target.assert_called_once_with(
            gatewayIdentifier="gw-test-id", targetId="TGT1"
        )
        assert info.target_id == "TGT1"
        assert info.status == "READY"
        # A healthy target reports no reasons.
        assert info.status_reasons == []

    def test_get_captures_status_reasons_for_failed_target(self, service, boto_client):
        """A FAILED target's statusReasons must be surfaced so the admin UI can
        explain the failure instead of leaving it invisible (issue #419 UX)."""
        reason = (
            "Failed to connect and fetch tools from the provided MCP target "
            "server. Error - Authorization error when sending message"
        )
        boto_client.get_gateway_target.return_value = _create_response(
            status="FAILED", statusReasons=[reason]
        )
        info = service.get_target(target_id="TGT1")
        assert info.status == "FAILED"
        assert info.status_reasons == [reason]

    def test_get_not_found_raises(self, service, boto_client):
        boto_client.get_gateway_target.side_effect = _client_error(
            "ResourceNotFoundException"
        )
        with pytest.raises(GatewayTargetNotFoundError):
            service.get_target(target_id="missing")


class TestDeleteTarget:
    def test_delete_success(self, service, boto_client):
        service.delete_target(target_id="TGT1")
        boto_client.delete_gateway_target.assert_called_once_with(
            gatewayIdentifier="gw-test-id", targetId="TGT1"
        )

    def test_delete_not_found_is_noop(self, service, boto_client):
        boto_client.delete_gateway_target.side_effect = _client_error(
            "ResourceNotFoundException"
        )
        # Must not raise — the catalog row is removed even when AWS is already clean.
        service.delete_target(target_id="missing")

    def test_delete_other_error_bubbles(self, service, boto_client):
        boto_client.delete_gateway_target.side_effect = _client_error(
            "ThrottlingException"
        )
        with pytest.raises(ClientError):
            service.delete_target(target_id="TGT1")


class TestListTargets:
    def test_paginates(self, service, boto_client):
        boto_client.list_gateway_targets.side_effect = [
            {"items": [{"targetId": "a", "name": "na", "status": "READY"}], "nextToken": "t"},
            {"items": [{"targetId": "b", "name": "nb", "status": "READY"}]},
        ]
        infos = service.list_targets()
        assert [i.target_id for i in infos] == ["a", "b"]
        assert boto_client.list_gateway_targets.call_count == 2
        assert boto_client.list_gateway_targets.call_args_list[1].kwargs["nextToken"] == "t"


class TestGatewayIdResolution:
    def test_resolved_once_and_cached(self, service, boto_client, ssm_client):
        boto_client.create_gateway_target.return_value = _create_response()
        service.create_target(_iam_config())
        service.delete_target(target_id="TGT1")
        # SSM is read exactly once across multiple operations.
        ssm_client.get_parameter.assert_called_once()

    def test_explicit_gateway_id_skips_ssm(self, boto_client, ssm_client):
        svc = GatewayTargetService(
            client=boto_client, ssm_client=ssm_client, gateway_id="gw-explicit"
        )
        boto_client.create_gateway_target.return_value = _create_response()
        svc.create_target(_iam_config())
        ssm_client.get_parameter.assert_not_called()
        assert (
            boto_client.create_gateway_target.call_args.kwargs["gatewayIdentifier"]
            == "gw-explicit"
        )

    def test_missing_ssm_param_raises_runtime_error(self, boto_client, ssm_client):
        ssm_client.get_parameter.side_effect = _client_error("ParameterNotFound")
        svc = GatewayTargetService(client=boto_client, ssm_client=ssm_client)
        with pytest.raises(RuntimeError):
            svc.create_target(_iam_config())

    def test_env_override_skips_ssm(self, boto_client, ssm_client, monkeypatch):
        # AGENTCORE_GATEWAY_ID lets local/CI bypass the SSM lookup entirely.
        monkeypatch.setenv("AGENTCORE_GATEWAY_ID", "gw-from-env")
        svc = GatewayTargetService(client=boto_client, ssm_client=ssm_client)
        boto_client.create_gateway_target.return_value = _create_response()
        svc.create_target(_iam_config())
        ssm_client.get_parameter.assert_not_called()
        assert (
            boto_client.create_gateway_target.call_args.kwargs["gatewayIdentifier"]
            == "gw-from-env"
        )


class TestLambdaGrant:
    """Per-target gateway-role InvokeFunctionUrl grant (#419 admin self-service)."""

    LAMBDA_URL = "https://abc123.lambda-url.us-west-2.on.aws/mcp"

    def _lambda(self):
        lam = MagicMock()
        lam.get_function_url_config.return_value = {"FunctionUrl": self.LAMBDA_URL}
        lam.remove_permission.side_effect = _client_error("ResourceNotFoundException")
        return lam

    def test_create_grants_role_before_creating_target(self, boto_client):
        boto_client.get_gateway.return_value = {"roleArn": "arn:aws:iam::1:role/gw"}
        boto_client.create_gateway_target.return_value = _create_response()
        lam = self._lambda()
        svc = GatewayTargetService(
            client=boto_client, gateway_id="gw-test-id", lambda_client=lam
        )
        svc.create_target(
            _iam_config(endpoint_url=self.LAMBDA_URL, lambda_function_name="mcp-foo-dev")
        )
        # Grant names the gateway role on exactly the target function...
        lam.add_permission.assert_called_once()
        assert lam.add_permission.call_args.kwargs["Principal"] == "arn:aws:iam::1:role/gw"
        assert lam.add_permission.call_args.kwargs["Action"] == "lambda:InvokeFunctionUrl"
        # ...and the target is still created.
        boto_client.create_gateway_target.assert_called_once()

    def test_non_lambda_iam_target_skips_grant(self, boto_client):
        boto_client.create_gateway_target.return_value = _create_response()
        lam = MagicMock()
        svc = GatewayTargetService(
            client=boto_client, gateway_id="gw-test-id", lambda_client=lam
        )
        svc.create_target(_iam_config())  # endpoint is example.com, not a Lambda URL
        lam.add_permission.assert_not_called()
        boto_client.get_gateway.assert_not_called()

    def test_delete_revokes_grant(self, boto_client):
        lam = MagicMock()
        svc = GatewayTargetService(
            client=boto_client, gateway_id="gw-test-id", lambda_client=lam
        )
        cfg = _iam_config(
            endpoint_url=self.LAMBDA_URL, lambda_function_name="mcp-foo-dev"
        )
        svc.delete_target(target_id="TGT1", config=cfg)
        lam.remove_permission.assert_called_once_with(
            FunctionName="mcp-foo-dev", StatementId="agentcore-gw-weather-target"
        )
