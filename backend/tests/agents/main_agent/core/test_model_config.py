"""
Tests for ModelConfig — Requirements 1.1–1.12.

Covers default initialization, provider auto-detection, explicit provider override,
provider-specific config dicts, to_dict, from_params with defaults and invalid provider.
"""

import pytest

from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig


# ---------------------------------------------------------------------------
# Req 1.1 — Default values
# ---------------------------------------------------------------------------
class TestModelConfigDefaults:
    """Validates: Requirement 1.1"""

    def test_default_model_id(self, model_config: ModelConfig):
        assert model_config.model_id == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_default_temperature(self, model_config: ModelConfig):
        assert model_config.temperature == 0.7

    def test_default_caching_enabled(self, model_config: ModelConfig):
        assert model_config.caching_enabled is True

    def test_default_provider(self, model_config: ModelConfig):
        assert model_config.provider == ModelProvider.BEDROCK

    def test_default_max_tokens(self, model_config: ModelConfig):
        assert model_config.max_tokens is None

    def test_default_retry_config(self, model_config: ModelConfig):
        assert model_config.retry_config is None


# ---------------------------------------------------------------------------
# Req 1.2–1.4 — get_provider auto-detection
# ---------------------------------------------------------------------------
class TestGetProviderAutoDetect:
    """Validates: Requirements 1.2, 1.3, 1.4"""

    @pytest.mark.parametrize("model_id", ["gpt-4o", "gpt-3.5-turbo", "GPT-4"])
    def test_gpt_prefix_returns_openai(self, model_id: str):
        """Req 1.2 — model IDs starting with 'gpt-' → OPENAI."""
        cfg = ModelConfig(model_id=model_id)
        assert cfg.get_provider() == ModelProvider.OPENAI

    @pytest.mark.parametrize("model_id", ["o1-preview", "o1-mini"])
    def test_o1_prefix_returns_openai(self, model_id: str):
        """Req 1.2 — model IDs starting with 'o1-' → OPENAI."""
        cfg = ModelConfig(model_id=model_id)
        assert cfg.get_provider() == ModelProvider.OPENAI

    @pytest.mark.parametrize("model_id", ["gemini-pro", "gemini-1.5-flash"])
    def test_gemini_prefix_returns_gemini(self, model_id: str):
        """Req 1.3 — model IDs starting with 'gemini-' → GEMINI."""
        cfg = ModelConfig(model_id=model_id)
        assert cfg.get_provider() == ModelProvider.GEMINI

    @pytest.mark.parametrize(
        "model_id",
        [
            "anthropic.claude-3-sonnet",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "claude-3-opus",
        ],
    )
    def test_anthropic_or_claude_returns_bedrock(self, model_id: str):
        """Req 1.4 — model IDs containing 'anthropic' or 'claude' → BEDROCK."""
        cfg = ModelConfig(model_id=model_id)
        assert cfg.get_provider() == ModelProvider.BEDROCK


# ---------------------------------------------------------------------------
# Req 1.5 — Explicit provider override
# ---------------------------------------------------------------------------
class TestExplicitProviderOverride:
    """Validates: Requirement 1.5"""

    def test_explicit_openai_overrides_bedrock_model_id(self):
        cfg = ModelConfig(
            model_id="anthropic.claude-3-sonnet",
            provider=ModelProvider.OPENAI,
        )
        assert cfg.get_provider() == ModelProvider.OPENAI

    def test_explicit_gemini_overrides_gpt_model_id(self):
        cfg = ModelConfig(
            model_id="gpt-4o",
            provider=ModelProvider.GEMINI,
        )
        assert cfg.get_provider() == ModelProvider.GEMINI


# ---------------------------------------------------------------------------
# Req 1.6 — to_bedrock_config with caching
# ---------------------------------------------------------------------------
class TestToBedrockConfig:
    """Validates: Requirements 1.6, 1.7"""

    def test_bedrock_config_with_caching_disabled_due_to_bedrock_limitation(self):
        """Req 1.6 — caching_enabled=True but cache_config omitted due to
        Bedrock limitation with non-PDF document blocks. See model_config.py TODO."""
        cfg = ModelConfig(caching_enabled=True)
        result = cfg.to_bedrock_config()

        assert result["model_id"] == cfg.model_id
        assert result["temperature"] == cfg.temperature
        assert "cache_config" not in result

    def test_bedrock_config_without_caching(self):
        """Req 1.6 (negative) — caching disabled → no cache_config key."""
        cfg = ModelConfig(caching_enabled=False)
        result = cfg.to_bedrock_config()

        assert result["model_id"] == cfg.model_id
        assert result["temperature"] == cfg.temperature
        assert "cache_config" not in result

    def test_bedrock_config_with_retry(self, retry_config: RetryConfig):
        """Req 1.7 — RetryConfig present → boto_client_config in output."""
        cfg = ModelConfig(caching_enabled=False, retry_config=retry_config)
        result = cfg.to_bedrock_config()

        assert "boto_client_config" in result

    def test_bedrock_config_without_retry(self):
        """Req 1.7 (negative) — no RetryConfig → no boto_client_config."""
        cfg = ModelConfig(caching_enabled=False, retry_config=None)
        result = cfg.to_bedrock_config()

        assert "boto_client_config" not in result


