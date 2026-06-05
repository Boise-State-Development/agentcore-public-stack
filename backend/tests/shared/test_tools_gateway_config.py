"""Phase 1 (issue #419): MCPGatewayConfig model, serialization, request/response
wiring, and the credential/listing-mode co-gating validator.

These are pure-model tests — no AWS. The GatewayTargetService that turns a
config into a live Gateway target is covered separately in
test_gateway_target_service.py.
"""

import pytest
from pydantic import ValidationError

from apis.shared.tools.models import (
    AdminToolResponse,
    GatewayCredentialType,
    GatewayListingMode,
    GatewayOAuthGrantType,
    MCPGatewayConfig,
    MCPGatewayConfigRequest,
    MCPGatewayConfigResponse,
    MCPToolEntry,
    ToolDefinition,
    ToolProtocol,
)


# ---------------------------------------------------------------- serialization


class TestMCPGatewayConfigSerialization:
    def test_iam_round_trip(self):
        config = MCPGatewayConfig(
            target_name="weather-target",
            endpoint_url="https://example.com/mcp",
            listing_mode=GatewayListingMode.DEFAULT,
            credential_type=GatewayCredentialType.GATEWAY_IAM_ROLE,
            aws_service="lambda",
            aws_region="us-west-2",
            tools=[
                MCPToolEntry(name="get_forecast", needs_approval=False),
                MCPToolEntry(name="set_alert", needs_approval=True, description="writes"),
            ],
            target_id="TGT123",
            gateway_arn="arn:aws:bedrock-agentcore:us-west-2:111:gateway/gw-abc",
        )

        data = config.to_dict()
        assert data["targetName"] == "weather-target"
        assert data["endpointUrl"] == "https://example.com/mcp"
        assert data["listingMode"] == "default"
        assert data["credentialType"] == "gateway_iam_role"
        assert data["credentialProviderArn"] is None
        assert data["awsService"] == "lambda"
        assert data["awsRegion"] == "us-west-2"
        assert data["targetId"] == "TGT123"
        assert data["gatewayArn"].endswith("gateway/gw-abc")
        assert [t["name"] for t in data["tools"]] == ["get_forecast", "set_alert"]

        restored = MCPGatewayConfig.from_dict(data)
        assert restored.target_name == "weather-target"
        assert restored.listing_mode == GatewayListingMode.DEFAULT
        assert restored.credential_type == GatewayCredentialType.GATEWAY_IAM_ROLE
        assert restored.aws_service == "lambda"
        assert restored.target_id == "TGT123"
        assert restored.gateway_arn == config.gateway_arn
        assert restored.approval_required_names() == {"set_alert"}

    def test_oauth_round_trip(self):
        config = MCPGatewayConfig(
            target_name="github-target",
            endpoint_url="https://github-mcp.example.com/mcp",
            listing_mode=GatewayListingMode.DEFAULT,
            credential_type=GatewayCredentialType.OAUTH,
            credential_provider_arn="arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default/oauth2credentialprovider/github",
            oauth_scopes=["repo", "read:user"],
            custom_parameters={"audience": "https://api.github.com"},
        )
        # 3LO (authorization_code) is the default grant.
        assert config.grant_type == GatewayOAuthGrantType.AUTHORIZATION_CODE

        data = config.to_dict()
        assert data["grantType"] == "authorization_code"
        assert data["customParameters"] == {"audience": "https://api.github.com"}

        restored = MCPGatewayConfig.from_dict(data)
        assert restored.credential_type == GatewayCredentialType.OAUTH
        assert restored.oauth_scopes == ["repo", "read:user"]
        assert restored.credential_provider_arn == config.credential_provider_arn
        assert restored.grant_type == GatewayOAuthGrantType.AUTHORIZATION_CODE
        assert restored.custom_parameters == {"audience": "https://api.github.com"}

    def test_client_credentials_grant_round_trip(self):
        config = MCPGatewayConfig(
            target_name="m2m-target",
            endpoint_url="https://m2m.example.com/mcp",
            credential_type=GatewayCredentialType.OAUTH,
            credential_provider_arn="arn:...:oauth2credentialprovider/m2m",
            grant_type=GatewayOAuthGrantType.CLIENT_CREDENTIALS,
        )
        restored = MCPGatewayConfig.from_dict(config.to_dict())
        assert restored.grant_type == GatewayOAuthGrantType.CLIENT_CREDENTIALS
        assert restored.custom_parameters is None

    def test_from_dict_defaults_and_legacy_string_tools(self):
        # Sparse dict (e.g. an older / minimal row) hydrates with safe defaults,
        # and the legacy List[str] tools format is tolerated like MCPServerConfig.
        restored = MCPGatewayConfig.from_dict(
            {
                "targetName": "t",
                "endpointUrl": "https://e/mcp",
                "tools": ["alpha", "beta"],
            }
        )
        assert restored.listing_mode == GatewayListingMode.DEFAULT
        # Default credential type is NONE (public) — the least-config path.
        assert restored.credential_type == GatewayCredentialType.NONE
        assert [t.name for t in restored.tools] == ["alpha", "beta"]


