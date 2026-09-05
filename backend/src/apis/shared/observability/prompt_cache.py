"""Prompt-cache economics: prefix fingerprints and per-call cache classification.

Bedrock prompt caching is exact-prefix-match with a ~5-minute sliding TTL.
When the request prefix (toolConfig + system prompt + prior message history)
is byte-identical to the previous call's, tokens are read from cache at a
steep discount; any divergence forces a full cache re-write at a premium.
A production audit (session aecd387d, $1.60) showed 75% of spend was
avoidable re-writes caused by nondeterministic prefix assembly — and proving
that took hours of manual forensics. This module makes the whole class
measurable:

- ``fingerprint_*`` produce short stable hashes of the three prefix
  components so consecutive metadata rows show *which* component changed
  when a cache miss happens.
- ``classify_cache_status`` labels each model call from its token usage and
  the previous call's row.
- ``compute_wasted_usd`` prices the avoidable portion of a re-write.

A nonzero read is *not* proof the prefix was cached: the 2026-08-05 compaction
spiral read an 11k tools+system segment while re-writing 190k of history on
every one of 56 calls, and the classifier called all 56 a ``hit`` with
``wastedUsd = 0``. ``partial_miss`` (see ``PARTIAL_MISS_WRITE_READ_RATIO``)
is the status that separates "read the prefix, wrote the tail" from "read a
sliver, wrote the prefix."

Pure functions only — no AWS calls — so they are unit-testable and safe to
import from any package (agents/, app_api, inference_api all may import
``apis.shared``).
"""

import hashlib
import json
import os
from enum import Enum
from typing import Any, Mapping, Optional

# Kill switch for the whole prompt-cache observability layer (fingerprint
# hook, per-call cacheStatus derivation + session rollups, EMF emission).
# Default ON; only the literal string "false" disables it — an empty or
# unset value stays enabled (workflow env vars can materialize as "").
PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV = "PROMPT_CACHE_OBSERVABILITY_ENABLED"


def prompt_cache_observability_enabled() -> bool:
    """Whether prompt-cache observability is enabled (env kill switch).

    Read per call (no module-level caching) so tests and live config changes
    behave predictably; the env read is negligible next to the DynamoDB
    lookup and hash work it gates.
    """
    return os.environ.get(PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV, "").lower() != "false"

# Prompt-cache TTL (sliding, seconds). A gap between consecutive model calls
# larger than this means the cache entry legitimately expired — the re-write
# was unavoidable.
#
# The TTL is a property of the MODEL, not of this module. Bedrock/Anthropic
# prompt caching is a ~5-minute sliding window; the OpenAI Responses API on
# `bedrock-runtime` holds entries for 30 minutes. Treating every model as
# 5 minutes is wrong by 6x for GPT-5.6, and wrong in the direction that
# HIDES waste: a gap inside the real TTL gets called `miss_ttl_expired`
# (unavoidable) instead of `miss_avoidable`, and `partial_miss` — whose gate
# is `gap <= ttl` — silently degrades to `hit`. Both suppress `wastedUsd`,
# which is the metric that exists to catch exactly this.
#
# `CACHE_TTL_SECONDS` is retained as the default (and for importers) but
# callers that know the model should pass `ttl_seconds` from
# :func:`cache_ttl_seconds_for` instead.
CACHE_TTL_SECONDS = 300
DEFAULT_CACHE_TTL_SECONDS = CACHE_TTL_SECONDS

# OpenAI Responses API on bedrock-runtime: entries live 30 minutes.
# Corroborated by the Price List API's own SKU naming for these models —
# the cache-write usage types are `...-cache-write-tokens-30m-...`.
OPENAI_RESPONSES_CACHE_TTL_SECONDS = 1800

# Providers whose models use the OpenAI Responses 30-minute TTL.
#
# Deliberately NOT the whole OpenAI family. `mantle` serves `openai.gpt-5.4`
# with implicit-only caching whose retention AWS does not document as 30m,
# and guessing there would re-introduce the same class of error in the other
# direction (over-reporting waste). Only the model whose TTL is documented
# gets the longer window.
_OPENAI_RESPONSES_TTL_PROVIDERS = frozenset({"bedrock-responses"})


