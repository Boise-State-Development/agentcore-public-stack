"""BedrockModel variant that makes native token counting work for our models,
and keeps a throttled count from ever stalling the reply.

Two problems, one subclass.

**Model id.** Bedrock's CountTokens API rejects cross-region inference-profile
model ids (geographic ``us.anthropic.…`` / ``eu.…`` / ``jp.…`` and so on, and
``global.…``) with a misleading ``ValidationException: The provided model
doesn't support counting tokens.`` — even though on-demand invocation of the
newer Claude models *requires* the inference-profile id. CountTokens only accepts the base
foundation-model id (``anthropic.…``). The inference profile is pure
cross-region routing, so the base-id count is exact, not an approximation.
This subclass passes the base id to CountTokens explicitly; invocation
(``stream`` / ``structured_output``) keeps the profile id untouched. It never
mutates ``self.config`` — an earlier version swapped ``config["model_id"]``
for the duration of the call, which is only safe while a count and a stream
can never overlap on one instance. That stops being true the moment any count
runs concurrently with the model call, so the swap is gone.

**Latency.** Strands awaits ``count_tokens`` in front of *every* model call
(``event_loop._estimate_input_tokens`` → ``BeforeModelCallEvent
.projected_input_tokens``). A native count there is a CountTokens round trip
added to time to first token: ~70 ms at minimum, ~150 ms for a 30k-token
prompt, measured against dev Bedrock (#1343). Nothing in this stack needs that
projection to be native — Strands' proactive compression is off (we build
``SlidingWindowConversationManager`` without a compression threshold) and our
compaction does not read it — so the factory builds this model with
``native_projection=False`` and ``count_tokens`` answers with the heuristic.
Native counts come from ``native_count_tokens``, which the context-attribution
hook calls from a background task, concurrently with the model call.

**Throttling.** CountTokens has its own request-rate quota, separate from the
model's token quota, and under load it throttles before the model does. The
SDK's shared ``bedrock-runtime`` client retries with backoff first, so while
counts sat in front of the model call the *reply* waited out that backoff (p95
5.8 s measured on a 40k-user load run, docs/specs/load-test-assessment-2026-09.md
§1 fix 3). Counting therefore goes through a dedicated client with a single
attempt and a short read timeout: a throttle costs at most one failed request.
"""

import asyncio
import logging
import os
import re
from typing import Any, Optional

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands.models import BedrockModel
from strands.models import bedrock as _strands_bedrock
from strands.models.model import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.exceptions import ProviderTokenCountError
from strands.types.tools import ToolSpec

logger = logging.getLogger(__name__)

# Cross-region inference-profile prefixes: the geographic profiles (``us``,
# ``us-gov``, ``eu``, ``apac``, ``au``, ``jp``) plus ``global``. Closed set per
# the AWS docs (the per-model cards list each profile id) — we only strip these
# exact codes so a real model id is never mangled. Missing one is silent: the
# profile id reaches CountTokens, Bedrock rejects it as unsupported, the model
# lands on the skip list and every count for it is the heuristic.
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(us|us-gov|eu|apac|au|jp|global)\.")

# Bound on a single CountTokens attempt. Read + connect timeout, seconds. A
# healthy count answers in well under a second; anything slower is the quota
# talking, and the heuristic is a better use of the time.
COUNT_TOKENS_TIMEOUT_ENV = "COUNT_TOKENS_TIMEOUT_SECONDS"
COUNT_TOKENS_TIMEOUT_DEFAULT = 2.0

# Attempts per count, including the first. 1 = never retry a throttle.
COUNT_TOKENS_MAX_ATTEMPTS_ENV = "COUNT_TOKENS_MAX_ATTEMPTS"
COUNT_TOKENS_MAX_ATTEMPTS_DEFAULT = 1

_THROTTLE_CODES = frozenset({"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"})


def base_foundation_model_id(model_id: str) -> str:
    """Return the base foundation-model id for ``model_id``.

    Strips a leading cross-region inference-profile prefix (``us.`` /
    ``us-gov.`` / ``eu.`` / ``apac.`` / ``au.`` / ``jp.`` / ``global.``).
    No-op for ids that are already base ids or that belong to another
    provider — so it is safe to call unconditionally on the Bedrock path.
    """
    return _INFERENCE_PROFILE_PREFIX.sub("", model_id, count=1)