# ----------------------------------------------------------- ToolDefinition link


class TestToolDefinitionGatewayConfig:
    def _gateway_tool(self) -> ToolDefinition:
        return ToolDefinition(
            tool_id="gw_weather",
            display_name="Weather (Gateway)",
            description="Weather via the AgentCore Gateway",
            protocol=ToolProtocol.MCP_GATEWAY,
            mcp_gateway_config=MCPGatewayConfig(
                target_name="weather-target",
                endpoint_url="https://example.com/mcp",
                target_id="TGT123",
                gateway_arn="arn:aws:bedrock-agentcore:us-west-2:111:gateway/gw-abc",
            ),
        )

    def test_to_dynamo_item_emits_gateway_config(self):
        item = self._gateway_tool().to_dynamo_item()
        assert "mcpGatewayConfig" in item
        assert item["mcpGatewayConfig"]["targetId"] == "TGT123"
        # The other protocol configs stay absent — no cross-contamination.
        assert "mcpConfig" not in item
        assert "a2aConfig" not in item

    def test_dynamo_round_trip(self):
        tool = self._gateway_tool()
        restored = ToolDefinition.from_dynamo_item(tool.to_dynamo_item())
        assert restored.protocol == ToolProtocol.MCP_GATEWAY
        assert restored.mcp_gateway_config is not None
        assert restored.mcp_gateway_config.target_name == "weather-target"
        assert restored.mcp_gateway_config.target_id == "TGT123"
        assert restored.mcp_config is None
        assert restored.a2a_config is None

    def test_non_gateway_tool_has_no_gateway_config_key(self):
        tool = ToolDefinition(
            tool_id="local_thing",
            display_name="Local",
            description="A local tool",
            protocol=ToolProtocol.LOCAL,
        )
        item = tool.to_dynamo_item()
        assert "mcpGatewayConfig" not in item
        assert ToolDefinition.from_dynamo_item(item).mcp_gateway_config is None


# ------------------------------------------------------------- request/response


class TestRequestResponseWiring:
    def test_request_to_model(self):
        req = MCPGatewayConfigRequest(
            **{
                "targetName": "weather-target",
                "endpointUrl": "https://example.com/mcp",
                "listingMode": "default",
                "credentialType": "gateway_iam_role",
                "awsService": "lambda",
                "tools": [{"name": "get_forecast", "needsApproval": True}],
            }
        )
        model = req.to_model()
        assert isinstance(model, MCPGatewayConfig)
        assert model.target_name == "weather-target"
        assert model.aws_service == "lambda"
        assert model.approval_required_names() == {"get_forecast"}
        # Request never carries AWS-assigned identifiers.
        assert model.target_id is None
        assert model.gateway_arn is None

    def test_request_carries_oauth_grant_and_custom_parameters(self):
        req = MCPGatewayConfigRequest(
            **{
                "targetName": "github-target",
                "endpointUrl": "https://gh/mcp",
                "credentialType": "oauth",
                "credentialProviderArn": "arn:...:oauth2credentialprovider/gh",
                "grantType": "client_credentials",
                "customParameters": {"audience": "https://api.github.com"},
            }
        )
        model = req.to_model()
        assert model.grant_type == GatewayOAuthGrantType.CLIENT_CREDENTIALS
        assert model.custom_parameters == {"audience": "https://api.github.com"}
        resp = MCPGatewayConfigResponse.from_model(model)
        assert resp.grant_type == "client_credentials"
        assert resp.custom_parameters == {"audience": "https://api.github.com"}

    def test_response_from_model_exposes_identifiers(self):
        config = MCPGatewayConfig(
            target_name="weather-target",
            endpoint_url="https://example.com/mcp",
            target_id="TGT123",
            gateway_arn="arn:aws:bedrock-agentcore:us-west-2:111:gateway/gw-abc",
        )
        resp = MCPGatewayConfigResponse.from_model(config)
        assert resp.target_id == "TGT123"
        assert resp.listing_mode == "default"
        # Default credential type is the public (none) path.
        assert resp.credential_type == "none"

    def test_admin_tool_response_includes_gateway_config(self):
        tool = ToolDefinition(
            tool_id="gw_weather",
            display_name="Weather (Gateway)",
            description="Weather via the AgentCore Gateway",
            protocol=ToolProtocol.MCP_GATEWAY,
            mcp_gateway_config=MCPGatewayConfig(
                target_name="weather-target",
                endpoint_url="https://example.com/mcp",
                target_id="TGT123",
            ),
        )
        resp = AdminToolResponse.from_tool_definition(tool, allowed_roles=[])
        assert resp.mcp_gateway_config is not None
        assert resp.mcp_gateway_config.target_name == "weather-target"
        assert resp.mcp_config is None
        assert resp.a2a_config is None


