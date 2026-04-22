"""Tests for AgentCoreIdentityClient."""

from unittest.mock import MagicMock, patch

import pytest

from agents.main_agent.integrations.agentcore_identity import (
    AgentCoreIdentityClient,
    TokenResult,
    WorkloadTokenUnavailableError,
)


class TestTokenResult:
    def test_access_token_only_is_valid(self) -> None:
        result = TokenResult(access_token="abc")
        assert result.access_token == "abc"
        assert result.authorization_url is None
        assert result.requires_consent is False

    def test_authorization_url_only_is_valid(self) -> None:
        result = TokenResult(authorization_url="https://example.com/auth")
        assert result.requires_consent is True

    def test_both_populated_raises(self) -> None:
        with pytest.raises(ValueError):
            TokenResult(access_token="a", authorization_url="https://example.com")

    def test_neither_populated_raises(self) -> None:
        with pytest.raises(ValueError):
            TokenResult()


@pytest.fixture
def mock_identity_sdk():
    """Patch the IdentityClient class used inside the wrapper."""
    with patch(
        "agents.main_agent.integrations.agentcore_identity.IdentityClient"
    ) as sdk_cls:
        yield sdk_cls


@pytest.fixture
def mock_context():
    """Patch BedrockAgentCoreContext accessors used inside the wrapper."""
    with patch(
        "agents.main_agent.integrations.agentcore_identity.BedrockAgentCoreContext"
    ) as ctx:
        ctx.get_workload_access_token.return_value = "workload-token-xyz"
        ctx.get_oauth2_callback_url.return_value = "https://cb.example.com/oauth"
        yield ctx


class TestGetTokenForUserCacheHit:
    def test_returns_access_token_when_vault_has_token(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token.return_value = "ya29.access-token"

        client = AgentCoreIdentityClient(region="us-east-1")
        result = client.get_token_for_user(
            provider_name="google-workspace", scopes=["openid"]
        )

        assert result.access_token == "ya29.access-token"
        assert result.requires_consent is False

        sdk_instance.get_token.assert_called_once()
        kwargs = sdk_instance.get_token.call_args.kwargs
        assert kwargs["provider_name"] == "google-workspace"
        assert kwargs["scopes"] == ["openid"]
        assert kwargs["auth_flow"] == "USER_FEDERATION"
        assert kwargs["agent_identity_token"] == "workload-token-xyz"
        assert kwargs["callback_url"] == "https://cb.example.com/oauth"
        assert kwargs["force_authentication"] is False

    def test_explicit_callback_url_overrides_context(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token.return_value = "t"

        client = AgentCoreIdentityClient()
        client.get_token_for_user(
            provider_name="p",
            scopes=["s"],
            callback_url="https://override.example.com/cb",
        )

        kwargs = sdk_instance.get_token.call_args.kwargs
        assert kwargs["callback_url"] == "https://override.example.com/cb"


class TestGetTokenForUserConsentRequired:
    def test_returns_authorization_url_when_sdk_invokes_callback(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        """When the user needs to consent, the SDK calls on_auth_url with the
        consent URL. The wrapper captures it and returns a TokenResult with
        authorization_url set rather than raising."""
        sdk_instance = mock_identity_sdk.return_value

        def fake_get_token(**kwargs):
            kwargs["on_auth_url"]("https://accounts.example.com/consent?x=1")
            return None

        sdk_instance.get_token.side_effect = fake_get_token

        client = AgentCoreIdentityClient()
        result = client.get_token_for_user(provider_name="p", scopes=["s"])

        assert result.requires_consent is True
        assert result.authorization_url == "https://accounts.example.com/consent?x=1"
        assert result.access_token is None

    def test_auth_url_takes_precedence_over_stale_token(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        """Defensive: if the SDK both returns a token AND invokes on_auth_url,
        we treat consent-required as the authoritative signal."""
        sdk_instance = mock_identity_sdk.return_value

        def fake_get_token(**kwargs):
            kwargs["on_auth_url"]("https://consent.example.com")
            return "stale-token"

        sdk_instance.get_token.side_effect = fake_get_token

        client = AgentCoreIdentityClient()
        result = client.get_token_for_user(provider_name="p", scopes=["s"])

        assert result.requires_consent is True
        assert result.authorization_url == "https://consent.example.com"


class TestGetTokenForUserErrors:
    def test_raises_when_no_workload_token_on_context(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        mock_context.get_workload_access_token.return_value = None

        client = AgentCoreIdentityClient()
        with pytest.raises(WorkloadTokenUnavailableError):
            client.get_token_for_user(provider_name="p", scopes=["s"])

    def test_raises_when_sdk_returns_nothing_and_no_auth_url(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token.return_value = None

        client = AgentCoreIdentityClient()
        with pytest.raises(RuntimeError, match="neither a token nor"):
            client.get_token_for_user(provider_name="p", scopes=["s"])

    def test_force_authentication_flag_is_forwarded(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token.return_value = "t"

        client = AgentCoreIdentityClient()
        client.get_token_for_user(
            provider_name="p", scopes=["s"], force_authentication=True
        )

        kwargs = sdk_instance.get_token.call_args.kwargs
        assert kwargs["force_authentication"] is True