def cache_ttl_seconds_for(
    provider: Optional[str] = None,
    model_id: Optional[str] = None,
) -> int:
    """Prompt-cache TTL in seconds for the model that served a call.

    Args:
        provider: The model's registered provider (``bedrock``, ``mantle``,
            ``bedrock-responses``, ...). The authoritative signal.
        model_id: Model id, used only as a fallback when the provider is
            absent on older metadata rows written before the field existed.

    Returns:
        The TTL to measure this call's gap against. Falls back to
        :data:`DEFAULT_CACHE_TTL_SECONDS` for anything unrecognized —
        under-reporting waste rather than inventing it.
    """
    if provider and provider.lower() in _OPENAI_RESPONSES_TTL_PROVIDERS:
        return OPENAI_RESPONSES_CACHE_TTL_SECONDS
    if not provider and model_id:
        # Historical rows: infer from the inference-profile-prefixed id the
        # bedrock-runtime transport requires (us./global. + openai.gpt-5.6).
        normalized = model_id.lower()
        if "openai.gpt-5.6" in normalized:
            return OPENAI_RESPONSES_CACHE_TTL_SECONDS
    return DEFAULT_CACHE_TTL_SECONDS

# How many times larger than the cache *read* a cache *write* has to be before
# a nonzero read stops meaning "the prefix was cached" and starts meaning "a
# small leading segment was cached and everything after it was re-written".
#
# A healthy steady-state turn reads the whole prior prefix and writes only the
# appended tail, so write ≪ read. The compaction spiral (prod, 2026-08-05)
# inverted that: 11k read (the tools + system segments) against 190k written
# (the entire conversation history) on every turn, 18:1 — and every one of
# those calls was classified `hit`. Ratio 3 sits far above any normal turn's
# write:read and far below the ratios the failure mode produces, so it
# separates the two without a tuning exercise. Raising it under-reports;
# lowering it starts catching ordinary long-tail turns.
PARTIAL_MISS_WRITE_READ_RATIO = 3


class CacheStatus(str, Enum):
    """Derived per-call prompt-cache outcome.

    - ``first_write``: the initial, expected cache population — either no
      previous call row for the session, or every activity-bearing signal
      says no cache existed yet (the previous call was ``uncached``, e.g.
      the prompt was below the model's minimum cacheable prefix, so there
      was nothing to read from).
    - ``hit``: tokens were read from cache and the write alongside them is
      the ordinary appended tail (see ``partial_miss`` for the other case).
    - ``partial_miss``: tokens *were* read, but the write dwarfs the read
      (``> PARTIAL_MISS_WRITE_READ_RATIO ×``) while the entry that served the
      read was still live — a leading segment hit and the rest of the prefix
      was re-written. Costs as much as a full miss and used to hide inside
      ``hit``; the bug class ``miss_avoidable`` catches for a *cold* prefix.
    - ``miss_ttl_expired``: nothing read, cache re-written, and the gap since
      the previous call exceeded the cache TTL — unavoidable.
    - ``miss_avoidable``: nothing read, cache re-written, previous call had a
      live cache entry within the TTL — the prefix must have changed. This is
      the bug class the fingerprints exist to diagnose.
    - ``uncached``: no cache activity at all (caching disabled, non-Bedrock
      provider, or prompt below the minimum cacheable length).
    """

    FIRST_WRITE = "first_write"
    HIT = "hit"
    PARTIAL_MISS = "partial_miss"
    MISS_TTL_EXPIRED = "miss_ttl_expired"
    MISS_AVOIDABLE = "miss_avoidable"
    UNCACHED = "uncached"


