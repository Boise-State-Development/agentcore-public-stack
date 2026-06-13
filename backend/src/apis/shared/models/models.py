"""Managed model data models.

These models define the structure for managed models used across
app API and inference API deployments.
"""

from pydantic import BaseModel, Field, ConfigDict, model_validator
from typing import Any, Dict, List, Optional
from datetime import datetime


class ModelParamSpec(BaseModel):
    """Capability + bounds for a single inference parameter on a model.

    Stored per-model in the registry. Drives both the admin form (what's
    tweakable, what bounds to enforce) and the runtime gate (whether to
    pass the param through to the provider SDK at all).
    """
    model_config = ConfigDict(populate_by_name=True)

    supported: bool = True
    min: Optional[float] = None
    max: Optional[float] = None
    allowed: Optional[list[Any]] = Field(
        None,
        description="Permissible values for enum-style params (e.g. `effort`: "
                    "low/medium/high/xhigh/max). When set, `default` and any "
                    "user override must be a member; `min`/`max` don't apply. "
                    "Keep ordered low->high so future clamping (request `max`, "
                    "model caps at `high`) can degrade gracefully."
    )
    default: Optional[Any] = Field(
        None,
        description="Value sent when the user doesn't override. Type depends on the param "
                    "(number for temperature/top_p, int budget for thinking, "
                    "string for effort, etc.)."
    )
    locked: bool = Field(
        False,
        description="If true, the admin default is final and user overrides are ignored. "
                    "Used by Phase 2 user-tweak surface; ignored today."
    )

    @model_validator(mode="after")
    def _check_bounds(self) -> "ModelParamSpec":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be <= max")
        if isinstance(self.default, (int, float)):
            if self.min is not None and self.default < self.min:
                raise ValueError("default must be >= min")
            if self.max is not None and self.default > self.max:
                raise ValueError("default must be <= max")
        if self.allowed is not None:
            if not self.allowed:
                raise ValueError("allowed must be non-empty when set")
            if self.default is not None and self.default not in self.allowed:
                raise ValueError("default must be one of allowed")
        return self


