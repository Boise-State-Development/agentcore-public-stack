"""
Tests for extract_region_from_url and detect_aws_service_from_url.

Requirements: 25.1–25.3
"""
import pytest

from unittest.mock import AsyncMock, MagicMock, patch

from agents.main_agent.integrations.agentcore_identity import (
    TokenResult,
    WorkloadTokenUnavailableError,
)
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


class TestGetOAuthTokenViaAgentCoreIdentity:
    """Tests for ExternalMCPIntegration._get_oauth_token delegating to AgentCore Identity."""

    @pytest.mark.asyncio
    async def test_fetches_scopes_from_provider_repo_and_calls_identity(self):
        """Provider scopes from the platform's provider record are forwarded to AgentCore."""
        integration = ExternalMCPIntegration()

        mock_provider = MagicMock()
        mock_provider.scopes = ["openid", "profile", "email"]
        mock_repo = MagicMock()
        mock_repo.get_provider = AsyncMock(return_value=mock_provider)

        mock_identity = MagicMock()
        mock_identity.get_token_for_user.return_value = TokenResult(access_token="tok")

        with patch(
            "apis.shared.oauth.provider_repository.get_provider_repository",
            return_value=mock_repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.get_agentcore_identity_client",
            return_value=mock_identity,
        ):
            result = await integration._get_oauth_token(provider_id="google")

        assert result.access_token == "tok"
        mock_identity.get_token_for_user.assert_called_once_with(
            provider_name="google", scopes=["openid", "profile", "email"]
        )

    @pytest.mark.asyncio
    async def test_returns_authorization_url_when_consent_required(self):
        integration = ExternalMCPIntegration()

        mock_repo = MagicMock()
        mock_repo.get_provider = AsyncMock(
            return_value=MagicMock(scopes=["openid"])
        )

        mock_identity = MagicMock()
        mock_identity.get_token_for_user.return_value = TokenResult(
            authorization_url="https://accounts.example.com/consent"
        )

        with patch(
            "apis.shared.oauth.provider_repository.get_provider_repository",
            return_value=mock_repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.get_agentcore_identity_client",
            return_value=mock_identity,
        ):
            result = await integration._get_oauth_token(provider_id="google")

        assert result.requires_consent is True
        assert result.authorization_url == "https://accounts.example.com/consent"

    @pytest.mark.asyncio
    async def test_empty_scopes_when_provider_record_missing(self):
        """Missing provider record falls back to empty scopes so the call still succeeds
        and AgentCore can apply its own provider defaults."""
        integration = ExternalMCPIntegration()

        mock_repo = MagicMock()
        mock_repo.get_provider = AsyncMock(return_value=None)

        mock_identity = MagicMock()
        mock_identity.get_token_for_user.return_value = TokenResult(access_token="t")

        with patch(
            "apis.shared.oauth.provider_repository.get_provider_repository",
            return_value=mock_repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.get_agentcore_identity_client",
            return_value=mock_identity,
        ):
            await integration._get_oauth_token(provider_id="unknown")

        mock_identity.get_token_for_user.assert_called_once_with(
            provider_name="unknown", scopes=[]
        )

    @pytest.mark.asyncio
    async def test_propagates_workload_token_unavailable(self):
        """Misconfigured middleware should surface as a typed error, not be swallowed."""
        integration = ExternalMCPIntegration()

        mock_repo = MagicMock()
        mock_repo.get_provider = AsyncMock(return_value=MagicMock(scopes=[]))

        mock_identity = MagicMock()
        mock_identity.get_token_for_user.side_effect = WorkloadTokenUnavailableError(
            "no ctx"
        )

        with patch(
            "apis.shared.oauth.provider_repository.get_provider_repository",
            return_value=mock_repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.get_agentcore_identity_client",
            return_value=mock_identity,
        ):
            with pytest.raises(WorkloadTokenUnavailableError):
                await integration._get_oauth_token(provider_id="google")


class TestPendingConsent:
    """Tests for the per-user consent URL stash consumed by the SSE emitter."""

    def test_record_and_drain_roundtrip(self):
        integration = ExternalMCPIntegration()
        integration._record_pending_consent(
            user_id="u1", provider_id="google", authorization_url="https://a/1"
        )

        drained = integration.drain_pending_consent("u1")

        assert drained == [
            {"provider_id": "google", "authorization_url": "https://a/1"}
        ]

    def test_drain_is_idempotent(self):
        """Second drain returns empty — consent prompts are single-delivery."""
        integration = ExternalMCPIntegration()
        integration._record_pending_consent("u1", "google", "https://a/1")

        integration.drain_pending_consent("u1")
        second = integration.drain_pending_consent("u1")

        assert second == []

    def test_dedupe_by_provider(self):
        """Two tools needing the same provider produce one consent prompt."""
        integration = ExternalMCPIntegration()
        integration._record_pending_consent("u1", "google", "https://a/1")
        integration._record_pending_consent("u1", "google", "https://a/1")

        drained = integration.drain_pending_consent("u1")

        assert len(drained) == 1

    def test_per_user_isolation(self):
        integration = ExternalMCPIntegration()
        integration._record_pending_consent("u1", "google", "https://a/1")
        integration._record_pending_consent("u2", "slack", "https://a/2")

        assert integration.drain_pending_consent("u1") == [
            {"provider_id": "google", "authorization_url": "https://a/1"}
        ]
        assert integration.drain_pending_consent("u2") == [
            {"provider_id": "slack", "authorization_url": "https://a/2"}
        ]

    def test_drain_empty_when_no_prompts(self):
        integration = ExternalMCPIntegration()
        assert integration.drain_pending_consent("u-nobody") == []
