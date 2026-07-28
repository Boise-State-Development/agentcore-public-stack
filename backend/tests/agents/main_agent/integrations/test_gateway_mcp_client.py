"""
Tests for FilteredMCPClient, get_gateway_client_if_enabled, and create_filtered_gateway_client.

Requirements: 24.1–24.3
"""
import httpx
import pytest
from unittest.mock import patch, MagicMock

from agents.main_agent.integrations.gateway_mcp_client import (
    FilteredMCPClient,
    GatewayAuthError,
    _build_gateway_auth,
    _gateway_inbound_auth_mode,
    get_gateway_client_if_enabled,
    create_filtered_gateway_client,
)
from agents.main_agent.integrations.oauth_auth import OAuthBearerAuth
from agents.main_agent.tools.gateway_integration import GatewayIntegration


class TestFilteredMCPClient:
    """Tests for FilteredMCPClient initialization and attribute storage."""

    def test_stores_enabled_tool_ids(self):
        """Req 24.1: FilteredMCPClient stores the enabled_tool_ids."""
        tool_ids = ["gateway_wiki_search", "gateway_arxiv_search"]
        client = FilteredMCPClient(
            client_factory=MagicMock(),
            enabled_tool_ids=tool_ids,
        )
        assert client.enabled_tool_ids == tool_ids

    def test_stores_prefix(self):
        """Req 24.1: FilteredMCPClient stores the prefix."""
        client = FilteredMCPClient(
            client_factory=MagicMock(),
            enabled_tool_ids=["gateway_tool1"],
            prefix="custom_prefix",
        )
        assert client.prefix == "custom_prefix"

    def test_default_prefix_is_gateway(self):
        """Req 24.1: FilteredMCPClient defaults prefix to 'gateway'."""
        client = FilteredMCPClient(
            client_factory=MagicMock(),
            enabled_tool_ids=[],
        )
        assert client.prefix == "gateway"


class TestGetGatewayClientIfEnabled:
    """Tests for get_gateway_client_if_enabled environment gating."""

    @patch("agents.main_agent.integrations.gateway_mcp_client.GATEWAY_ENABLED", False)
    def test_returns_none_when_disabled(self):
        """Req 24.2: Returns None when AGENTCORE_GATEWAY_MCP_ENABLED is 'false'."""
        result = get_gateway_client_if_enabled(enabled_tool_ids=["gateway_tool1"])
        assert result is None

    @patch("agents.main_agent.integrations.gateway_mcp_client.GATEWAY_ENABLED", False)
    def test_returns_none_when_disabled_no_tool_ids(self):
        """Req 24.2: Returns None when disabled even without tool IDs."""
        result = get_gateway_client_if_enabled()
        assert result is None


class TestCreateFilteredGatewayClient:
    """Tests for create_filtered_gateway_client with no gateway tool IDs."""

    def test_returns_none_when_no_gateway_ids(self):
        """Req 24.3: WHEN no gateway tool IDs are provided, returns None."""
        result = create_filtered_gateway_client(enabled_tool_ids=[])
        assert result is None

    def test_returns_none_when_no_ids_match_prefix(self):
        """Req 24.3: WHEN enabled IDs don't start with prefix, returns None."""
        result = create_filtered_gateway_client(
            enabled_tool_ids=["local_calculator", "local_weather"],
        )
        assert result is None

    def test_returns_none_with_custom_prefix_no_match(self):
        """Req 24.3: WHEN using custom prefix and no IDs match, returns None."""
        result = create_filtered_gateway_client(
            enabled_tool_ids=["gateway_tool1", "gateway_tool2"],
            prefix="custom",
        )
        assert result is None