def count_tokens_timeout_seconds() -> float:
    """Per-attempt timeout for CountTokens, from the environment."""
    raw = os.environ.get(COUNT_TOKENS_TIMEOUT_ENV, "")
    try:
        value = float(raw) if raw else COUNT_TOKENS_TIMEOUT_DEFAULT
    except ValueError:
        value = COUNT_TOKENS_TIMEOUT_DEFAULT
    return value if value > 0 else COUNT_TOKENS_TIMEOUT_DEFAULT


def count_tokens_max_attempts() -> int:
    """Attempts per CountTokens call (including the first), from the environment."""
    raw = os.environ.get(COUNT_TOKENS_MAX_ATTEMPTS_ENV, "")
    try:
        value = int(raw) if raw else COUNT_TOKENS_MAX_ATTEMPTS_DEFAULT
    except ValueError:
        value = COUNT_TOKENS_MAX_ATTEMPTS_DEFAULT
    return value if value >= 1 else COUNT_TOKENS_MAX_ATTEMPTS_DEFAULT


def build_count_tokens_client_config() -> BotocoreConfig:
    """Botocore config for the dedicated CountTokens client.

    Exposed so the bound is testable without a network: one attempt by default,
    a short read/connect timeout, and the same ``strands-agents`` user-agent
    marker the SDK's own client carries so the calls stay attributable.
    """
    timeout = count_tokens_timeout_seconds()
    return BotocoreConfig(
        retries={"total_max_attempts": count_tokens_max_attempts(), "mode": "standard"},
        read_timeout=timeout,
        connect_timeout=timeout,
        user_agent_extra="strands-agents count-tokens",
    )


