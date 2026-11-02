"""Provider-aware token-usage normalization.

Every cost and context-size path we own assumes the **Bedrock Converse**
convention: ``inputTokens``, ``cacheReadInputTokens`` and
``cacheWriteInputTokens`` are three *disjoint* buckets whose sum is the call's
total input. ``CostCalculator.calculate_message_cost`` documents that contract
and prices each bucket at its own rate; the context-attribution sum in the
stream coordinator adds all three to get "current context size".

The OpenAI family reports the opposite convention — ``input_tokens`` is
**inclusive**. Per AWS's GPT-5.6 prompt-caching guidance the identity is::

    input_tokens = cached_tokens + cache_write_tokens + non-cached remainder

Strands passes ``input_tokens`` straight through as ``inputTokens`` while
*also* reporting ``cacheReadInputTokens``
(``strands/models/openai_responses.py`` and ``strands/models/openai.py``), so
without normalization every cached token is billed twice: once at the full
input rate and once at the cache-read rate. With cache writes it is worse —
a written token would be billed at the input rate *plus* the 1.25x write
premium.

This module fixes both halves, once, at the earliest seam we control:

1. :func:`normalize_usage` restores disjointness for the OpenAI family and
   leaves Bedrock usage untouched.
2. :func:`usage_normalized` wraps a Strands OpenAI-family model class so the
   normalization is applied while the model formats its ``metadata`` chunk —
   ahead of the cost calculator, the metadata writers, the prompt-cache
   observability layer and the SSE stream. Downstream consumers keep reading
   plain Converse-shaped usage dicts and need no provider awareness.

``cacheWriteInputTokens`` arrives from the SDK itself. Both OpenAI paths map
``cache_write_tokens`` upstream (Chat Completions in 1.57 via #4361, Responses
in 1.57.2 via #4193), each beside an ``inputTokens`` that is still inclusive,
so :func:`normalize_usage` subtracts the write bucket once like any other.
We carried a local recovery of that field through 1.57.1, when the SDK dropped
it and ``cacheWriteInputTokens`` was structurally 0 for GPT-5.6 — which pinned
``wastedUsd`` at $0 and made the 1.25x write premium invisible. It became a
same-value overwrite on 1.57.2 and was removed. ``TestStrandsSdkContract``
asserts the upstream mapping against the real SDK, so a bump that regressed it
would fail there rather than silently zeroing the write premium again.

⚠️ :func:`normalize_usage` is **not idempotent** for the OpenAI family — it
subtracts. Apply it exactly once per usage payload, at the model seam. Do not
add a second call at a site that reads the usage dict.
"""

import logging
from enum import Enum
from typing import Any, Dict, Mapping

logger = logging.getLogger(__name__)


class UsageProvider(str, Enum):
    """Token-accounting convention a model's usage payload follows.

    ``BEDROCK`` — Converse semantics: the three input buckets are already
    disjoint. Also the correct value for any provider that follows the same
    convention; normalization is a no-op.

    ``OPENAI`` — Chat Completions *and* the Responses API: ``inputTokens`` is
    inclusive of the cache buckets and must have them subtracted out.
    """

    BEDROCK = "bedrock"
    OPENAI = "openai"


def normalize_usage(
    usage: Mapping[str, Any],
    provider: UsageProvider,
) -> Dict[str, Any]:
    """Return a copy of ``usage`` whose input buckets are disjoint.

    Args:
        usage: Converse-shaped usage dict (``inputTokens``, ``outputTokens``,
            ``totalTokens``, and optionally ``cacheReadInputTokens`` /
            ``cacheWriteInputTokens``).
        provider: The convention ``usage`` currently follows.

    Returns:
        A new dict. For :attr:`UsageProvider.BEDROCK` it is an unmodified copy.
        For :attr:`UsageProvider.OPENAI`, ``inputTokens`` has the cache buckets
        subtracted out, clamped at 0.

    Note:
        Not idempotent for the OpenAI family — see the module docstring.
    """
    normalized: Dict[str, Any] = dict(usage)

    if provider != UsageProvider.OPENAI:
        return normalized

    cache_read = normalized.get("cacheReadInputTokens") or 0
    cache_write = normalized.get("cacheWriteInputTokens") or 0
    if not cache_read and not cache_write:
        return normalized

    input_tokens = normalized.get("inputTokens") or 0
    # Clamp: a provider bug that reports cache buckets larger than the
    # inclusive total (it has happened upstream) must not produce a negative
    # bucket that the calculator would silently credit against the bill.
    normalized["inputTokens"] = max(0, input_tokens - cache_read - cache_write)
    return normalized


