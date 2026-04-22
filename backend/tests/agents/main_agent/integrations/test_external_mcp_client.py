"""
Tests for the external MCP client helpers.

OAuth provisioning moved to `OAuthConsentHook` (see
`tests/agents/main_agent/session/hooks/test_oauth_consent.py`); this
module covers the URL-parsing helpers and the integration's
MCPClient -> provider_id map that the hook reads from.

Requirements: 25.1–25.3
"""
import pytest

from agents.main_agent.integrations.external_mcp_client import (
    ExternalMCPIntegration,
    detect_aws_service_from_url,
    extract_region_from_url,
)


class TestExtractRegionFromUrl:
    """Tests for extract_region_from_url region extraction."""

    def test_extracts_region_from_lambda_url(self):
        """Req 25.1: Extracts region from Lambda Function URL."""
        url = "https://abc123.lambda-url.us-west-2.on.aws/"
        assert extract_region_from_url(url) == "us-west-2"

    def test_extracts_region_from_api_gateway_url(self):
        """Req 25.1: Extracts region from API Gateway URL."""
        url = "https://xyz789.execute-api.eu-west-1.amazonaws.com/prod"
        assert extract_region_from_url(url) == "eu-west-1"

    def test_extracts_region_from_agentcore_url(self):
        """Req 25.1: Extracts region from AgentCore Gateway URL."""
        url = "https://gateway-abc.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
        assert extract_region_from_url(url) == "us-east-1"

    def test_returns_none_for_non_matching_url(self):
        """Req 25.2: Returns None when URL has no recognizable region pattern."""
        url = "https://example.com/api/v1"
        assert extract_region_from_url(url) is None

    def test_returns_none_for_plain_domain(self):
        """Req 25.2: Returns None for a plain domain with no AWS pattern."""
        url = "https://my-mcp-server.herokuapp.com/mcp"
        assert extract_region_from_url(url) is None


class TestDetectAwsServiceFromUrl:
    """Tests for detect_aws_service_from_url service detection."""

    def test_detects_lambda_service(self):
        """Req 25.3: Detects 'lambda' for Lambda Function URLs."""
        url = "https://abc123.lambda-url.us-west-2.on.aws/"
        assert detect_aws_service_from_url(url) == "lambda"

    def test_detects_execute_api_service(self):
        """Req 25.3: Detects 'execute-api' for API Gateway URLs."""
        url = "https://xyz789.execute-api.us-east-1.amazonaws.com/prod"
        assert detect_aws_service_from_url(url) == "execute-api"

    def test_detects_bedrock_agentcore_service(self):
        """Req 25.3: Detects 'bedrock-agentcore' for AgentCore Gateway URLs."""
        url = "https://gateway-abc.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
        assert detect_aws_service_from_url(url) == "bedrock-agentcore"

    def test_defaults_to_lambda_for_unknown_url(self):
        """Req 25.3: Defaults to 'lambda' for unrecognized URL patterns."""
        url = "https://example.com/api/v1"
        assert detect_aws_service_from_url(url) == "lambda"


class TestProviderForClient:
    """The integration's MCPClient -> provider_id map is what
    `OAuthConsentHook.provider_lookup` consults."""

    def test_unknown_client_returns_none(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        assert integration.provider_for_client(FakeClient()) is None

    def test_records_and_resolves_provider_for_client(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        client = FakeClient()
        # Simulate what `load_external_tools` does after creating an
        # OAuth-gated MCP client.
        integration._provider_for_client_id[id(client)] = "google-workspace"

        assert integration.provider_for_client(client) == "google-workspace"

    def test_clear_user_clients_drops_provider_mapping(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        client = FakeClient()
        integration.clients["alice:gmail"] = client
        integration._provider_for_client_id[id(client)] = "google-workspace"

        integration.clear_user_clients("alice")

        assert "alice:gmail" not in integration.clients
        assert integration.provider_for_client(client) is None
