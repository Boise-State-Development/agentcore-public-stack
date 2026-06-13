"""
Tests for AgentFactory — Requirements 4.1–4.8.

Covers create_agent for Bedrock, OpenAI, and Gemini providers, API key validation,
retry strategy configuration, and SequentialToolExecutor usage.
"""

from unittest.mock import MagicMock, patch

import pytest

from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bedrock_config() -> ModelConfig:
    """ModelConfig that resolves to BEDROCK provider."""
    return ModelConfig(model_id="anthropic.claude-3-sonnet", provider=ModelProvider.BEDROCK)


def _openai_config() -> ModelConfig:
    """ModelConfig that resolves to OPENAI provider."""
    return ModelConfig(model_id="gpt-4o", provider=ModelProvider.OPENAI)


def _gemini_config() -> ModelConfig:
    """ModelConfig that resolves to GEMINI provider."""
    return ModelConfig(model_id="gemini-pro", provider=ModelProvider.GEMINI)


# Common call kwargs shared across tests
_COMMON_KWARGS = dict(
    system_prompt="You are a helpful assistant.",
    tools=[],
    session_manager=MagicMock(),
)


# ---------------------------------------------------------------------------
# Req 4.1 — Bedrock provider creates Agent with BedrockModel
# ---------------------------------------------------------------------------
class TestCreateAgentBedrock:
    """Validates: Requirement 4.1"""

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_bedrock_provider_creates_bedrock_model(self, mock_bedrock_cls, mock_agent_cls):
        from agents.main_agent.core.agent_factory import AgentFactory

        mock_bedrock_instance = MagicMock()
        mock_bedrock_cls.return_value = mock_bedrock_instance

        AgentFactory.create_agent(model_config=_bedrock_config(), **_COMMON_KWARGS)

        mock_bedrock_cls.assert_called_once()
        mock_agent_cls.assert_called_once()
        assert mock_agent_cls.call_args.kwargs["model"] is mock_bedrock_instance


# ---------------------------------------------------------------------------
# Req 4.2 — OpenAI provider with API key creates Agent with OpenAIModel
# ---------------------------------------------------------------------------
class TestCreateAgentOpenAI:
    """Validates: Requirement 4.2"""

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.OpenAIModel")
    def test_openai_provider_creates_openai_model(self, mock_openai_cls, mock_agent_cls, monkeypatch):
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mock_openai_instance = MagicMock()
        mock_openai_cls.return_value = mock_openai_instance

        AgentFactory.create_agent(model_config=_openai_config(), **_COMMON_KWARGS)

        mock_openai_cls.assert_called_once()
        # Verify client_args contains the API key
        call_kwargs = mock_openai_cls.call_args.kwargs
        assert call_kwargs["client_args"]["api_key"] == "sk-test-key"
        mock_agent_cls.assert_called_once()
        assert mock_agent_cls.call_args.kwargs["model"] is mock_openai_instance


# ---------------------------------------------------------------------------
# Bedrock Mantle provider creates Agent with OpenAIModel against the
# regional Mantle endpoint, authenticated with a minted bearer token.
# ---------------------------------------------------------------------------
class TestCreateAgentMantle:
    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.OpenAIModel")
    def test_mantle_provider_creates_openai_model_with_mantle_client(
        self, mock_openai_cls, mock_agent_cls, monkeypatch
    ):
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("AWS_REGION", "us-west-2")
        mock_model_instance = MagicMock()
        mock_openai_cls.return_value = mock_model_instance

        mantle_config = ModelConfig(
            model_id="openai.gpt-oss-120b", provider=ModelProvider.MANTLE
        )
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            return_value="bedrock-api-key-token",
        ) as mock_token:
            AgentFactory.create_agent(model_config=mantle_config, **_COMMON_KWARGS)

        mock_token.assert_called_once_with("us-west-2")
        mock_openai_cls.assert_called_once()
        call_kwargs = mock_openai_cls.call_args.kwargs
        assert call_kwargs["client_args"]["api_key"] == "bedrock-api-key-token"
        assert (
            call_kwargs["client_args"]["base_url"]
            == "https://bedrock-mantle.us-west-2.api.aws/v1"
        )
        assert call_kwargs["model_id"] == "openai.gpt-oss-120b"
        mock_agent_cls.assert_called_once()
        assert mock_agent_cls.call_args.kwargs["model"] is mock_model_instance

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.OpenAIModel")
    def test_mantle_endpoint_path_selects_base_url(
        self, mock_openai_cls, mock_agent_cls, monkeypatch
    ):
        """A model carrying mantle_endpoint_path='/openai/v1' (e.g. Gemma 4)
        must build the base URL on that path, not the default /v1."""
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("AWS_REGION", "us-west-2")
        mantle_config = ModelConfig(
            model_id="google.gemma-4-31b",
            provider=ModelProvider.MANTLE,
            mantle_endpoint_path="/openai/v1",
        )
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            return_value="bedrock-api-key-token",
        ):
            AgentFactory.create_agent(model_config=mantle_config, **_COMMON_KWARGS)

        call_kwargs = mock_openai_cls.call_args.kwargs
        assert (
            call_kwargs["client_args"]["base_url"]
            == "https://bedrock-mantle.us-west-2.api.aws/openai/v1"
        )

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.OpenAIModel")
    def test_mantle_without_credentials_raises(
        self, mock_openai_cls, mock_agent_cls, monkeypatch
    ):
        from agents.main_agent.core.agent_factory import AgentFactory

        mantle_config = ModelConfig(
            model_id="openai.gpt-oss-120b", provider=ModelProvider.MANTLE
        )
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            side_effect=ValueError("No AWS credentials available"),
        ):
            with pytest.raises(ValueError, match="No AWS credentials"):
                AgentFactory.create_agent(model_config=mantle_config, **_COMMON_KWARGS)


