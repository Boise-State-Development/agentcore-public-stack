"""Usage for the model call a Stop or a dropped socket cut off.

A completed call is metered from the provider's own terminal ``metadata``
event. A cut one often never delivers it: the stream stops at a
``content_block_delta`` and ``accumulated_metadata['usage']`` stays empty. The
calls the turn already finished are metered from their real usage; this module
is only about the one still in flight when the turn was torn down.

Before this, that call was recorded as the context-attribution projection in
``inputTokens`` with ``outputTokens: 0`` and no cache buckets — three separate
misstatements of one 9-second turn on dev (session e31c51ad, 2026-10-20):

* ``outputTokens: 0`` for an answer the user had watched render, and which is
  persisted in the conversation;
* the whole prompt in ``inputTokens``, i.e. priced at the plain uncached rate,
  although the next call in that session read 39,972 tokens from cache and so
  proved a cache *write* had happened — billed at 1.25x base, not 1x;
* ``cacheStatus: "uncached"``, which asserts no cache activity at all and is
  the field people read when chasing a cache regression.

What is recoverable and what is not:

* **Input** is recoverable and close to exact. ``get_projected_input_tokens``
  prefers a native CountTokens of the request this call actually sent.
* **Output** is recoverable as a lower bound: the text we streamed and
  persisted, counted with the serving model's own tokenizer. It cannot cover
  tool-use arguments or reasoning tokens the provider billed and we never saw,
  so it under-reports rather than invents.
* **The read/write split of the input** is *not* recoverable. What is knowable
  is that with cachePoints on the request, plain ``inputTokens`` is the one
  bucket that cannot be right: Bedrock bills an uncached prefix as a write and
  a cached one as a read, never as plain input. So the projection is attributed
  across the cache buckets using the last call whose usage we *did* see — in
  this process, which for a mid-turn Stop is a call seconds old — and wholly to
  the write bucket when there is no such call.

Every figure built here is an estimate and is labelled as one. The row carries
``usageEstimated``, a field -> method map, and its ``cacheStatus`` is written
``estimated`` rather than a classification derived from guessed buckets: a
status is the signal an investigation starts from, and a plausible-looking
``first_write`` on numbers nobody measured is worse than an honest "not
measured". ``wastedUsd`` and the avoidable-miss metrics are suppressed for the
same reason (``apis.shared.sessions.metadata._derive_cache_observability``).

All of this runs after the stream is over, inside the interruption's shielded
teardown task — never on the path to a first token.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Methods recorded in the row's ``usageEstimated`` map, per field.
METHOD_NATIVE_COUNT = "native_count"      # Bedrock CountTokens on the real payload
METHOD_PROJECTION = "projection"          # Strands' usage-anchored input projection
METHOD_STREAMED_TEXT = "streamed_text"    # counted from the text we streamed
METHOD_ESTIMATE = "chars_per_token"       # the chars/4 fallback
METHOD_PRIOR_CALL = "prior_call_prefix"   # split from the last call that reported usage
METHOD_FIRST_WRITE = "assumed_first_write"
METHOD_UNCACHED = "no_cache_points"
METHOD_BELOW_MINIMUM = "below_cache_minimum"  # cachePoints sent, prompt too short to cache

#: Smallest prefix Bedrock will cache, per Claude model, from the Bedrock user
#: guide's explicit-caching table (re-read 2026-10-22). Below it a cachePoint
#: is ignored and the prompt is billed as plain input, so the cache buckets
#: must stay empty.
#:
#: Bedrock's table is the one that bills us, and it is not Anthropic's: it puts
#: Opus 4.7 at 4,096 where Anthropic's own page says 2,048. The minimum is also
#: not monotonic across generations — 512 on the 5.x line, 4,096 on Haiku 4.5
#: — so no single floor is safe. A flat 1,024 put every 1,024-4,095-token stop
#: on Haiku 4.5, the default model, into the write bucket; measured on dev
#: (2026-10-22), the next call in that conversation read nothing from cache.
#:
#: Keyed ``(family, major, minor)``, with ``minor`` ``None`` for a model that
#: has none (``claude-sonnet-5``).
_MIN_CACHEABLE_PREFIX_TOKENS: Dict[Tuple[str, int, Optional[int]], int] = {
    ("opus", 5, 5): 512,
    ("opus", 5, None): 512,
    ("sonnet", 5, 5): 512,
    ("haiku", 5, 5): 512,
    ("fable", 5, 1): 512,
    ("fable", 5, None): 512,
    ("mythos", 5, 1): 512,
    ("mythos", 5, None): 512,
    ("opus", 4, 8): 1024,
    ("sonnet", 5, None): 1024,
    ("sonnet", 4, 6): 1024,
    ("sonnet", 4, 5): 1024,
    ("sonnet", 3, 7): 1024,
    ("sonnet", 3, 5): 1024,
    ("opus", 4, 7): 4096,
    ("opus", 4, 6): 4096,
    ("opus", 4, 5): 4096,
    ("haiku", 4, 5): 4096,
}

#: For a Claude id the table does not name (a newer model, an application
#: inference profile ARN): the commonest published minimum, and what this
#: module used for every model before the table existed.
DEFAULT_MIN_CACHEABLE_PREFIX_TOKENS = 1024

# `claude-haiku-4-5-20251001`, `claude-opus-5-5`, `claude-sonnet-5`; a two-digit
# minor is allowed, a date suffix is not mistaken for one.
_MODERN_CLAUDE_ID = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")
# `claude-3-7-sonnet-20250219`: version before family.
_LEGACY_CLAUDE_ID = re.compile(r"claude-(\d+)-(\d{1,2})-(opus|sonnet|haiku)")


def min_cacheable_prefix_tokens(model_id: Optional[str]) -> int:
    """Smallest prompt Bedrock will cache for ``model_id``.

    Reads the family and version out of any Bedrock spelling of the id — a bare
    model id, a ``us.``/``global.`` inference profile, a ``-v1:0`` suffix — and
    falls back to ``DEFAULT_MIN_CACHEABLE_PREFIX_TOKENS`` when it names no model
    in the table.
    """
    mid = (model_id or "").lower()
    match = _MODERN_CLAUDE_ID.search(mid)
    if match:
        family, major, minor = match.group(1), int(match.group(2)), match.group(3)
        key = (family, major, int(minor) if minor is not None else None)
        return _MIN_CACHEABLE_PREFIX_TOKENS.get(key, DEFAULT_MIN_CACHEABLE_PREFIX_TOKENS)
    match = _LEGACY_CLAUDE_ID.search(mid)
    if match:
        key = (match.group(3), int(match.group(1)), int(match.group(2)))
        return _MIN_CACHEABLE_PREFIX_TOKENS.get(key, DEFAULT_MIN_CACHEABLE_PREFIX_TOKENS)
    return DEFAULT_MIN_CACHEABLE_PREFIX_TOKENS

#: Characters per token for the fallback output estimate, matching
#: ``apis.shared.memory.tokens.estimate_tokens``.
_CHARS_PER_TOKEN = 4


def estimate_text_tokens(text: str) -> int:
    """Cheap token estimate (ceil of chars / 4)."""
    return -(-len(text) // _CHARS_PER_TOKEN)


def last_reported_cached_prefix(messages: Any) -> Optional[int]:
    """Cached prefix size after the newest call that reported usage, or ``None``.

    Reads ``metadata.usage`` off the newest assistant message carrying one —
    the same signal ``_has_usage_baseline`` uses — and returns
    ``cacheRead + cacheWrite``, which is how much of that call's prompt
    Bedrock held in cache once it answered. For a Stop during a multi-call
    turn that message is seconds old, so the entry is still live and the cut
    call would have read it.

    ``None`` when no message reports usage: a first turn, or a conversation
    restored into a fresh container, where nothing in process witnessed a
    cache entry.
    """
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = (message.get("metadata") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            cached = int(usage.get("cacheReadInputTokens") or 0) + int(
                usage.get("cacheWriteInputTokens") or 0
            )
        except (TypeError, ValueError):
            return None
        return cached or None
    return None


def attribute_input_tokens(
    total: int,
    *,
    cache_points_sent: bool,
    prior_cached_prefix: Optional[int],
    min_cacheable_tokens: int = DEFAULT_MIN_CACHEABLE_PREFIX_TOKENS,
) -> Tuple[Dict[str, int], str]:
    """Split a projected prompt size across the billing buckets.

    Pure arithmetic over the facts the caller can establish: whether this
    request carried cachePoints at all, the serving model's minimum cacheable
    prefix (``min_cacheable_prefix_tokens``), and how large a cache entry the
    last call we saw usage for left behind.

    Returns the bucket dict plus the method name recorded on the row, so a
    reader can tell "split from the call before it" from "assumed to be the
    first write" without re-deriving either.
    """
    total = max(0, int(total))
    plain = {"inputTokens": total, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    # No caching on this request (caching off, or a transport Strands sends no
    # cachePoints for), or a prompt too short for this model to cache at all:
    # plain input is the correct bucket, not a fallback.
    if not cache_points_sent:
        return plain, METHOD_UNCACHED
    if total < min_cacheable_tokens:
        return plain, METHOD_BELOW_MINIMUM
    if prior_cached_prefix:
        read = min(int(prior_cached_prefix), total)
        return {
            "inputTokens": 0,
            "cacheReadInputTokens": read,
            "cacheWriteInputTokens": total - read,
        }, METHOD_PRIOR_CALL
    return {
        "inputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": total,
    }, METHOD_FIRST_WRITE


def _cache_points_sent(wrapper: Any) -> bool:
    """Whether this turn's requests carried Bedrock cachePoints.

    ``bedrock_cache_points_supported`` is the single predicate the system and
    tools cachePoints are both placed from, so it is the same answer the
    request was built with rather than a second opinion about it. Falls back to
    the plain ``caching_enabled`` flag for wrappers without it (tests, other
    transports), and to False when neither is readable — under-claiming cache
    activity rather than inventing it.
    """
    model_config = getattr(wrapper, "model_config", None)
    if model_config is None:
        return False
    predicate = getattr(model_config, "bedrock_cache_points_supported", None)
    if callable(predicate):
        try:
            return bool(predicate())
        except Exception:  # noqa: BLE001 - never fail metering over a predicate
            logger.debug("Could not read cache-point support", exc_info=True)
    return bool(getattr(model_config, "caching_enabled", False))


def _serving_model_id(wrapper: Any) -> Optional[str]:
    """The model id this turn's requests were built for, when readable."""
    model_id = getattr(getattr(wrapper, "model_config", None), "model_id", None)
    return model_id if isinstance(model_id, str) else None


