"""Tests for AgentCoreIdentityClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.oauth.agentcore_identity import (
    AgentCoreIdentityClient,
    TokenResult,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
)


class TestCustomParametersFor:
    """Merge of vendor baseline + admin extras. Baseline is non-negotiable
    because admins can't safely turn off documented requirements (e.g.
    Google's `access_type=offline` for refresh tokens)."""

    def test_google_baseline_alone(self) -> None:
        assert custom_parameters_for("google") == {"access_type": "offline"}

    def test_google_match_is_case_insensitive(self) -> None:
        # OAuthProviderType.GOOGLE.value is "google", but defensive against
        # callers that pass the upper-case enum name.
        assert custom_parameters_for("Google") == {"access_type": "offline"}

    @pytest.mark.parametrize(
        "vendor", ["microsoft", "github", "canvas", "custom", "unknown"]
    )
    def test_other_vendors_with_no_extras_return_none(self, vendor: str) -> None:
        # Per the AgentCore Identity docs, only Google requires baseline
        # extras today. Returning None lets callers pass through.
        assert custom_parameters_for(vendor) is None

    def test_none_returns_none(self) -> None:
        assert custom_parameters_for(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert custom_parameters_for("") is None

    def test_admin_extras_merged_with_google_baseline(self) -> None:
        # Admin can add domain restriction / prompt without losing
        # the access_type=offline requirement.
        result = custom_parameters_for(
            "google", {"hd": "mycompany.com", "prompt": "consent"}
        )
        assert result == {
            "access_type": "offline",
            "hd": "mycompany.com",
            "prompt": "consent",
        }

    def test_admin_cannot_override_baseline_keys(self) -> None:
        # Admin-supplied access_type=online is silently superseded by the
        # baseline. This is intentional — overriding it would silently
        # break refresh tokens, the exact bug we hardcoded against.
        result = custom_parameters_for("google", {"access_type": "online"})
        assert result == {"access_type": "offline"}

    def test_admin_extras_only_for_non_baseline_vendor(self) -> None:
        # Vendors with no baseline still pass through admin extras.
        result = custom_parameters_for("github", {"prompt": "consent"})
        assert result == {"prompt": "consent"}

    def test_empty_admin_extras_treated_as_none(self) -> None:
        assert custom_parameters_for("microsoft", {}) is None
        assert custom_parameters_for("microsoft", None) is None


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
        "apis.shared.oauth.agentcore_identity.IdentityClient"
    ) as sdk_cls:
        yield sdk_cls


@pytest.fixture
def mock_context():
    """Patch BedrockAgentCoreContext accessors used inside the wrapper."""
    with patch(
        "apis.shared.oauth.agentcore_identity.BedrockAgentCoreContext"
    ) as ctx:
        ctx.get_workload_access_token.return_value = "workload-token-xyz"
        ctx.get_oauth2_callback_url.return_value = "https://cb.example.com/oauth"
        yield ctx


