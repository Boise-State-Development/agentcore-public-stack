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

    def test_default_inference_params_is_empty(self, model_config: ModelConfig):
        """No default temperature — newer reasoning models reject any value."""
        assert model_config.inference_params == {}

    def test_default_caching_enabled(self, model_config: ModelConfig):
        assert model_config.caching_enabled is True

    def test_default_provider(self, model_config: ModelConfig):
        assert model_config.provider == ModelProvider.BEDROCK

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

    def test_bedrock_config_with_caching_enabled_sets_auto_cache_config(self):
        """Req 1.6 — caching_enabled=True emits a CacheConfig(strategy="auto").
        Strands places cache points per-model and no-ops with a warning for
        models that don't support automatic caching."""
        from strands.models import CacheConfig

        cfg = ModelConfig(caching_enabled=True)
        result = cfg.to_bedrock_config()

        assert result["model_id"] == cfg.model_id
        assert "temperature" not in result  # No default temperature emitted
        assert isinstance(result["cache_config"], CacheConfig)
        assert result["cache_config"].strategy == "auto"

    def test_bedrock_config_without_caching(self):
        """Req 1.6 (negative) — caching disabled → no cache_config key."""
        cfg = ModelConfig(caching_enabled=False)
        result = cfg.to_bedrock_config()

        assert result["model_id"] == cfg.model_id
        assert "cache_config" not in result

    def test_bedrock_config_enables_native_token_count(self):
        """Native Bedrock CountTokens is enabled on the Bedrock path so
        projected_input_tokens / count_tokens() return authoritative counts
        instead of the chars/4 heuristic — the foundation for per-turn context
        attribution. The runtime-role IAM grant landed in #428. Set
        unconditionally (Strands falls back + caches the skip if a model can't
        count), so it holds regardless of caching or inference params."""
        assert ModelConfig().to_bedrock_config()["use_native_token_count"] is True
        assert (
            ModelConfig(caching_enabled=True).to_bedrock_config()["use_native_token_count"]
            is True
        )
        assert (
            ModelConfig(inference_params={"temperature": 0.4})
            .to_bedrock_config()["use_native_token_count"]
            is True
        )

    def test_bedrock_config_emits_temperature_only_when_set(self):
        """Inference params only ride along when explicitly configured."""
        cfg = ModelConfig(inference_params={"temperature": 0.4, "top_p": 0.9})
        result = cfg.to_bedrock_config()

        assert result["temperature"] == 0.4
        assert result["top_p"] == 0.9

    def test_bedrock_config_translates_thinking_to_nested_field(self):
        """Canonical 'thinking' carries an int budget that gets wrapped into the
        Anthropic ``{type, budget_tokens}`` shape under
        additional_request_fields on Bedrock — that's the field name Strands'
        BedrockConfig actually forwards to the Converse API."""
        cfg = ModelConfig(inference_params={"thinking": 4096})
        result = cfg.to_bedrock_config()

        assert result["additional_request_fields"]["thinking"] == {
            "type": "enabled",
            "budget_tokens": 4096,
        }
        assert "thinking" not in result
        assert "additional_model_request_fields" not in result

    def test_bedrock_config_routes_top_k_through_additional_request_fields(self):
        """top_k isn't on the Bedrock Converse standard shape — Strands needs it
        in additional_request_fields or the SDK silently drops it."""
        cfg = ModelConfig(inference_params={"top_k": 40})
        result = cfg.to_bedrock_config()

        assert result["additional_request_fields"]["top_k"] == 40
        assert "top_k" not in result

    def test_bedrock_config_thinking_suppresses_sampling_params(self):
        """Anthropic rejects temperature/top_p/top_k while extended thinking is on,
        so the translator drops them before dispatch."""
        cfg = ModelConfig(
            inference_params={
                "thinking": 2048,
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 40,
                "max_tokens": 8192,
            }
        )
        result = cfg.to_bedrock_config()

        assert "temperature" not in result
        assert "top_p" not in result
        assert "top_k" not in result
        assert result["max_tokens"] == 8192
        assert result["additional_request_fields"]["thinking"]["budget_tokens"] == 2048

    def test_bedrock_config_thinking_disabled_passes_sampling_params_through(self):
        """A 0 / None thinking value is a no-op — sampling params survive."""
        cfg = ModelConfig(
            inference_params={"thinking": 0, "temperature": 0.5, "top_p": 0.8}
        )
        result = cfg.to_bedrock_config()

        # No thinking config is added when thinking is disabled. (Claude models
        # may still carry `additional_request_fields.anthropic_beta` for
        # fine-grained tool streaming — see the dedicated tests below — so we
        # assert thinking absence specifically rather than the whole key.)
        assert "thinking" not in result.get("additional_request_fields", {})
        assert result["temperature"] == 0.5
        assert result["top_p"] == 0.8

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-opus-4-7-20260115-v1:0",
            "us.anthropic.claude-opus-4-6",
            "us.anthropic.claude-sonnet-4-6",
            "claude-mythos-preview",
        ],
    )
    def test_bedrock_thinking_uses_adaptive_shape_on_newer_models(self, model_id):
        """Opus 4.6/4.7, Sonnet 4.6 and Mythos require/recommend adaptive
        thinking. Opus 4.7 rejects `{type:"enabled"}` with a 400, so the
        int budget only signals "on" and the shape is `{type:"adaptive"}`.
        `display:"summarized"` keeps the reasoning trace from going blank
        (Opus 4.7 defaults display to "omitted")."""
        cfg = ModelConfig(model_id=model_id, inference_params={"thinking": 4096})
        result = cfg.to_bedrock_config()

        assert result["additional_request_fields"]["thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }
        assert "budget_tokens" not in result["additional_request_fields"]["thinking"]

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-4-5-20250101-v1:0",
            "claude-3-opus",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
    )
    def test_bedrock_thinking_keeps_legacy_enabled_shape_on_older_models(self, model_id):
        """Older models (Sonnet 4.5, Claude 3, Haiku 4.5) still take the
        legacy `{type:"enabled", budget_tokens:N}` shape — unchanged."""
        cfg = ModelConfig(model_id=model_id, inference_params={"thinking": 4096})
        result = cfg.to_bedrock_config()

        assert result["additional_request_fields"]["thinking"] == {
            "type": "enabled",
            "budget_tokens": 4096,
        }

    def test_bedrock_adaptive_thinking_still_suppresses_sampling_params(self):
        """Anthropic rejects temperature/top_p/top_k while extended thinking
        is on regardless of mode — suppression still fires for adaptive."""
        cfg = ModelConfig(
            model_id="us.anthropic.claude-opus-4-7-20260115-v1:0",
            inference_params={"thinking": 2048, "temperature": 0.7, "top_p": 0.9},
        )
        result = cfg.to_bedrock_config()

        assert "temperature" not in result
        assert "top_p" not in result
        assert result["additional_request_fields"]["thinking"]["type"] == "adaptive"

    def test_bedrock_effort_maps_to_output_config(self):
        """`effort` rides through additional_request_fields as Anthropic's
        top-level `output_config.effort` — not a Converse standard field."""
        cfg = ModelConfig(
            model_id="us.anthropic.claude-opus-4-7-20260115-v1:0",
            inference_params={"effort": "xhigh"},
        )
        result = cfg.to_bedrock_config()

        assert result["additional_request_fields"]["output_config"]["effort"] == "xhigh"
        assert "effort" not in result
        assert "output_config" not in result

    def test_bedrock_effort_and_adaptive_thinking_coexist(self):
        """effort and adaptive thinking are independent knobs — both land
        under additional_request_fields together."""
        cfg = ModelConfig(
            model_id="us.anthropic.claude-opus-4-7-20260115-v1:0",
            inference_params={"thinking": 2048, "effort": "high"},
        )
        result = cfg.to_bedrock_config()

        arf = result["additional_request_fields"]
        assert arf["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert arf["output_config"]["effort"] == "high"

    def test_bedrock_config_coerces_float_max_tokens_to_int(self):
        """JSON-sourced inference params can carry a float (100000.0); the
        Bedrock SDK rejects a float maxTokens, so it must be coerced to int."""
        cfg = ModelConfig(inference_params={"max_tokens": 100000.0, "top_k": 40.0})
        result = cfg.to_bedrock_config()

        assert result["max_tokens"] == 100000
        assert isinstance(result["max_tokens"], int)
        assert result["additional_request_fields"]["top_k"] == 40
        assert isinstance(result["additional_request_fields"]["top_k"], int)

    def test_gemini_config_coerces_float_max_tokens_to_int(self):
        """Coercion applies across providers — Gemini max_output_tokens too."""
        cfg = ModelConfig(
            model_id="gemini-pro", inference_params={"max_tokens": 2048.0}
        )
        result = cfg.to_gemini_config()

        assert result["params"]["max_output_tokens"] == 2048
        assert isinstance(result["params"]["max_output_tokens"], int)

    def test_bedrock_config_drops_unknown_canonical_param(self):
        """Provider translation table silently drops keys it doesn't know."""
        cfg = ModelConfig(inference_params={"reasoning_effort": "high"})
        result = cfg.to_bedrock_config()

        assert "reasoning_effort" not in result

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

    # -- MCP Apps (SEP-1865) fine-grained tool streaming ---------------------

    _FGTS_BETA = "fine-grained-tool-streaming-2025-05-14"

    def test_bedrock_config_adds_fine_grained_tool_streaming_for_claude(self, monkeypatch):
        """Claude model + MCP Apps host on → fine-grained tool streaming beta
        is added so Bedrock streams tool input incrementally (not buffered)."""
        monkeypatch.setenv("AGENTCORE_MCP_APPS_HOST_ENABLED", "true")
        cfg = ModelConfig(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")
        result = cfg.to_bedrock_config()

        assert result["additional_request_fields"]["anthropic_beta"] == [self._FGTS_BETA]

    def test_bedrock_config_no_fine_grained_streaming_when_mcp_apps_disabled(self, monkeypatch):
        """MCP Apps host off → the beta is not added (opted-out environments
        keep Anthropic's default JSON-validated tool input)."""
        monkeypatch.setenv("AGENTCORE_MCP_APPS_HOST_ENABLED", "false")
        cfg = ModelConfig(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")
        result = cfg.to_bedrock_config()

        assert "anthropic_beta" not in result.get("additional_request_fields", {})

    def test_bedrock_config_no_fine_grained_streaming_for_non_claude(self, monkeypatch):
        """Non-Claude Bedrock model → the Anthropic-only beta is never added
        (would be rejected by other providers' models)."""
        monkeypatch.setenv("AGENTCORE_MCP_APPS_HOST_ENABLED", "true")
        cfg = ModelConfig(model_id="us.amazon.nova-pro-v1:0")
        result = cfg.to_bedrock_config()

        assert "anthropic_beta" not in result.get("additional_request_fields", {})

    def test_bedrock_config_fine_grained_streaming_merges_with_thinking(self, monkeypatch):
        """The beta is added alongside an existing `additional_request_fields`
        block (e.g. thinking) rather than clobbering it."""
        monkeypatch.setenv("AGENTCORE_MCP_APPS_HOST_ENABLED", "true")
        cfg = ModelConfig(
            model_id="us.anthropic.claude-sonnet-4-6",
            inference_params={"thinking": 2048, "max_tokens": 8192},
        )
        result = cfg.to_bedrock_config()

        arf = result["additional_request_fields"]
        assert arf["anthropic_beta"] == [self._FGTS_BETA]
        assert "thinking" in arf


# ---------------------------------------------------------------------------
# Req 1.8–1.9 — to_openai_config / to_gemini_config
# ---------------------------------------------------------------------------
class TestToOpenAIConfig:
    """Validates: Requirements 1.8, 1.9"""

    def test_openai_config_basic(self):
        """Req 1.8 — dict with model_id and params.temperature."""
        cfg = ModelConfig(model_id="gpt-4o", inference_params={"temperature": 0.5})
        result = cfg.to_openai_config()

        assert result["model_id"] == "gpt-4o"
        assert result["params"]["temperature"] == 0.5

    def test_openai_config_with_max_tokens(self):
        """Req 1.9 — max_tokens appears in params."""
        cfg = ModelConfig(model_id="gpt-4o", inference_params={"max_tokens": 1024})
        result = cfg.to_openai_config()

        assert result["params"]["max_tokens"] == 1024

    def test_openai_config_without_inference_params_omits_params_block(self):
        cfg = ModelConfig(model_id="gpt-4o")
        result = cfg.to_openai_config()

        assert "params" not in result

    def test_openai_config_drops_top_k(self):
        """OpenAI doesn't support top_k — translation table drops it silently."""
        cfg = ModelConfig(model_id="gpt-4o", inference_params={"top_k": 40})
        result = cfg.to_openai_config()

        assert "params" not in result


class TestToMantleConfig:
    """Bedrock Mantle — OpenAI-wire-compatible config translation."""

    def test_mantle_config_basic(self):
        cfg = ModelConfig(
            model_id="openai.gpt-oss-120b",
            provider=ModelProvider.MANTLE,
            inference_params={"temperature": 0.5},
        )
        result = cfg.to_mantle_config()

        assert result["model_id"] == "openai.gpt-oss-120b"
        assert result["params"]["temperature"] == 0.5

    def test_mantle_config_without_inference_params_omits_params_block(self):
        cfg = ModelConfig(
            model_id="qwen.qwen3-coder-30b-a3b-instruct",
            provider=ModelProvider.MANTLE,
        )
        result = cfg.to_mantle_config()

        assert "params" not in result

    def test_mantle_config_drops_top_k(self):
        """OpenAI wire protocol has no top_k — translation drops it silently."""
        cfg = ModelConfig(
            model_id="openai.gpt-oss-120b",
            provider=ModelProvider.MANTLE,
            inference_params={"top_k": 40},
        )
        result = cfg.to_mantle_config()

        assert "params" not in result

    def test_explicit_mantle_provider_wins_over_auto_detect(self):
        """Mantle is never auto-detected; explicit provider must stick even
        for ids that look like other providers' (e.g. `openai.` prefix)."""
        cfg = ModelConfig(model_id="openai.gpt-oss-120b", provider=ModelProvider.MANTLE)

        assert cfg.get_provider() == ModelProvider.MANTLE

    def test_from_params_accepts_mantle(self):
        cfg = ModelConfig.from_params(
            model_id="qwen.qwen3-coder-30b-a3b-instruct", provider="mantle"
        )

        assert cfg.get_provider() == ModelProvider.MANTLE


class TestToGeminiConfig:
    """Validates: Requirement 1.9"""

    def test_gemini_config_basic(self):
        cfg = ModelConfig(model_id="gemini-pro", inference_params={"temperature": 0.3})
        result = cfg.to_gemini_config()

        assert result["model_id"] == "gemini-pro"
        assert result["params"]["temperature"] == 0.3

    def test_gemini_config_with_max_tokens(self):
        """Req 1.9 — max_tokens → max_output_tokens in params."""
        cfg = ModelConfig(model_id="gemini-pro", inference_params={"max_tokens": 2048})
        result = cfg.to_gemini_config()

        assert result["params"]["max_output_tokens"] == 2048

    def test_gemini_config_without_inference_params_omits_params_block(self):
        cfg = ModelConfig(model_id="gemini-pro")
        result = cfg.to_gemini_config()

        assert "params" not in result


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
            "caching_enabled",
            "provider",
            "inference_params",
            "mantle_endpoint_path",
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
        assert cfg.inference_params == default.inference_params
        assert cfg.caching_enabled == default.caching_enabled
        assert cfg.provider == default.provider

    def test_from_params_custom_values(self):
        cfg = ModelConfig.from_params(
            model_id="gpt-4o",
            caching_enabled=False,
            provider="openai",
            inference_params={"temperature": 0.2, "max_tokens": 512},
        )

        assert cfg.model_id == "gpt-4o"
        assert cfg.inference_params == {"temperature": 0.2, "max_tokens": 512}
        assert cfg.caching_enabled is False
        assert cfg.provider == ModelProvider.OPENAI

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
