"""
Tests for the external MCP client helpers.

OAuth provisioning moved to `OAuthConsentHook` (see
`tests/agents/main_agent/session/hooks/test_oauth_consent.py`); this
module covers the URL-parsing helpers and the integration's
MCPClient -> provider_id map that the hook reads from.

Requirements: 25.1–25.3
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class TestClearToolClients:
    """Admin updates to a tool must invalidate cached clients for that
    tool so the next agent build reconnects with the updated config."""

    def test_clears_non_oauth_tool_and_keeps_other_tools(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        gmail = FakeClient()
        jira = FakeClient()
        integration.clients["gmail"] = gmail
        integration.clients["jira"] = jira

        integration.clear_tool_clients("gmail")

        assert "gmail" not in integration.clients
        assert integration.clients["jira"] is jira

    def test_clears_all_user_scoped_keys_for_tool(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        alice_gmail = FakeClient()
        bob_gmail = FakeClient()
        alice_jira = FakeClient()
        integration.clients["alice:gmail"] = alice_gmail
        integration.clients["bob:gmail"] = bob_gmail
        integration.clients["alice:jira"] = alice_jira
        integration._provider_for_client_id[id(alice_gmail)] = "google-workspace"
        integration._provider_for_client_id[id(bob_gmail)] = "google-workspace"

        integration.clear_tool_clients("gmail")

        assert "alice:gmail" not in integration.clients
        assert "bob:gmail" not in integration.clients
        assert integration.clients["alice:jira"] is alice_jira
        assert integration.provider_for_client(alice_gmail) is None
        assert integration.provider_for_client(bob_gmail) is None

    def test_does_not_match_tool_id_as_key_suffix_without_colon(self):
        """Guard against substring false positives: a tool named "gmail"
        must not clear a tool named "super-gmail"."""
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        super_gmail = FakeClient()
        integration.clients["super-gmail"] = super_gmail

        integration.clear_tool_clients("gmail")

        assert integration.clients["super-gmail"] is super_gmail

    def test_no_op_when_tool_not_cached(self):
        integration = ExternalMCPIntegration()
        integration.clear_tool_clients("never-loaded")
        assert integration.clients == {}


def _fake_tool(updated_at, tool_id="gmail"):
    """Minimal tool stand-in for load_external_tools."""
    return SimpleNamespace(
        tool_id=tool_id,
        protocol="mcp_external",
        mcp_config=SimpleNamespace(
            server_url="https://example.com/mcp",
            approval_required_names=lambda: set(),
        ),
        forward_auth_token=False,
        requires_oauth_provider=None,
        updated_at=updated_at,
    )


class TestLoadExternalToolsVersioning:
    """`load_external_tools` must rebuild the MCPClient when the tool's
    `updated_at` changes. Without this, admin edits to MCP config (URL,
    auth mode, etc.) never take effect for the process lifetime."""

    @pytest.mark.asyncio
    async def test_reuses_client_when_updated_at_unchanged(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))

        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.app_api.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ) as create_mock:
            first = await integration.load_external_tools(["gmail"])
            second = await integration.load_external_tools(["gmail"])

        assert first == second
        assert create_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_rebuilds_client_when_updated_at_changes(self):
        integration = ExternalMCPIntegration()
        old = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        new = _fake_tool(datetime(2025, 2, 1, tzinfo=timezone.utc))

        repo = SimpleNamespace(get_tool=AsyncMock(side_effect=[old, new]))

        client_old = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        client_new = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.app_api.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[client_old, client_new],
        ):
            first = await integration.load_external_tools(["gmail"])
            second = await integration.load_external_tools(["gmail"])

        assert first == [client_old]
        assert second == [client_new]
        assert integration.clients["gmail"] is client_new
        # Old client must be evicted, not left dangling under the same key.
        assert client_old not in integration.clients.values()


class TestLoadExternalToolsPreflight:
    """A single unreachable MCP server must not fail the whole turn —
    `load_external_tools` pre-flights each new client and silently drops
    the ones whose session can't be opened."""

    @pytest.mark.asyncio
    async def test_skips_client_when_preflight_fails(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))

        bad_client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("connection refused"))
        )

        with patch(
            "apis.app_api.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=bad_client,
        ):
            result = await integration.load_external_tools(["gmail"])

        assert result == []
        # Failed clients must not be cached — otherwise we'd serve a
        # broken client back on subsequent turns.
        assert "gmail" not in integration.clients

    @pytest.mark.asyncio
    async def test_one_failing_client_does_not_block_others(self):
        integration = ExternalMCPIntegration()
        bad_tool = _fake_tool(
            datetime(2025, 1, 1, tzinfo=timezone.utc), tool_id="calendar"
        )
        good_tool = _fake_tool(
            datetime(2025, 1, 1, tzinfo=timezone.utc), tool_id="gmail"
        )
        repo = SimpleNamespace(
            get_tool=AsyncMock(side_effect=[bad_tool, good_tool])
        )

        bad_client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("connection refused"))
        )
        good_client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.app_api.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[bad_client, good_client],
        ):
            result = await integration.load_external_tools(["calendar", "gmail"])

        assert result == [good_client]
        assert integration.clients == {"gmail": good_client}