class TestGatewayInboundAuthMode:
    """Auth-mode resolution from the CDK-managed env var."""

    def test_defaults_to_jwt(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_GATEWAY_INBOUND_AUTH", raising=False)
        assert _gateway_inbound_auth_mode() == "jwt"

    def test_reads_iam(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "iam")
        assert _gateway_inbound_auth_mode() == "iam"

    def test_is_case_insensitive_and_trims(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "  IAM  ")
        assert _gateway_inbound_auth_mode() == "iam"

    def test_unknown_value_falls_back_to_jwt(self, monkeypatch):
        """An unrecognized value must not silently disable user auth."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "banana")
        assert _gateway_inbound_auth_mode() == "jwt"


class TestBuildGatewayAuth:
    """Auth handler selection — the security-critical path."""

    def test_jwt_mode_uses_bearer_token(self, monkeypatch):
        """JWT mode attaches the user's token as a Bearer credential."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "jwt")
        auth = _build_gateway_auth("us-west-2", "user-token-abc")
        assert isinstance(auth, OAuthBearerAuth)

        # Verify the token actually lands on the Authorization header.
        request = httpx.Request("POST", "https://gateway.example.com/mcp")
        next(auth.auth_flow(request))
        assert request.headers["Authorization"] == "Bearer user-token-abc"

    def test_jwt_mode_without_token_raises(self, monkeypatch):
        """No user token in JWT mode must fail loudly, not build a 401 client."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "jwt")
        with pytest.raises(GatewayAuthError, match="no user access token"):
            _build_gateway_auth("us-west-2", None)

    def test_jwt_mode_with_empty_token_raises(self, monkeypatch):
        """An empty-string token is as unusable as a missing one."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "jwt")
        with pytest.raises(GatewayAuthError):
            _build_gateway_auth("us-west-2", "")

    def test_iam_mode_uses_sigv4_and_ignores_token(self, monkeypatch):
        """IAM rollback mode signs with SigV4 regardless of any user token."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "iam")
        sentinel = MagicMock()
        with patch(
            "agents.main_agent.integrations.gateway_mcp_client.get_sigv4_auth",
            return_value=sentinel,
        ) as mock_sigv4:
            assert _build_gateway_auth("us-west-2", None) is sentinel
            mock_sigv4.assert_called_once_with(region="us-west-2")

    def test_tokens_are_not_shared_between_clients(self, monkeypatch):
        """Two users' auth handlers must carry their own distinct tokens.

        Guards the multi-tenant invariant: a bearer credential must never be
        cached at module scope or reused across agent instances.
        """
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "jwt")
        auth_a = _build_gateway_auth("us-west-2", "token-user-a")
        auth_b = _build_gateway_auth("us-west-2", "token-user-b")

        req_a = httpx.Request("POST", "https://gateway.example.com/mcp")
        req_b = httpx.Request("POST", "https://gateway.example.com/mcp")
        next(auth_a.auth_flow(req_a))
        next(auth_b.auth_flow(req_b))

        assert req_a.headers["Authorization"] == "Bearer token-user-a"
        assert req_b.headers["Authorization"] == "Bearer token-user-b"


class TestGatewayIntegrationAuthDegradation:
    """GatewayIntegration must degrade gracefully when no token is available."""

    def test_missing_token_yields_no_client_instead_of_raising(self, monkeypatch):
        """A tokenless turn loses Gateway tools but does not fail outright."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "jwt")
        integration = GatewayIntegration()
        with patch(
            "agents.main_agent.tools.gateway_integration.get_gateway_client_if_enabled",
            side_effect=GatewayAuthError("no token"),
        ):
            result = integration.get_client(["gateway_a___b"], auth_token=None)
        assert result is None
        assert integration.client is None
        assert integration.is_available() is False

    def test_token_is_forwarded_to_client_factory(self, monkeypatch):
        """The user's token must reach the client factory unchanged."""
        monkeypatch.setenv("AGENTCORE_GATEWAY_INBOUND_AUTH", "jwt")
        integration = GatewayIntegration()
        sentinel = MagicMock()
        with patch(
            "agents.main_agent.tools.gateway_integration.get_gateway_client_if_enabled",
            return_value=sentinel,
        ) as mock_factory:
            result = integration.get_client(["gateway_a___b"], auth_token="tok-123")
        assert result is sentinel
        mock_factory.assert_called_once_with(
            enabled_tool_ids=["gateway_a___b"], auth_token="tok-123"
        )