# ------------------------------------------------------------------- validation


class TestCoGatingValidator:
    def test_oauth_requires_default_listing(self):
        with pytest.raises(ValidationError):
            MCPGatewayConfig(
                target_name="t",
                endpoint_url="https://e/mcp",
                credential_type=GatewayCredentialType.OAUTH,
                credential_provider_arn="arn:...:oauth2credentialprovider/x",
                listing_mode=GatewayListingMode.DYNAMIC,
            )

    def test_oauth_requires_provider_arn(self):
        with pytest.raises(ValidationError):
            MCPGatewayConfig(
                target_name="t",
                endpoint_url="https://e/mcp",
                credential_type=GatewayCredentialType.OAUTH,
            )

    def test_api_key_requires_provider_arn(self):
        with pytest.raises(ValidationError):
            MCPGatewayConfig(
                target_name="t",
                endpoint_url="https://e/mcp",
                credential_type=GatewayCredentialType.API_KEY,
            )

    def test_iam_rejects_provider_arn(self):
        with pytest.raises(ValidationError):
            MCPGatewayConfig(
                target_name="t",
                endpoint_url="https://e/mcp",
                credential_type=GatewayCredentialType.GATEWAY_IAM_ROLE,
                aws_service="lambda",
                credential_provider_arn="arn:...:oauth2credentialprovider/x",
            )

    def test_iam_requires_aws_service(self):
        # mcpServer IAM targets need an explicit service for the
        # iamCredentialProvider — without it AWS rejects CreateGatewayTarget.
        with pytest.raises(ValidationError):
            MCPGatewayConfig(
                target_name="t",
                endpoint_url="https://e/mcp",
                credential_type=GatewayCredentialType.GATEWAY_IAM_ROLE,
            )

    def test_valid_configs_accepted(self):
        # None (public) — the default; minimal config.
        MCPGatewayConfig(target_name="t", endpoint_url="https://e/mcp")
        # IAM with aws_service, no ARN
        MCPGatewayConfig(
            target_name="t",
            endpoint_url="https://e/mcp",
            credential_type=GatewayCredentialType.GATEWAY_IAM_ROLE,
            aws_service="lambda",
        )
        # OAuth, DEFAULT listing, with ARN
        MCPGatewayConfig(
            target_name="t",
            endpoint_url="https://e/mcp",
            credential_type=GatewayCredentialType.OAUTH,
            credential_provider_arn="arn:...:oauth2credentialprovider/x",
            listing_mode=GatewayListingMode.DEFAULT,
        )
        # API key with ARN
        MCPGatewayConfig(
            target_name="t",
            endpoint_url="https://e/mcp",
            credential_type=GatewayCredentialType.API_KEY,
            credential_provider_arn="arn:...:apikeycredentialprovider/x",
        )

    def test_request_to_model_propagates_validation(self):
        # An invalid combination at the wire layer surfaces as a validation
        # error when materialized — the route relies on this for its 400.
        req = MCPGatewayConfigRequest(
            **{
                "targetName": "t",
                "endpointUrl": "https://e/mcp",
                "credentialType": "oauth",
                "listingMode": "dynamic",
                "credentialProviderArn": "arn:...:oauth2credentialprovider/x",
            }
        )
        with pytest.raises(ValidationError):
            req.to_model()