# Strands formats every provider chunk through one method per model class, but
# the two OpenAI-family classes disagree on its name: OpenAIModel exposes a
# public `format_chunk`, OpenAIResponsesModel a private `_format_chunk`.
_CHUNK_FORMATTER_NAMES = ("_format_chunk", "format_chunk")

# Subclasses are memoized so repeated model construction reuses one type —
# keeps `isinstance` stable and avoids leaking a class per agent build.
_NORMALIZED_CLASSES: Dict[type, type] = {}


def _normalize_metadata_chunk(chunk: Any) -> Any:
    """Rewrite a formatted ``metadata`` chunk into Converse usage semantics.

    Args:
        chunk: The ``StreamEvent`` the base model produced for a usage event.

    Returns:
        ``chunk``, mutated in place when it carried a usage payload.
    """
    if not isinstance(chunk, dict):
        return chunk

    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        return chunk

    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        return chunk

    usage.update(normalize_usage(usage, UsageProvider.OPENAI))
    return chunk


def usage_normalized(base_cls: Any) -> Any:
    """Return a subclass of a Strands OpenAI-family model that reports disjoint usage.

    Args:
        base_cls: ``OpenAIModel`` or ``OpenAIResponsesModel`` (or a subclass).

    Returns:
        A memoized subclass whose chunk formatter normalizes usage. Anything
        that is not a class is returned unchanged — ``unittest.mock.patch``
        replaces a class with a non-type, and production always passes a real
        class.

    Raises:
        TypeError: If the class exposes neither chunk-formatter name. Failing
            loudly is deliberate: a silent fallthrough would double-bill every
            OpenAI-family token with no symptom other than the bill.
    """
    if not isinstance(base_cls, type):
        return base_cls

    cached = _NORMALIZED_CLASSES.get(base_cls)
    if cached is not None:
        return cached

    formatter_name = next(
        (name for name in _CHUNK_FORMATTER_NAMES if hasattr(base_cls, name)),
        None,
    )
    if formatter_name is None:
        raise TypeError(
            f"{base_cls.__name__} exposes neither "
            f"{' nor '.join(_CHUNK_FORMATTER_NAMES)}; the Strands chunk-"
            "formatting seam moved. OpenAI token usage cannot be normalized — "
            "update apis/shared/models/usage_normalization.py."
        )

    def _formatter(self: Any, event: Dict[str, Any], **kwargs: Any) -> Any:
        chunk = getattr(super(subclass, self), formatter_name)(event, **kwargs)
        return _normalize_metadata_chunk(chunk)

    _formatter.__name__ = formatter_name
    _formatter.__qualname__ = f"UsageNormalized{base_cls.__name__}.{formatter_name}"
    _formatter.__doc__ = (
        "Format a chunk, then restore Bedrock-Converse token-bucket semantics."
    )

    subclass = type(
        f"UsageNormalized{base_cls.__name__}",
        (base_cls,),
        {
            formatter_name: _formatter,
            "__doc__": (
                f"{base_cls.__name__} that reports disjoint token buckets.\n\n"
                "See apis/shared/models/usage_normalization.py — OpenAI's "
                "inclusive `input_tokens` is double-billed by our cost paths "
                "otherwise."
            ),
        },
    )

    _NORMALIZED_CLASSES[base_cls] = subclass
    logger.debug("Installed usage normalization on %s", base_cls.__name__)
    return subclass