class SupportedParams(BaseModel):
    """Per-model inference parameter capability map.

    Open-ended dict keyed by canonical param name (`temperature`, `top_p`,
    `top_k`, `max_tokens`, `thinking`, `reasoning_effort`, ...). Each
    provider's `ModelConfig.to_<provider>_config()` translates canonical
    names into the SDK-specific shape and silently drops unknown keys.

    For ``thinking``, ``ModelParamSpec.default`` carries the budget in
    tokens (int >= 1024, or 0/None to disable). The provider translator
    wraps a truthy value into the Anthropic ``{type: "enabled",
    budget_tokens: N}`` shape on older models, or ``{type: "adaptive"}``
    on models that require adaptive thinking (Opus 4.6/4.7, Sonnet 4.6) —
    where the int just means "thinking on" and depth is governed by the
    separate ``effort`` param.
    """
    model_config = ConfigDict(populate_by_name=True)

    params: Dict[str, ModelParamSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_thinking_invariants(self) -> "SupportedParams":
        """Enforce Anthropic's extended-thinking rules at config time.

        Catches the two failure modes that would otherwise only surface as a
        Bedrock 400 mid-conversation: budget below the 1024 floor, or budget
        >= max_tokens. Skipped when thinking is unsupported or disabled.
        """
        thinking = self.params.get("thinking")
        if thinking is None or not thinking.supported:
            return self
        budget = thinking.default
        if budget in (None, False, 0):
            return self
        # bool is a subclass of int — reject it explicitly so a stale `true`
        # default from the old toggle schema fails loudly instead of being
        # interpreted as a 1-token budget. Whole-number floats are accepted
        # and coerced because DynamoDB roundtrips numeric fields through
        # Decimal → float, so an int stored from the admin form comes back
        # as `4096.0` and would otherwise fail this check on every list call.
        if isinstance(budget, bool):
            raise ValueError("thinking default must be an int budget (>= 1024) or null/0")
        if isinstance(budget, float):
            if not budget.is_integer():
                raise ValueError("thinking default must be an int budget (>= 1024) or null/0")
            budget = int(budget)
            thinking.default = budget
        elif not isinstance(budget, int):
            raise ValueError("thinking default must be an int budget (>= 1024) or null/0")
        if budget < 1024:
            raise ValueError("thinking budget must be >= 1024")
        max_tokens = self.params.get("max_tokens")
        mt_default = max_tokens.default if max_tokens else None
        if isinstance(mt_default, float) and mt_default.is_integer():
            mt_default = int(mt_default)
        if isinstance(mt_default, int) and not isinstance(mt_default, bool) and budget >= mt_default:
            raise ValueError("thinking budget must be < max_tokens default")
        return self


def _max_tokens_within_ceiling(
    max_output_tokens: Optional[int],
    supported_params: Optional[SupportedParams],
) -> None:
    """Reject a max_tokens spec that lets the runtime request more output
    than the model can physically produce.

    Mirrors the Angular ``maxTokensCeilingValidator``. Only checks when both
    the model ceiling and a *supported* max_tokens spec are present in the
    same payload — a partial update touching only one side is left to the
    per-field bounds rules.
    """
    if max_output_tokens is None or supported_params is None:
        return
    spec = supported_params.params.get("max_tokens")
    if spec is None or not spec.supported:
        return
    if spec.max is not None and spec.max > max_output_tokens:
        raise ValueError("max_tokens max must be <= maxOutputTokens")
    if (
        isinstance(spec.default, (int, float))
        and not isinstance(spec.default, bool)
        and spec.default > max_output_tokens
    ):
        raise ValueError("max_tokens default must be <= maxOutputTokens")


class ManagedModelCreate(BaseModel):
    """Request model for creating a managed model."""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId", min_length=1)
    model_name: str = Field(..., alias="modelName", min_length=1)
    provider: str = Field(..., min_length=1)
    provider_name: str = Field(..., alias="providerName", min_length=1)
    input_modalities: List[str] = Field(..., alias="inputModalities", min_length=1)
    output_modalities: List[str] = Field(..., alias="outputModalities", min_length=1)
    max_input_tokens: int = Field(..., alias="maxInputTokens", ge=1)
    max_output_tokens: int = Field(..., alias="maxOutputTokens", ge=1)
    # Access control: AppRoles (preferred) or legacy JWT roles
    allowed_app_roles: List[str] = Field(
        default_factory=list,
        alias="allowedAppRoles",
        description="AppRole IDs that can access this model (preferred over availableToRoles)"
    )
    available_to_roles: List[str] = Field(
        default_factory=list,
        alias="availableToRoles",
        description="[DEPRECATED] Legacy JWT role names. Use allowedAppRoles instead. "
                    "During transition, access is granted if user matches EITHER field."
    )
    enabled: bool = True
    input_price_per_million_tokens: float = Field(..., alias="inputPricePerMillionTokens", ge=0)
    output_price_per_million_tokens: float = Field(..., alias="outputPricePerMillionTokens", ge=0)
    cache_write_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheWritePricePerMillionTokens",
        ge=0,
        description="Price per million tokens written to cache (Bedrock only, ~25% markup)"
    )
    cache_read_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheReadPricePerMillionTokens",
        ge=0,
        description="Price per million tokens read from cache (Bedrock only, ~90% discount)"
    )
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")
    supports_caching: Optional[bool] = Field(
        None,
        alias="supportsCaching",
        description="Whether this model supports prompt caching. Defaults to True for Bedrock Claude models, False for others."
    )
    is_default: bool = Field(
        False,
        alias="isDefault",
        description="Whether this is the default model for new sessions. Only one model can be default."
    )
    mantle_endpoint_path: Optional[str] = Field(
        None,
        alias="mantleEndpointPath",
        description="Bedrock Mantle endpoint path segment (provider='mantle' only): "
                    "'/v1' (OpenAI Chat Completions, the default) or '/openai/v1' "
                    "(e.g. Gemma 4). The per-model value comes from the model card; "
                    "there is no API that exposes it. Ignored for other providers."
    )
    supported_params: Optional[SupportedParams] = Field(
        None,
        alias="supportedParams",
        description="Per-model inference parameter capabilities (temperature, top_p, etc.). "
                    "When None, the runtime sends no inference params."
    )

    @model_validator(mode="after")
    def _check_max_tokens_within_ceiling(self) -> "ManagedModelCreate":
        _max_tokens_within_ceiling(self.max_output_tokens, self.supported_params)
        return self