class TestGetTokenForUserCacheHit:
    @pytest.mark.asyncio
    async def test_returns_access_token_when_vault_has_token(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token = AsyncMock(return_value="ya29.access-token")

        client = AgentCoreIdentityClient(region="us-east-1")
        result = await client.get_token_for_user(
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
        # Wrapper appends provider_id to the callback so the /oauth-complete
        # page knows which provider to dismiss in the consent banner.
        assert kwargs["callback_url"] == (
            "https://cb.example.com/oauth?provider_id=google-workspace"
        )
        assert kwargs["force_authentication"] is False

    @pytest.mark.asyncio
    async def test_explicit_callback_url_overrides_context(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token = AsyncMock(return_value="t")

        client = AgentCoreIdentityClient()
        await client.get_token_for_user(
            provider_name="p",
            scopes=["s"],
            callback_url="https://override.example.com/cb",
        )

        kwargs = sdk_instance.get_token.call_args.kwargs
        assert kwargs["callback_url"] == (
            "https://override.example.com/cb?provider_id=p"
        )


class TestGetTokenForUserConsentRequired:
    @pytest.mark.asyncio
    async def test_returns_authorization_url_when_sdk_invokes_callback(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        """When the user needs to consent, the SDK calls on_auth_url with the
        consent URL. The wrapper captures it and returns a TokenResult with
        authorization_url set rather than raising."""
        sdk_instance = mock_identity_sdk.return_value

        async def fake_get_token(**kwargs):
            kwargs["on_auth_url"]("https://accounts.example.com/consent?x=1")
            return None

        sdk_instance.get_token = AsyncMock(side_effect=fake_get_token)

        client = AgentCoreIdentityClient()
        result = await client.get_token_for_user(provider_name="p", scopes=["s"])

        assert result.requires_consent is True
        assert result.authorization_url == "https://accounts.example.com/consent?x=1"
        assert result.access_token is None

    @pytest.mark.asyncio
    async def test_auth_url_takes_precedence_over_stale_token(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        """Defensive: if the SDK both returns a token AND invokes on_auth_url,
        we treat consent-required as the authoritative signal."""
        sdk_instance = mock_identity_sdk.return_value

        async def fake_get_token(**kwargs):
            kwargs["on_auth_url"]("https://consent.example.com")
            return "stale-token"

        sdk_instance.get_token = AsyncMock(side_effect=fake_get_token)

        client = AgentCoreIdentityClient()
        result = await client.get_token_for_user(provider_name="p", scopes=["s"])

        assert result.requires_consent is True
        assert result.authorization_url == "https://consent.example.com"


class TestGetTokenForUserErrors:
    @pytest.mark.asyncio
    async def test_raises_when_no_workload_token_on_context(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        mock_context.get_workload_access_token.return_value = None

        client = AgentCoreIdentityClient()
        with pytest.raises(WorkloadTokenUnavailableError):
            await client.get_token_for_user(provider_name="p", scopes=["s"])

    @pytest.mark.asyncio
    async def test_raises_when_sdk_returns_nothing_and_no_auth_url(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token = AsyncMock(return_value=None)

        client = AgentCoreIdentityClient()
        with pytest.raises(RuntimeError, match="neither a token nor"):
            await client.get_token_for_user(provider_name="p", scopes=["s"])

    @pytest.mark.asyncio
    async def test_force_authentication_flag_is_forwarded(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token = AsyncMock(return_value="t")

        client = AgentCoreIdentityClient()
        await client.get_token_for_user(
            provider_name="p", scopes=["s"], force_authentication=True
        )

        kwargs = sdk_instance.get_token.call_args.kwargs
        assert kwargs["force_authentication"] is True

    @pytest.mark.asyncio
    async def test_custom_parameters_are_forwarded_to_sdk(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        # AgentCore Identity needs Google's `access_type=offline` forwarded
        # via the SDK's `custom_parameters` kwarg — without it Google
        # issues no refresh token and the vault entry expires after 1hr.
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token = AsyncMock(return_value="t")

        client = AgentCoreIdentityClient()
        await client.get_token_for_user(
            provider_name="p",
            scopes=["s"],
            custom_parameters={"access_type": "offline"},
        )

        kwargs = sdk_instance.get_token.call_args.kwargs
        assert kwargs["custom_parameters"] == {"access_type": "offline"}

    @pytest.mark.asyncio
    async def test_custom_parameters_omitted_when_none_or_empty(
        self, mock_identity_sdk: MagicMock, mock_context: MagicMock
    ) -> None:
        # The SDK only ships the kwarg when we actually have something to
        # send; absent custom_parameters should not appear in the call.
        sdk_instance = mock_identity_sdk.return_value
        sdk_instance.get_token = AsyncMock(return_value="t")

        client = AgentCoreIdentityClient()
        await client.get_token_for_user(provider_name="p", scopes=["s"])
        assert "custom_parameters" not in sdk_instance.get_token.call_args.kwargs

        sdk_instance.get_token.reset_mock()
        await client.get_token_for_user(
            provider_name="p", scopes=["s"], custom_parameters={}
        )
        assert "custom_parameters" not in sdk_instance.get_token.call_args.kwargs