async def _count_output_tokens(strands_agent: Any, text: str) -> Tuple[int, str]:
    """Tokens of the partial answer, with the method that produced them."""
    if not text:
        return 0, METHOD_STREAMED_TEXT
    try:
        from agents.main_agent.session.hooks.context_attribution import count_text_tokens

        counted = await count_text_tokens(getattr(strands_agent, "model", None), text)
        if counted is not None:
            return int(counted), METHOD_NATIVE_COUNT
    except Exception:  # noqa: BLE001 - metering must never fail over a count
        logger.debug("Could not count interrupted-turn output natively", exc_info=True)
    return estimate_text_tokens(text), METHOD_ESTIMATE


async def build_interrupted_call_usage(
    *,
    strands_agent: Any,
    wrapper: Any,
    partial_text: str,
) -> Optional[Dict[str, Any]]:
    """Usage + estimate labels for the call that was cut off, or ``None``.

    ``None`` when the input size cannot be established at all — there is no
    honest row to write then, and the previous behaviour (write nothing) is
    the right one. Returns the slot payload the stream coordinator merges into
    ``per_message_metadata``: ``{"usage": {...}, "usageEstimate": {...}}``.
    """
    from agents.main_agent.session.hooks.context_attribution import (
        get_projected_input_tokens,
    )

    try:
        projected = get_projected_input_tokens(strands_agent)
    except Exception:  # noqa: BLE001 - best-effort enrichment only
        logger.debug("Could not project interrupted-turn input usage", exc_info=True)
        return None
    if not projected or projected <= 0:
        return None

    buckets, cache_method = attribute_input_tokens(
        int(projected),
        cache_points_sent=_cache_points_sent(wrapper),
        prior_cached_prefix=last_reported_cached_prefix(
            getattr(strands_agent, "messages", None)
        ),
        min_cacheable_tokens=min_cacheable_prefix_tokens(_serving_model_id(wrapper)),
    )
    output_tokens, output_method = await _count_output_tokens(strands_agent, partial_text or "")

    usage = {
        **buckets,
        "outputTokens": output_tokens,
        "totalTokens": (
            buckets["inputTokens"]
            + buckets["cacheReadInputTokens"]
            + buckets["cacheWriteInputTokens"]
            + output_tokens
        ),
    }
    estimate = {
        "reason": "interrupted",
        "inputTokens": METHOD_PROJECTION,
        "outputTokens": output_method,
        "cacheSplit": cache_method,
    }
    logger.info(
        "Estimated usage for the interrupted call: input=%d read=%d write=%d output=%d "
        "(cache split: %s, output: %s)",
        usage["inputTokens"], usage["cacheReadInputTokens"],
        usage["cacheWriteInputTokens"], output_tokens, cache_method, output_method,
    )
    return {"usage": usage, "usageEstimate": estimate}