class ManagedModelUpdate(BaseModel):
    """Request model for updating a managed model."""
    model_config = ConfigDict(populate_by_name=True)

    model_id: Optional[str] = Field(None, alias="modelId", min_length=1)
    model_name: Optional[str] = Field(None, alias="modelName")
    provider: Optional[str] = None
    provider_name: Optional[str] = Field(None, alias="providerName")
    input_modalities: Optional[List[str]] = Field(None, alias="inputModalities")
    output_modalities: Optional[List[str]] = Field(None, alias="outputModalities")
    max_input_tokens: Optional[int] = Field(None, alias="maxInputTokens", ge=1)
    max_output_tokens: Optional[int] = Field(None, alias="maxOutputTokens", ge=1)
    # Access control: AppRoles (preferred) or legacy JWT roles
    allowed_app_roles: Optional[List[str]] = Field(
        None,
        alias="allowedAppRoles",
        description="AppRole IDs that can access this model (preferred over availableToRoles)"
    )
    available_to_roles: Optional[List[str]] = Field(
        None,
        alias="availableToRoles",
        description="[DEPRECATED] Legacy JWT role names. Use allowedAppRoles instead."
    )
    enabled: Optional[bool] = None
    input_price_per_million_tokens: Optional[float] = Field(None, alias="inputPricePerMillionTokens", ge=0)
    output_price_per_million_tokens: Optional[float] = Field(None, alias="outputPricePerMillionTokens", ge=0)
    cache_write_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheWritePricePerMillionTokens",
        ge=0,
        description="Price per million tokens written to cache (Bedrock only, ~25% markup)"
    )
    cache_read_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheReadPricePerMillionTokens",
        ge=0,
        description="Price per million tokens read from cache (Bedrock only, ~90% discount)"
    )
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")
    supports_caching: Optional[bool] = Field(
        None,
        alias="supportsCaching",
        description="Whether this model supports prompt caching."
    )
    is_default: Optional[bool] = Field(
        None,
        alias="isDefault",
        description="Whether this is the default model for new sessions."
    )
    mantle_endpoint_path: Optional[str] = Field(
        None,
        alias="mantleEndpointPath",
        description="Bedrock Mantle endpoint path segment (provider='mantle' only): "
                    "'/v1' or '/openai/v1'. Ignored for other providers."
    )
    supported_params: Optional[SupportedParams] = Field(
        None,
        alias="supportedParams",
        description="Per-model inference parameter capabilities."
    )

    @model_validator(mode="after")
    def _check_max_tokens_within_ceiling(self) -> "ManagedModelUpdate":
        _max_tokens_within_ceiling(self.max_output_tokens, self.supported_params)
        return self


class ManagedModel(BaseModel):
    """Managed model with full details including cache pricing."""
    model_config = ConfigDict(populate_by_name=True)

    id: str
    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str
    provider_name: str = Field(..., alias="providerName")
    input_modalities: List[str] = Field(..., alias="inputModalities")
    output_modalities: List[str] = Field(..., alias="outputModalities")
    max_input_tokens: int = Field(..., alias="maxInputTokens")
    max_output_tokens: int = Field(..., alias="maxOutputTokens")
    # Access control: AppRoles (preferred) or legacy JWT roles
    allowed_app_roles: List[str] = Field(
        default_factory=list,
        alias="allowedAppRoles",
        description="AppRole IDs that can access this model (preferred over availableToRoles)"
    )
    available_to_roles: List[str] = Field(
        default_factory=list,
        alias="availableToRoles",
        description="[DEPRECATED] Legacy JWT role names. Use allowedAppRoles instead."
    )
    enabled: bool
    input_price_per_million_tokens: float = Field(..., alias="inputPricePerMillionTokens")
    output_price_per_million_tokens: float = Field(..., alias="outputPricePerMillionTokens")
    cache_write_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheWritePricePerMillionTokens",
        description="Price per million tokens written to cache (Bedrock only, ~25% markup)"
    )
    cache_read_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheReadPricePerMillionTokens",
        description="Price per million tokens read from cache (Bedrock only, ~90% discount)"
    )
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")
    supports_caching: bool = Field(
        True,
        alias="supportsCaching",
        description="Whether this model supports prompt caching. Defaults to True."
    )
    is_default: bool = Field(
        False,
        alias="isDefault",
        description="Whether this is the default model for new sessions. Only one model can be default."
    )
    mantle_endpoint_path: Optional[str] = Field(
        None,
        alias="mantleEndpointPath",
        description="Bedrock Mantle endpoint path segment (provider='mantle' only): "
                    "'/v1' or '/openai/v1'. Ignored for other providers."
    )
    supported_params: Optional[SupportedParams] = Field(
        None,
        alias="supportedParams",
        description="Per-model inference parameter capabilities."
    )
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")