# ---------------------------------------------------------------------------
# Req 4.3 — Gemini provider with API key creates Agent with GeminiModel
# ---------------------------------------------------------------------------
class TestCreateAgentGemini:
    """Validates: Requirement 4.3"""

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.GeminiModel")
    def test_gemini_provider_creates_gemini_model(self, mock_gemini_cls, mock_agent_cls, monkeypatch):
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "gemini-test-key")
        mock_gemini_instance = MagicMock()
        mock_gemini_cls.return_value = mock_gemini_instance

        AgentFactory.create_agent(model_config=_gemini_config(), **_COMMON_KWARGS)

        mock_gemini_cls.assert_called_once()
        call_kwargs = mock_gemini_cls.call_args.kwargs
        assert call_kwargs["client_args"]["api_key"] == "gemini-test-key"
        mock_agent_cls.assert_called_once()
        assert mock_agent_cls.call_args.kwargs["model"] is mock_gemini_instance


# ---------------------------------------------------------------------------
# Req 4.4 — Missing OPENAI_API_KEY raises ValueError
# ---------------------------------------------------------------------------
class TestMissingOpenAIKey:
    """Validates: Requirement 4.4"""

    def test_openai_without_api_key_raises(self, monkeypatch):
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            AgentFactory.create_agent(model_config=_openai_config(), **_COMMON_KWARGS)


# ---------------------------------------------------------------------------
# Req 4.5 — Missing GOOGLE_GEMINI_API_KEY raises ValueError
# ---------------------------------------------------------------------------
class TestMissingGeminiKey:
    """Validates: Requirement 4.5"""

    def test_gemini_without_api_key_raises(self, monkeypatch):
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.delenv("GOOGLE_GEMINI_API_KEY", raising=False)

        with pytest.raises(ValueError, match="GOOGLE_GEMINI_API_KEY"):
            AgentFactory.create_agent(model_config=_gemini_config(), **_COMMON_KWARGS)


# ---------------------------------------------------------------------------
# Req 4.6 — Bedrock with retry_config passes ModelRetryStrategy
# ---------------------------------------------------------------------------
class TestRetryStrategy:
    """Validates: Requirements 4.6, 4.7"""

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_bedrock_with_retry_config_passes_retry_strategy(
        self, mock_bedrock_cls, mock_agent_cls
    ):
        """Req 4.6 — retry_config on Bedrock → ModelRetryStrategy passed to Agent."""
        from agents.main_agent.core.agent_factory import AgentFactory

        retry = RetryConfig(sdk_max_attempts=5, sdk_initial_delay=1.0, sdk_max_delay=10.0)
        cfg = ModelConfig(
            model_id="anthropic.claude-3-sonnet",
            provider=ModelProvider.BEDROCK,
            retry_config=retry,
            caching_enabled=False,
        )

        AgentFactory.create_agent(model_config=cfg, **_COMMON_KWARGS)

        agent_kwargs = mock_agent_cls.call_args.kwargs
        assert agent_kwargs["retry_strategy"] is not None

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_bedrock_without_retry_config_passes_none(self, mock_bedrock_cls, mock_agent_cls):
        """Req 4.7 — no retry_config → retry_strategy is None."""
        from agents.main_agent.core.agent_factory import AgentFactory

        cfg = ModelConfig(
            model_id="anthropic.claude-3-sonnet",
            provider=ModelProvider.BEDROCK,
            retry_config=None,
            caching_enabled=False,
        )

        AgentFactory.create_agent(model_config=cfg, **_COMMON_KWARGS)

        agent_kwargs = mock_agent_cls.call_args.kwargs
        assert agent_kwargs["retry_strategy"] is None

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.OpenAIModel")
    def test_openai_retry_strategy_is_none(self, mock_openai_cls, mock_agent_cls, monkeypatch):
        """Req 4.7 — non-Bedrock provider → retry_strategy is None even with retry_config."""
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        cfg = ModelConfig(
            model_id="gpt-4o",
            provider=ModelProvider.OPENAI,
            retry_config=RetryConfig(),
            caching_enabled=False,
        )

        AgentFactory.create_agent(model_config=cfg, **_COMMON_KWARGS)

        agent_kwargs = mock_agent_cls.call_args.kwargs
        assert agent_kwargs["retry_strategy"] is None

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.GeminiModel")
    def test_gemini_retry_strategy_is_none(self, mock_gemini_cls, mock_agent_cls, monkeypatch):
        """Req 4.7 — Gemini provider → retry_strategy is None."""
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "gemini-test-key")
        cfg = ModelConfig(
            model_id="gemini-pro",
            provider=ModelProvider.GEMINI,
            retry_config=RetryConfig(),
            caching_enabled=False,
        )

        AgentFactory.create_agent(model_config=cfg, **_COMMON_KWARGS)

        agent_kwargs = mock_agent_cls.call_args.kwargs
        assert agent_kwargs["retry_strategy"] is None


# ---------------------------------------------------------------------------
# Req 4.8 — SequentialToolExecutor is passed to Agent
# ---------------------------------------------------------------------------
class TestSequentialToolExecutor:
    """Validates: Requirement 4.8"""

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_sequential_tool_executor_passed(self, mock_bedrock_cls, mock_agent_cls):
        from agents.main_agent.core.agent_factory import AgentFactory
        from strands.tools.executors import SequentialToolExecutor

        AgentFactory.create_agent(model_config=_bedrock_config(), **_COMMON_KWARGS)

        agent_kwargs = mock_agent_cls.call_args.kwargs
        assert isinstance(agent_kwargs["tool_executor"], SequentialToolExecutor)
