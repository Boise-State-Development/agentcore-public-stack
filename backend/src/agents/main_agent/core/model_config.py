"""
Model configuration for multi-provider LLM support (Bedrock, OpenAI, Gemini)
"""
import logging
import os
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum

from agents.main_agent.config.constants import EnvVars, Defaults

logger = logging.getLogger(__name__)

# Anthropic "fine-grained tool streaming" beta. WITHOUT it, Bedrock/Anthropic
# BUFFERS a tool_use block's input JSON and flushes every `input_json_delta`
# in one burst once the block is complete; WITH it, the deltas stream as the
# model generates them. Required for MCP Apps (SEP-1865) progressive
# tool-input rendering — see `ModelConfig.to_bedrock_config`.
_FINE_GRAINED_TOOL_STREAMING_BETA = "fine-grained-tool-streaming-2025-05-14"


class ModelProvider(str, Enum):
    """Supported LLM providers"""
    BEDROCK = "bedrock"
    OPENAI = "openai"
    GEMINI = "gemini"
    # Bedrock Mantle — AWS's OpenAI-compatible inference surface for
    # Bedrock-hosted open-weight models (`bedrock-mantle.<region>.api.aws`).
    # Distinct from BEDROCK because it rides the OpenAI wire protocol with a
    # bearer token, not the Converse API with SigV4. Never auto-detected from
    # model_id — admins set it explicitly on the managed model.
    MANTLE = "mantle"


# Canonical param name -> provider-native key path (dot-separated for nested SDK fields).
# A canonical param without an entry here is silently dropped for that provider.
_BEDROCK_PARAM_MAP: Dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    # `top_k` and `thinking` aren't part of the Bedrock Converse standard
    # request shape, so Strands routes them through `additional_request_fields`
    # — anything else gets silently dropped by the SDK before hitting AWS.
    "top_k": "additional_request_fields.top_k",
    "max_tokens": "max_tokens",
    "thinking": "additional_request_fields.thinking",
    # `effort` is Anthropic's top-level `output_config.effort`. It isn't on
    # the Bedrock Converse standard shape either, so it rides through
    # `additionalModelRequestFields` like `thinking`/`top_k`. Soft guidance
    # for thinking depth on adaptive models; also tunes overall token spend.
    "effort": "additional_request_fields.output_config.effort",
}

_OPENAI_PARAM_MAP: Dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_tokens": "max_tokens",
    "reasoning_effort": "reasoning_effort",
}

# Mantle speaks the OpenAI chat-completions protocol, so the canonical->native
# mapping mirrors OpenAI's. Kept separate so Mantle-specific divergence (e.g.
# params some open-weight models reject) has a home without touching OpenAI.
_MANTLE_PARAM_MAP: Dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_tokens": "max_tokens",
    "reasoning_effort": "reasoning_effort",
}

_GEMINI_PARAM_MAP: Dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "max_tokens": "max_output_tokens",
    "thinking": "thinking_config",
}

# Anthropic rejects these sampling params when extended thinking is enabled.
# Bedrock surfaces the same constraint; Gemini's docs are silent so we keep
# them. Suppression happens in `_apply_canonical_params` before dispatch.
_THINKING_INCOMPATIBLE = {"temperature", "top_p", "top_k"}

# Canonical params whose provider-native value must be a plain int. JSON- and
# DynamoDB-sourced inference params arrive untyped (Dict[str, Any]) and can be
# a float (e.g. 100000.0); the Bedrock Converse SDK rejects a float maxTokens
# with a hard boto3 validation error. Coerce at this single translation
# chokepoint. `thinking` is excluded — `_shape_thinking_value` already int()s it.
_INTEGER_CANONICAL_PARAMS: frozenset[str] = frozenset({"max_tokens", "top_k"})