# ---------------------------------------------------------------------------
# Req 1.8–1.9 — to_openai_config / to_gemini_config
# ---------------------------------------------------------------------------
class TestToOpenAIConfig:
    """Validates: Requirements 1.8, 1.9"""

    def test_openai_config_basic(self):
        """Req 1.8 — dict with model_id and params.temperature."""
        cfg = ModelConfig(model_id="gpt-4o", temperature=0.5)
        result = cfg.to_openai_config()

        assert result["model_id"] == "gpt-4o"
        assert result["params"]["temperature"] == 0.5

    def test_openai_config_with_max_tokens(self):
        """Req 1.9 — max_tokens appears in params."""
        cfg = ModelConfig(model_id="gpt-4o", max_tokens=1024)
        result = cfg.to_openai_config()

        assert result["params"]["max_tokens"] == 1024

    def test_openai_config_without_max_tokens(self):
        cfg = ModelConfig(model_id="gpt-4o", max_tokens=None)
        result = cfg.to_openai_config()

        assert "max_tokens" not in result["params"]


class TestToGeminiConfig:
    """Validates: Requirement 1.9"""

    def test_gemini_config_basic(self):
        cfg = ModelConfig(model_id="gemini-pro", temperature=0.3)
        result = cfg.to_gemini_config()

        assert result["model_id"] == "gemini-pro"
        assert result["params"]["temperature"] == 0.3

    def test_gemini_config_with_max_tokens(self):
        """Req 1.9 — max_tokens → max_output_tokens in params."""
        cfg = ModelConfig(model_id="gemini-pro", max_tokens=2048)
        result = cfg.to_gemini_config()

        assert result["params"]["max_output_tokens"] == 2048

    def test_gemini_config_without_max_tokens(self):
        cfg = ModelConfig(model_id="gemini-pro", max_tokens=None)
        result = cfg.to_gemini_config()

        assert "max_output_tokens" not in result["params"]


# ---------------------------------------------------------------------------
# Req 1.10 — to_dict
# ---------------------------------------------------------------------------
class TestToDict:
    """Validates: Requirement 1.10"""

    def test_to_dict_resolves_provider(self):
        """Provider in dict comes from get_provider, not the raw field."""
        cfg = ModelConfig(model_id="gpt-4o")
        d = cfg.to_dict()

        assert d["provider"] == "openai"

    def test_to_dict_keys(self, model_config: ModelConfig):
        d = model_config.to_dict()
        assert set(d.keys()) == {
            "model_id",
            "temperature",
            "caching_enabled",
            "provider",
            "max_tokens",
        }


# ---------------------------------------------------------------------------
# Req 1.11 — from_params with defaults
# ---------------------------------------------------------------------------
class TestFromParams:
    """Validates: Requirements 1.11, 1.12"""

    def test_from_params_all_defaults(self):
        """Req 1.11 — omitting all params yields default config."""
        cfg = ModelConfig.from_params()
        default = ModelConfig()

        assert cfg.model_id == default.model_id
        assert cfg.temperature == default.temperature
        assert cfg.caching_enabled == default.caching_enabled
        assert cfg.provider == default.provider

    def test_from_params_custom_values(self):
        cfg = ModelConfig.from_params(
            model_id="gpt-4o",
            temperature=0.2,
            caching_enabled=False,
            provider="openai",
            max_tokens=512,
        )

        assert cfg.model_id == "gpt-4o"
        assert cfg.temperature == 0.2
        assert cfg.caching_enabled is False
        assert cfg.provider == ModelProvider.OPENAI
        assert cfg.max_tokens == 512

    def test_from_params_invalid_provider_defaults_to_bedrock(self):
        """Req 1.12 — invalid provider string → BEDROCK."""
        cfg = ModelConfig.from_params(provider="not-a-provider")
        assert cfg.provider == ModelProvider.BEDROCK


# ---------------------------------------------------------------------------
# Req 2.1 — RetryConfig default values
# ---------------------------------------------------------------------------
class TestRetryConfigDefaults:
    """Validates: Requirement 2.1"""

    def test_default_boto_max_attempts(self, retry_config: RetryConfig):
        assert retry_config.boto_max_attempts == 3

    def test_default_sdk_max_attempts(self, retry_config: RetryConfig):
        assert retry_config.sdk_max_attempts == 4

    def test_default_sdk_initial_delay(self, retry_config: RetryConfig):
        assert retry_config.sdk_initial_delay == 2.0

    def test_default_sdk_max_delay(self, retry_config: RetryConfig):
        assert retry_config.sdk_max_delay == 16.0

    def test_default_boto_retry_mode(self, retry_config: RetryConfig):
        assert retry_config.boto_retry_mode == "standard"

    def test_default_connect_timeout(self, retry_config: RetryConfig):
        assert retry_config.connect_timeout == 5

    def test_default_read_timeout(self, retry_config: RetryConfig):
        assert retry_config.read_timeout == 120


