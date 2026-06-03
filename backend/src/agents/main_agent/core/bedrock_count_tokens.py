"""BedrockModel variant that makes native token counting work for our models.

Bedrock's CountTokens API rejects cross-region inference-profile model ids
(``us.anthropic.…`` / ``eu.…`` / ``apac.…`` / ``us-gov.…``) with a misleading
``ValidationException: The provided model doesn't support counting tokens.`` —
even though on-demand invocation of the newer Claude models *requires* the
inference-profile id. CountTokens only accepts the base foundation-model id
(``anthropic.…``). The inference profile is pure cross-region routing, so the
base-id count is exact, not an approximation.

This subclass de-prefixes the model id for the CountTokens call only;
invocation (``stream`` / ``structured_output``) keeps the profile id untouched.
Because Strands' per-turn estimate (``_estimate_input_tokens`` →
``BeforeModelCallEvent.projected_input_tokens``) routes through
``count_tokens``, making it authoritative improves proactive context-compaction
decisions in addition to feeding the context-attribution hook — both stop
relying on the chars/4 heuristic.
"""

import re

from strands.models import BedrockModel
from strands.types.content import Messages, SystemContentBlock
from strands.types.tools import ToolSpec

# Cross-region inference-profile geography prefixes. Closed set per AWS — we
# only strip these exact codes so a real model id is never mangled.
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(us|eu|apac|us-gov)\.")


def base_foundation_model_id(model_id: str) -> str:
    """Return the base foundation-model id for ``model_id``.

    Strips a leading cross-region inference-profile prefix (``us.`` / ``eu.`` /
    ``apac.`` / ``us-gov.``). No-op for ids that are already base ids or that
    belong to another provider — so it is safe to call unconditionally on the
    Bedrock path.
    """
    return _INFERENCE_PROFILE_PREFIX.sub("", model_id, count=1)


class CountTokensBedrockModel(BedrockModel):
    """``BedrockModel`` that counts tokens against the base foundation-model id.

    See the module docstring for why the inference-profile id can't be used for
    CountTokens. Everything else (invocation, config, streaming) is inherited
    unchanged.
    """

    async def count_tokens(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
    ) -> int:
        """Count tokens using the de-prefixed base model id.

        The SDK's native path reads ``self.config["model_id"]`` for the
        CountTokens ``modelId`` arg, so we swap to the base id for the duration
        of the count and restore it in ``finally``. Token counting and
        invocation never overlap on a single model instance — the model is
        per-session and the event loop awaits the estimate before streaming —
        so the swap window is safe, and the profile id is always restored
        before any ``stream`` call.
        """
        original_id = self.config["model_id"]
        base_id = base_foundation_model_id(original_id)
        if base_id == original_id:
            return await super().count_tokens(
                messages, tool_specs, system_prompt, system_prompt_content
            )

        self.config["model_id"] = base_id
        try:
            return await super().count_tokens(
                messages, tool_specs, system_prompt, system_prompt_content
            )
        finally:
            self.config["model_id"] = original_id
