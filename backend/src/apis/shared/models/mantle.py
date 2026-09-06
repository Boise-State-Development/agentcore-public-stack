"""Shared Bedrock Mantle model construction.

Single home for building a Strands OpenAI-compatible model pointed at Bedrock
Mantle, plus the per-API-mode canonical->native param maps. Both consumers use
this so the mantle build logic is never forked:

- the agent loop's ``AgentFactory._create_mantle_model`` (agents/), and
- the API-key ``/chat/api-converse`` handler (apis/app_api/), which cannot
  import ``agents/`` per the import-boundary rule.

Strands' ``bedrock_mantle_config`` owns the wire details — it mints the
short-term bearer token (via aws-bedrock-token-generator, requires
``bedrock-mantle:CallWithBearerToken``) and derives the regional base URL plus
the model-family base path. All this module does is pick the model class from
the declared API mode and forward the region + already-native params.
"""

from enum import Enum
from typing import Any, Dict, Optional

from .usage_normalization import usage_normalized


class MantleApiMode(str, Enum):
    """OpenAI-compatible API surface a Bedrock Mantle model speaks.

    Selects which Strands model class gets built: Chat Completions
    (``OpenAIModel``) or the Responses API (``OpenAIResponsesModel``). Some
    Mantle-hosted models (e.g. ``openai.gpt-5.x``) only serve Responses and
    reject Chat Completions outright, so this is a per-model fact the admin
    records — Mantle exposes no API to discover it.
    """
    CHAT_COMPLETIONS = "chat"
    RESPONSES = "responses"


# Canonical param name -> provider-native key path (dot-separated for nested SDK
# fields). A canonical param without an entry here is silently dropped.

# Mantle Chat Completions mirrors OpenAI's chat-completions protocol.
MANTLE_CHAT_PARAM_MAP: Dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_tokens": "max_tokens",
    "reasoning_effort": "reasoning_effort",
}

# Mantle Responses API (OpenAIResponsesModel) uses different native names:
# `max_output_tokens` for the output cap and a nested `reasoning.effort` object.
# The SDK spreads `params` straight into `responses.create(**params)` with no
# translation, so the canonical names must be pre-mapped here.
MANTLE_RESPONSES_PARAM_MAP: Dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_tokens": "max_output_tokens",
    "reasoning_effort": "reasoning.effort",
}


def param_map_for(api_mode: MantleApiMode) -> Dict[str, str]:
    """Return the canonical->native param map for a Mantle API mode."""
    return (
        MANTLE_RESPONSES_PARAM_MAP
        if api_mode == MantleApiMode.RESPONSES
        else MANTLE_CHAT_PARAM_MAP
    )


def _ensure_gemma4_openai_v1_routing() -> None:
    """Teach the Strands SDK to route ``google.gemma-4-*`` to ``/openai/v1``.

    Per its AWS model card, Gemma 4 is served *only* on the Mantle
    ``/openai/v1`` base path — "different from the ``v1`` path used by other
    models." A model whose prefix is missing from the SDK's
    ``_OPENAI_PATH_MODEL_PREFIXES`` falls through to ``/v1`` and the endpoint
    401s ("... is not enabled for this account").

    strands-agents 1.51.0 ships ``google.gemma-4-`` upstream, so this is now a
    no-op on the pinned SDK; the guard stays as a floor in case a future pin
    regresses the list.

    ``google.gemma-4-`` specifically — NOT ``google.gemma-``: Gemma 3 is served
    on ``/v1`` and must not be rerouted. Idempotent; safe to call on every build.
    """
    from strands.models import _openai_bedrock as _sdk

    prefix = "google.gemma-4-"
    if prefix not in _sdk._OPENAI_PATH_MODEL_PREFIXES:
        _sdk._OPENAI_PATH_MODEL_PREFIXES = (
            *_sdk._OPENAI_PATH_MODEL_PREFIXES,
            prefix,
        )


def build_mantle_model(
    model_id: str,
    api_mode: MantleApiMode,
    region: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
):
    """Build a Strands OpenAI-compatible model targeting Bedrock Mantle.

    Args:
        model_id: The Mantle model id (e.g. ``openai.gpt-5.4``).
        api_mode: Chat Completions vs Responses — picks the model class.
        region: Optional Mantle region override. ``None`` -> Strands resolves
            from the ambient boto/region chain. Drives both the endpoint host
            and the region the bearer token is signed for.
        params: Already-native inference params (canonical names must be
            pre-translated via :func:`param_map_for`). Spread into the client.

    Returns:
        ``OpenAIModel`` | ``OpenAIResponsesModel`` configured for Mantle.
    """
    # Lazy import: strands is heavy, and apis.shared is imported broadly.
    from strands.models import OpenAIResponsesModel
    from strands.models.openai import OpenAIModel

    # Bridge until google.gemma-4- lands in the SDK's base-path prefix table.
    _ensure_gemma4_openai_v1_routing()

    bedrock_mantle_config: Dict[str, Any] = {}
    if region:
        bedrock_mantle_config["region"] = region

    # Wrapped so the model reports Bedrock-Converse token-bucket semantics:
    # OpenAI's `input_tokens` is inclusive of the cache buckets, and Strands
    # drops `cache_write_tokens` outright. Applied here — the single OpenAI-
    # family construction seam both consumers share — so no downstream reader
    # of the usage dict needs to know which provider produced it.
    model_cls = usage_normalized(
        OpenAIResponsesModel
        if api_mode == MantleApiMode.RESPONSES
        else OpenAIModel
    )

    config: Dict[str, Any] = {"model_id": model_id}
    if params:
        config["params"] = params

    return model_cls(bedrock_mantle_config=bedrock_mantle_config, **config)