# ---------------------------------------------------------------------------
# Req 2.2 — RetryConfig.from_env reads environment variables
# ---------------------------------------------------------------------------
class TestRetryConfigFromEnvWithVars:
    """Validates: Requirement 2.2"""

    def test_from_env_reads_boto_max_attempts(self, monkeypatch):
        monkeypatch.setenv("RETRY_BOTO_MAX_ATTEMPTS", "10")
        cfg = RetryConfig.from_env()
        assert cfg.boto_max_attempts == 10

    def test_from_env_reads_sdk_max_attempts(self, monkeypatch):
        monkeypatch.setenv("RETRY_SDK_MAX_ATTEMPTS", "7")
        cfg = RetryConfig.from_env()
        assert cfg.sdk_max_attempts == 7

    def test_from_env_reads_sdk_initial_delay(self, monkeypatch):
        monkeypatch.setenv("RETRY_SDK_INITIAL_DELAY", "5.5")
        cfg = RetryConfig.from_env()
        assert cfg.sdk_initial_delay == 5.5

    def test_from_env_reads_sdk_max_delay(self, monkeypatch):
        monkeypatch.setenv("RETRY_SDK_MAX_DELAY", "30.0")
        cfg = RetryConfig.from_env()
        assert cfg.sdk_max_delay == 30.0

    def test_from_env_reads_boto_mode(self, monkeypatch):
        monkeypatch.setenv("RETRY_BOTO_MODE", "adaptive")
        cfg = RetryConfig.from_env()
        assert cfg.boto_retry_mode == "adaptive"

    def test_from_env_reads_connect_timeout(self, monkeypatch):
        monkeypatch.setenv("RETRY_CONNECT_TIMEOUT", "15")
        cfg = RetryConfig.from_env()
        assert cfg.connect_timeout == 15

    def test_from_env_reads_read_timeout(self, monkeypatch):
        monkeypatch.setenv("RETRY_READ_TIMEOUT", "300")
        cfg = RetryConfig.from_env()
        assert cfg.read_timeout == 300

    def test_from_env_reads_all_vars(self, monkeypatch):
        """Set all env vars at once and verify the full config."""
        monkeypatch.setenv("RETRY_BOTO_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("RETRY_BOTO_MODE", "legacy")
        monkeypatch.setenv("RETRY_CONNECT_TIMEOUT", "10")
        monkeypatch.setenv("RETRY_READ_TIMEOUT", "60")
        monkeypatch.setenv("RETRY_SDK_MAX_ATTEMPTS", "6")
        monkeypatch.setenv("RETRY_SDK_INITIAL_DELAY", "3.0")
        monkeypatch.setenv("RETRY_SDK_MAX_DELAY", "24.0")

        cfg = RetryConfig.from_env()

        assert cfg.boto_max_attempts == 5
        assert cfg.boto_retry_mode == "legacy"
        assert cfg.connect_timeout == 10
        assert cfg.read_timeout == 60
        assert cfg.sdk_max_attempts == 6
        assert cfg.sdk_initial_delay == 3.0
        assert cfg.sdk_max_delay == 24.0


# ---------------------------------------------------------------------------
# Req 2.3 — RetryConfig.from_env returns defaults when no env vars set
# ---------------------------------------------------------------------------
class TestRetryConfigFromEnvDefaults:
    """Validates: Requirement 2.3"""

    def test_from_env_defaults_without_env_vars(self, monkeypatch):
        """When no RETRY_* env vars are set, from_env returns default values."""
        # Ensure none of the retry env vars are present
        for var in (
            "RETRY_BOTO_MAX_ATTEMPTS",
            "RETRY_BOTO_MODE",
            "RETRY_CONNECT_TIMEOUT",
            "RETRY_READ_TIMEOUT",
            "RETRY_SDK_MAX_ATTEMPTS",
            "RETRY_SDK_INITIAL_DELAY",
            "RETRY_SDK_MAX_DELAY",
        ):
            monkeypatch.delenv(var, raising=False)

        cfg = RetryConfig.from_env()
        default = RetryConfig()

        assert cfg.boto_max_attempts == default.boto_max_attempts
        assert cfg.boto_retry_mode == default.boto_retry_mode
        assert cfg.connect_timeout == default.connect_timeout
        assert cfg.read_timeout == default.read_timeout
        assert cfg.sdk_max_attempts == default.sdk_max_attempts
        assert cfg.sdk_initial_delay == default.sdk_initial_delay
        assert cfg.sdk_max_delay == default.sdk_max_delay