def fingerprint_text(text: Optional[str]) -> str:
    """Short stable hash of a text blob (e.g. the system prompt)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def fingerprint_canonical_json(obj: Any) -> str:
    """Short stable hash of a JSON-serializable structure.

    Dict keys are sorted so key insertion order never changes the hash, but
    list order is preserved — deliberately, because list order (tool specs,
    message history, content blocks) is exactly what Bedrock's exact-prefix
    match is sensitive to.
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def classify_cache_status(
    cache_read_tokens: int,
    cache_write_tokens: int,
    previous_call_exists: bool,
    gap_seconds: Optional[float],
    previous_cached_prefix_tokens: Optional[int] = None,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> CacheStatus:
    """Classify one model call's cache outcome.

    Args:
        cache_read_tokens: ``cacheReadInputTokens`` for this call.
        cache_write_tokens: ``cacheWriteInputTokens`` for this call.
        previous_call_exists: Whether the session has an earlier call row.
        gap_seconds: Seconds since the previous call row's timestamp; None
            when unknown (treated conservatively as expired, not avoidable).
        previous_cached_prefix_tokens: Previous call's cacheRead + cacheWrite
            token total, or None when unknown. Zero means the previous call
            was uncached (e.g. prompt below the model's minimum cacheable
            prefix), so no cache entry existed for this call to read.
        ttl_seconds: The serving model's prompt-cache TTL. Defaults to the
            Bedrock/Anthropic 5-minute window; callers that know the model
            should pass :func:`cache_ttl_seconds_for`. A TTL shorter than the
            model's real one silently suppresses both ``partial_miss`` and
            ``miss_avoidable``, and with them ``wastedUsd``.
    """
    if cache_read_tokens > 0:
        # A read proves *something* was cached — but not that the prefix was.
        # When the write dwarfs the read against a live entry, most of the
        # prefix was re-written at the write premium and only a leading
        # segment hit. That is waste, and calling it a `hit` is what let the
        # compaction spiral run for 56 calls with `wastedUsd = 0`.
        #
        # Gated on the TTL for the same reason `miss_avoidable` is: past the
        # TTL the entry is gone and re-writing it is unavoidable, so a stale
        # or unknown gap (`None` — no same-prefix predecessor in the lookback
        # window) stays a `hit`. Under-reporting keeps the metric trustworthy;
        # crying wolf is what made the old one useless (#753).
        if (
            previous_call_exists
            and cache_write_tokens > PARTIAL_MISS_WRITE_READ_RATIO * cache_read_tokens
            and gap_seconds is not None
            and gap_seconds <= ttl_seconds
        ):
            return CacheStatus.PARTIAL_MISS
        return CacheStatus.HIT
    if cache_write_tokens <= 0:
        return CacheStatus.UNCACHED
    if not previous_call_exists:
        return CacheStatus.FIRST_WRITE
    if previous_cached_prefix_tokens is not None and previous_cached_prefix_tokens <= 0:
        # The previous call wrote nothing to the cache, so there was no entry
        # to read from — this write is the session's first real population
        # (typically the first prompt to cross the minimum cacheable length),
        # not a miss of any kind.
        return CacheStatus.FIRST_WRITE
    if gap_seconds is None or gap_seconds > ttl_seconds:
        return CacheStatus.MISS_TTL_EXPIRED
    return CacheStatus.MISS_AVOIDABLE


def compute_wasted_usd(
    cache_status: CacheStatus,
    cache_write_tokens: int,
    previous_cached_prefix_tokens: Optional[int],
    pricing_snapshot: Optional[Mapping[str, Any]],
    cache_read_tokens: int = 0,
) -> float:
    """USD wasted by an avoidable cache re-write; 0.0 for every other status.

    Prices ``miss_avoidable`` and ``partial_miss`` identically — both re-wrote
    prefix bytes that a live cache entry already held, and the dollars are the
    same whether the wasted share is 100% or 95%.

    The waste is the re-written portion of the prefix that was already cached
    on the previous call (``min(cacheWrite, previous cacheRead + cacheWrite)``;
    falls back to the full cacheWrite when the previous split is unknown),
    priced at the cache-write premium over the cache-read rate the tokens
    *should* have cost. On a ``partial_miss`` the tokens this call actually
    read come off the previously-cached cap first — they were served from
    cache, so they cannot also have been re-written.

    Args:
        cache_status: Result of :func:`classify_cache_status`.
        cache_write_tokens: ``cacheWriteInputTokens`` for this call.
        previous_cached_prefix_tokens: Previous call's cacheRead + cacheWrite
            token total, or None when unavailable.
        pricing_snapshot: The row's ``pricingSnapshot`` dict (camelCase keys).
        cache_read_tokens: ``cacheReadInputTokens`` for this call; only
            meaningful for ``partial_miss``, which is the sole status that
            reads and re-writes in the same call.
    """
    if cache_status not in (CacheStatus.MISS_AVOIDABLE, CacheStatus.PARTIAL_MISS):
        return 0.0
    if not pricing_snapshot or cache_write_tokens <= 0:
        return 0.0

    # `or 0` (not `.get(..., 0)`) — rows can store an explicit None.
    write_price = pricing_snapshot.get("cacheWritePricePerMtok") or 0
    read_price = pricing_snapshot.get("cacheReadPricePerMtok") or 0
    premium_per_mtok = write_price - read_price
    if premium_per_mtok <= 0:
        return 0.0

    rewritten = cache_write_tokens
    if previous_cached_prefix_tokens is not None and previous_cached_prefix_tokens > 0:
        previously_cached = previous_cached_prefix_tokens
        if cache_status is CacheStatus.PARTIAL_MISS:
            previously_cached = max(0, previously_cached - max(0, cache_read_tokens))
        rewritten = min(cache_write_tokens, previously_cached)

    return (rewritten / 1_000_000) * premium_per_mtok