class CountTokensBedrockModel(BedrockModel):
    """``BedrockModel`` that counts tokens against the base foundation-model id
    through a bounded, dedicated client.

    See the module docstring for why the inference-profile id can't be used for
    CountTokens and why the count must never wait out a retry backoff.
    Everything else (invocation, config, streaming) is inherited unchanged.
    """

    def __init__(self, *args: Any, native_projection: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._count_client: Any = None
        #: Whether ``count_tokens`` — which Strands awaits before every model
        #: call — goes to CountTokens. ``False`` keeps that call local (the
        #: heuristic) so it adds nothing to time to first token.
        self._native_projection = native_projection
        #: Counts this instance answered with the chars/4 heuristic rather than
        #: natively — native counting off, a model on the skip list, a
        #: throttle, any failure. Monotonic, so a reader that needs to know
        #: whether *its* counts were real compares two readings.
        self.heuristic_count_fallbacks = 0

    @property
    def token_count_is_authoritative(self) -> bool:
        """Whether ``count_tokens`` can answer natively for this model.

        ``False`` when native counting is off or the base id is on the SDK's
        skip list — Bedrock refused it as unsupported or AccessDenied, which
        ``count_tokens`` records on the first failed call. Some Converse
        models genuinely lack CountTokens: ``anthropic.claude-sonnet-5``
        answers "doesn't support counting tokens" under the base id and the
        ``us.`` profile id alike (verified in us-west-2, 2026-09-25), so every
        count for it is the heuristic. Read by the context-attribution hook,
        which must not build its tools residual out of heuristic counts.
        """
        if self.config.get("use_native_token_count") is not True:
            return False
        base_id = base_foundation_model_id(self.config["model_id"])
        return base_id not in _strands_bedrock._SKIP_COUNT_TOKENS_MODELS

    def _get_count_client(self) -> Any:
        """The dedicated CountTokens client, built on first use.

        Same region and endpoint as the SDK's invocation client, default
        credential chain (the factory passes no ``boto_session``), and the
        bounded config above. Lazy so a model that never counts (native
        counting off, or a model on the skip list) never builds it.
        """
        if self._count_client is None:
            meta = self.client.meta
            self._count_client = boto3.client(
                "bedrock-runtime",
                region_name=meta.region_name,
                endpoint_url=meta.endpoint_url,
                config=build_count_tokens_client_config(),
            )
        return self._count_client

    async def _heuristic_count(
        self,
        messages: Messages,
        tool_specs: Optional[list[ToolSpec]],
        system_prompt: Optional[str],
        system_prompt_content: Optional[list[SystemContentBlock]],
    ) -> int:
        """The SDK's provider-agnostic chars/4 estimate — what ``BedrockModel``
        itself falls back to. Counted in ``heuristic_count_fallbacks``."""
        self.heuristic_count_fallbacks += 1
        return await Model.count_tokens(self, messages, tool_specs, system_prompt, system_prompt_content)

    def native_count_tokens(
        self,
        messages: Messages,
        tool_specs: Optional[list[ToolSpec]] = None,
        system_prompt_content: Optional[list[SystemContentBlock]] = None,
    ) -> Optional[int]:
        """One native CountTokens call, or ``None`` when it can't be had.

        Blocking — callers off the event loop (``asyncio.to_thread``) only.
        This is the counter the context-attribution hook uses from its
        background task, so a count never sits in front of a model call.
        ``None`` covers every non-answer: native counting off, the base id on
        the SDK's skip list, a throttle, any failure. An unsupported model or
        an AccessDenied puts the base id on the skip list, as ``count_tokens``
        always has. Never falls back to the heuristic: a caller that gets
        ``None`` knows it has no native count, instead of having to infer it
        from ``heuristic_count_fallbacks``.
        """
        if self.config.get("use_native_token_count") is not True:
            return None
        base_id = base_foundation_model_id(self.config["model_id"])
        if base_id in _strands_bedrock._SKIP_COUNT_TOKENS_MODELS:
            return None
        try:
            request = self.format_request(messages, tool_specs, system_prompt_content)
            converse_input: dict[str, Any] = {
                key: request[key] for key in ("messages", "system", "toolConfig") if key in request
            }
            response = self._get_count_client().count_tokens(modelId=base_id, input={"converse": converse_input})
            input_tokens = response.get("inputTokens")
            if input_tokens is None:
                raise ProviderTokenCountError("Bedrock count_tokens returned None for inputTokens")
            logger.debug("model_id=<%s>, total_tokens=<%d> | native token count", base_id, input_tokens)
            return int(input_tokens)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "AccessDeniedException":
                logger.warning(
                    "model_id=<%s> | bedrock:CountTokens permission denied, falling back to heuristic estimation: %s",
                    base_id,
                    e,
                )
                _strands_bedrock._SKIP_COUNT_TOKENS_MODELS.add(base_id)
            elif code == "ValidationException" and "doesn't support counting tokens" in str(e):
                logger.debug(
                    "model_id=<%s> | model does not support CountTokens, caching for future calls,"
                    " falling back to estimation",
                    base_id,
                )
                _strands_bedrock._SKIP_COUNT_TOKENS_MODELS.add(base_id)
            elif code in _THROTTLE_CODES:
                # Expected under load. Not retried by design — the heuristic
                # is the bounded path and the reply must not wait.
                logger.info(
                    "model_id=<%s> code=<%s> | CountTokens throttled, using heuristic estimate this call",
                    base_id,
                    code,
                )
            else:
                logger.debug(
                    "model_id=<%s>, error=<%s> | native token counting failed, falling back to estimation",
                    base_id,
                    e,
                )
        except Exception as e:  # noqa: BLE001 - counting must never break a turn
            logger.debug(
                "model_id=<%s>, error=<%s> | native token counting failed, falling back to estimation",
                base_id,
                e,
            )
        return None

    async def count_tokens(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
    ) -> int:
        """Count tokens natively against the de-prefixed base model id.

        Mirrors ``BedrockModel.count_tokens`` (native-flag gate, per-model skip
        cache on AccessDenied / unsupported, heuristic fallback on any failure)
        with two differences: the request goes through the bounded dedicated
        client, and the model id is passed to the API rather than written into
        ``self.config``.

        This is the method Strands awaits in front of every model call. With
        ``native_projection=False`` it never goes to the network: the
        projection is the heuristic, and native counts come only from
        ``native_count_tokens``, off the critical path.
        """
        if not self._native_projection:
            return await self._heuristic_count(messages, tool_specs, system_prompt, system_prompt_content)
        if system_prompt and system_prompt_content is None:
            system_prompt_content = [{"text": system_prompt}]
        count = await asyncio.to_thread(self.native_count_tokens, messages, tool_specs, system_prompt_content)
        if count is None:
            return await self._heuristic_count(messages, tool_specs, system_prompt, system_prompt_content)
        return count