# Union of every canonical key we know how to translate. Used by the request
# merge step to gate user-supplied keys against an allow-list — admins can
# constrain known params with `supportedParams`, but users shouldn't be able
# to bypass that by inventing keys the admin hasn't seen yet (or that the
# provider mapping starts forwarding in a future release).
KNOWN_CANONICAL_PARAMS: frozenset[str] = frozenset(
    set(_BEDROCK_PARAM_MAP)
    | set(_OPENAI_PARAM_MAP)
    | set(_GEMINI_PARAM_MAP)
    | set(_MANTLE_PARAM_MAP)
)


def _set_nested(target: Dict[str, Any], dotted_path: str, value: Any) -> None:
    """Assign ``value`` into ``target`` at a dot-separated key path."""
    keys = dotted_path.split(".")
    cursor = target
    for key in keys[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[keys[-1]] = value


# Bedrock model-id substrings whose Anthropic models require (Opus 4.7) or
# recommend (Opus 4.6, Sonnet 4.6, Mythos) adaptive thinking. On these,
# `{type: "enabled", budget_tokens: N}` is rejected (4.7) or deprecated; the
# shape is `{type: "adaptive"}` and depth is governed by the `effort` param.
# Substring match — real ids are inference-profile-prefixed and date-stamped
# (e.g. `us.anthropic.claude-opus-4-7-20XXXXXX-v1:0`). Unknown ids fall back
# to the legacy enabled shape, which is the safe default for older models.
_BEDROCK_ADAPTIVE_THINKING_MARKERS = (
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-mythos",
)


def _bedrock_uses_adaptive_thinking(model_id: Optional[str]) -> bool:
    """True when the Bedrock model id requires/recommends adaptive thinking."""
    mid = (model_id or "").lower()
    return any(marker in mid for marker in _BEDROCK_ADAPTIVE_THINKING_MARKERS)


def _shape_thinking_value(
    provider_label: str, value: Any, model_id: Optional[str] = None
) -> Any:
    """Wrap a canonical ``thinking`` value into the provider-native object.

    The canonical value is an ``int`` budget (>= 1024), or falsy / 0 to disable.
    On Bedrock, older Anthropic models take ``{type: "enabled", budget_tokens}``;
    Opus 4.6/4.7 and Sonnet 4.6 require ``{type: "adaptive"}`` instead (Opus 4.7
    rejects ``enabled`` with a 400). For adaptive models the int budget only
    signals "thinking on" — depth is controlled by the separate ``effort``
    param — and we set ``display: "summarized"`` so the reasoning trace the
    UI renders isn't blank (Opus 4.7 defaults ``display`` to ``"omitted"``).
    Gemini wants ``{thinking_budget}``. Anything that's already a dict (admin
    pasting raw SDK shape) is passed through verbatim.
    """
    if isinstance(value, dict):
        return value
    if not value:
        return None
    if provider_label == "bedrock":
        if _bedrock_uses_adaptive_thinking(model_id):
            return {"type": "adaptive", "display": "summarized"}
        return {"type": "enabled", "budget_tokens": int(value)}
    if provider_label == "gemini":
        return {"thinking_budget": int(value)}
    return value


def _apply_canonical_params(
    target: Dict[str, Any],
    canonical_params: Dict[str, Any],
    provider_map: Dict[str, str],
    provider_label: str,
    model_id: Optional[str] = None,
) -> None:
    """Translate canonical inference params into provider-native shape.

    Unsupported params are dropped with a warning so callers can layer admin
    defaults plus user overrides without worrying about provider quirks.
    Sampling params that conflict with extended thinking are also dropped
    (Anthropic rejects ``temperature``/``top_p``/``top_k`` while thinking is on).
    """
    thinking_value = canonical_params.get("thinking")
    thinking_enabled = bool(thinking_value) and provider_map.get("thinking") is not None

    for name, value in canonical_params.items():
        if value is None:
            continue
        if thinking_enabled and name in _THINKING_INCOMPATIBLE:
            logger.debug(
                "Dropping '%s' for provider %s because extended thinking is enabled",
                name,
                provider_label,
            )
            continue
        native_path = provider_map.get(name)
        if native_path is None:
            logger.debug(
                "Dropping unsupported inference param '%s' for provider %s",
                name,
                provider_label,
            )
            continue
        if name == "thinking":
            shaped = _shape_thinking_value(provider_label, value, model_id)
            if shaped is None:
                continue
            value = shaped
        elif (
            name in _INTEGER_CANONICAL_PARAMS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            value = int(value)
        _set_nested(target, native_path, value)


@dataclass
class RetryConfig:
    """Configuration for model invocation retry behavior.

    Controls two independent retry layers:
    1. Botocore layer - HTTP-level retries before the Strands SDK sees errors
    2. Strands SDK layer - Agent event loop retries on ModelThrottledException

    When all retries are exhausted, the exception propagates to StreamCoordinator
    which streams it to the client as a conversational error message.

    Can be loaded from environment variables or passed directly.
    """
    # Botocore layer (HTTP-level retries, fires first)
    boto_max_attempts: int = 3          # Total attempts including initial call
    boto_retry_mode: str = "standard"   # "legacy", "standard", or "adaptive"
    connect_timeout: int = 5            # Seconds to wait for connection
    read_timeout: int = 120             # Seconds to wait for response

    # Strands SDK layer (agent event loop retries on ModelThrottledException)
    # Backoff sequence with defaults: 2s, 4s, 8s (3 retries before giving up)
    # Total worst-case wait: ~14s — fast enough for conversational UX
    sdk_max_attempts: int = 4           # Total attempts including initial call
    sdk_initial_delay: float = 2.0      # Seconds before first retry, doubles each retry
    sdk_max_delay: float = 16.0         # Cap on exponential backoff

    @classmethod
    def from_env(cls) -> "RetryConfig":
        """Load configuration from environment variables.

        Environment variables (all optional, defaults shown):
            RETRY_BOTO_MAX_ATTEMPTS=3
            RETRY_BOTO_MODE=standard
            RETRY_CONNECT_TIMEOUT=5
            RETRY_READ_TIMEOUT=120
            RETRY_SDK_MAX_ATTEMPTS=4
            RETRY_SDK_INITIAL_DELAY=2.0
            RETRY_SDK_MAX_DELAY=16.0
        """
        return cls(
            boto_max_attempts=int(os.environ.get(EnvVars.RETRY_BOTO_MAX_ATTEMPTS, str(Defaults.RETRY_BOTO_MAX_ATTEMPTS))),
            boto_retry_mode=os.environ.get(EnvVars.RETRY_BOTO_MODE, Defaults.RETRY_BOTO_MODE),
            connect_timeout=int(os.environ.get(EnvVars.RETRY_CONNECT_TIMEOUT, str(Defaults.RETRY_CONNECT_TIMEOUT))),
            read_timeout=int(os.environ.get(EnvVars.RETRY_READ_TIMEOUT, str(Defaults.RETRY_READ_TIMEOUT))),
            sdk_max_attempts=int(os.environ.get(EnvVars.RETRY_SDK_MAX_ATTEMPTS, str(Defaults.RETRY_SDK_MAX_ATTEMPTS))),
            sdk_initial_delay=float(os.environ.get(EnvVars.RETRY_SDK_INITIAL_DELAY, str(Defaults.RETRY_SDK_INITIAL_DELAY))),
            sdk_max_delay=float(os.environ.get(EnvVars.RETRY_SDK_MAX_DELAY, str(Defaults.RETRY_SDK_MAX_DELAY))),
        )


@dataclass
class ModelConfig:
    """Configuration for multi-provider LLM models.

    Inference params (temperature, top_p, max_tokens, thinking, ...) live in
    ``inference_params`` keyed by canonical name. They're translated into the
    provider-native shape inside ``to_<provider>_config()``. An empty dict
    means "send no inference params" — Anthropic recommends this for newer
    Opus models that reject ``temperature`` outright.
    """
    model_id: str = Defaults.MODEL_ID
    caching_enabled: bool = Defaults.CACHING_ENABLED
    provider: ModelProvider = ModelProvider.BEDROCK
    inference_params: Dict[str, Any] = field(default_factory=dict)
    retry_config: Optional[RetryConfig] = None
    # Bedrock Mantle endpoint path (``/v1`` or ``/openai/v1``). Only consulted
    # on the MANTLE provider path, where it selects the base URL the OpenAI
    # client targets. ``None`` falls back to ``/v1`` in the agent factory.
    mantle_endpoint_path: Optional[str] = None

    def get_provider(self) -> ModelProvider:
        """
        Detect provider from model_id if not explicitly set

        Returns:
            ModelProvider: Detected or configured provider
        """
        # Auto-detect from model_id patterns
        model_lower = self.model_id.lower()

        # Check if provider was explicitly set (not default)
        # If provider is set to non-Bedrock, return it immediately
        if self.provider != ModelProvider.BEDROCK:
            return self.provider

        # If provider is Bedrock (default), check if we should auto-detect
        if model_lower.startswith("gpt-") or model_lower.startswith("o1-"):
            return ModelProvider.OPENAI
        elif model_lower.startswith("gemini-"):
            return ModelProvider.GEMINI
        elif "anthropic" in model_lower or "claude" in model_lower:
            return ModelProvider.BEDROCK

        # Default to configured provider
        return self.provider

    def to_bedrock_config(self) -> Dict[str, Any]:
        """Convert to BedrockModel kwargs, translating canonical inference params."""
        config: Dict[str, Any] = {"model_id": self.model_id}
        _apply_canonical_params(
            config, self.inference_params, _BEDROCK_PARAM_MAP, "bedrock", self.model_id
        )

        # Native Bedrock CountTokens. With this on, Strands' per-turn estimate
        # (BeforeModelCallEvent.projected_input_tokens) and agent.model.count_tokens()
        # return authoritative Bedrock counts instead of the chars/4 heuristic —
        # the foundation for per-turn context attribution (decomposing the
        # otherwise-aggregate inputTokens into system / tools / messages via the
        # CountTokens differential). Every catalog model is Claude family and
        # supports the API; the runtime-role IAM grant landed in #428. Strands
        # falls back to the heuristic and caches the skip if a model ever
        # AccessDenies or doesn't support counting, so this is safe to set
        # unconditionally on the Bedrock path.
        config["use_native_token_count"] = True

        # Bedrock prompt caching. CacheConfig(strategy="auto") lets Strands
        # place cache points per-model: for a model that supports automatic
        # caching it injects a cachePoint on the system/tools/last-user blocks;
        # for one that doesn't it logs a warning and no-ops, so this is safe to
        # set whenever caching is enabled. The earlier SDK blocker — strands PR
        # #1438, where `cachePoint` blocks collided with non-PDF document
        # attachments — was fixed in strands-agents 1.39.0 (we pin 1.40.0).
        # Cache hits are user-visible in the cost/context badge the moment this
        # is on.
        # See: https://github.com/strands-agents/sdk-python/pull/1438
        if self.caching_enabled:
            from strands.models import CacheConfig
            config["cache_config"] = CacheConfig(strategy="auto")

        if self.retry_config:
            from botocore.config import Config as BotocoreConfig
            config["boto_client_config"] = BotocoreConfig(
                retries={
                    "max_attempts": self.retry_config.boto_max_attempts,
                    "mode": self.retry_config.boto_retry_mode,
                },
                connect_timeout=self.retry_config.connect_timeout,
                read_timeout=self.retry_config.read_timeout,
            )

        # MCP Apps (SEP-1865) progressive tool-input streaming. By default
        # Bedrock/Anthropic buffers a tool_use block's input JSON and flushes
        # every `input_json_delta` in one burst once the block is complete —
        # verified directly against `converse_stream`: a ~5.6KB `create_view`
        # input arrives as a single ~1s burst after ~10s of silence, which
        # defeats an App that renders progressively as arguments arrive (the
        # camera tour flashes all at once). Anthropic's fine-grained tool
        # streaming beta emits the input deltas as the model generates them
        # (same input verified spread evenly over ~8s), so the host's
        # `ui_tool_input_partial` relay flows in true real time. Scoped to
        # Bedrock Anthropic (Claude) models and gated on the MCP Apps host
        # flag — the only feature that needs it — so opted-out environments
        # keep Anthropic's default JSON-validated tool input. Merge into any
        # existing `additional_request_fields` (thinking/top_k/effort) rather
        # than clobbering it.
        if "claude" in self.model_id.lower():
            from agents.main_agent.integrations.mcp_apps import (
                is_mcp_apps_host_enabled,
            )

            if is_mcp_apps_host_enabled():
                arf = config.setdefault("additional_request_fields", {})
                betas = list(arf.get("anthropic_beta") or [])
                if _FINE_GRAINED_TOOL_STREAMING_BETA not in betas:
                    betas.append(_FINE_GRAINED_TOOL_STREAMING_BETA)
                arf["anthropic_beta"] = betas

        return config

    def to_openai_config(self) -> Dict[str, Any]:
        """Convert to OpenAIModel kwargs, translating canonical inference params."""
        params: Dict[str, Any] = {}
        _apply_canonical_params(
            params, self.inference_params, _OPENAI_PARAM_MAP, "openai", self.model_id
        )
        config: Dict[str, Any] = {"model_id": self.model_id}
        if params:
            config["params"] = params
        return config

    def to_mantle_config(self) -> Dict[str, Any]:
        """Convert to OpenAIModel kwargs for Bedrock Mantle.

        Mantle is OpenAI-wire-compatible, so the output feeds the same
        Strands ``OpenAIModel`` — only the client (base_url + bearer token)
        differs, and that's the factory's job.
        """
        params: Dict[str, Any] = {}
        _apply_canonical_params(
            params, self.inference_params, _MANTLE_PARAM_MAP, "mantle", self.model_id
        )
        config: Dict[str, Any] = {"model_id": self.model_id}
        if params:
            config["params"] = params
        return config

    def to_gemini_config(self) -> Dict[str, Any]:
        """Convert to GeminiModel kwargs, translating canonical inference params."""
        params: Dict[str, Any] = {}
        _apply_canonical_params(
            params, self.inference_params, _GEMINI_PARAM_MAP, "gemini", self.model_id
        )
        config: Dict[str, Any] = {"model_id": self.model_id}
        if params:
            config["params"] = params
        return config

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (for logging / debug)."""
        return {
            "model_id": self.model_id,
            "caching_enabled": self.caching_enabled,
            "provider": self.get_provider().value,
            "inference_params": dict(self.inference_params),
            "mantle_endpoint_path": self.mantle_endpoint_path,
        }

    @classmethod
    def from_params(
        cls,
        model_id: Optional[str] = None,
        caching_enabled: Optional[bool] = None,
        provider: Optional[str] = None,
        inference_params: Optional[Dict[str, Any]] = None,
        mantle_endpoint_path: Optional[str] = None,
    ) -> "ModelConfig":
        """Create ModelConfig from optional parameters.

        Args:
            model_id: Model ID (provider-specific format)
            caching_enabled: Whether to enable prompt caching (Bedrock only)
            provider: Provider name ("bedrock", "openai", "gemini", or "mantle")
            inference_params: Canonical-name -> value map (temperature, top_p,
                max_tokens, thinking, ...). Each provider's translation table
                drops unsupported keys silently.
            mantle_endpoint_path: Bedrock Mantle endpoint path ("/v1" or
                "/openai/v1"). Only consulted on the MANTLE provider path.
        """
        provider_enum = ModelProvider.BEDROCK
        if provider:
            try:
                provider_enum = ModelProvider(provider.lower())
            except ValueError:
                pass  # Invalid provider, fall back to default + auto-detect via model_id

        return cls(
            model_id=model_id or cls.model_id,
            caching_enabled=caching_enabled if caching_enabled is not None else cls.caching_enabled,
            provider=provider_enum,
            inference_params=dict(inference_params) if inference_params else {},
            mantle_endpoint_path=mantle_endpoint_path,
        )
