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

# Bedrock prompt-cache TTL (sliding, seconds). A gap between consecutive
# model calls larger than this means the cache entry legitimately expired —
# the re-write was unavoidable.
CACHE_TTL_SECONDS = 300


class CacheStatus(str, Enum):
    """Derived per-call prompt-cache outcome.

    - ``first_write``: no previous call row for the session — the initial,
      expected cache population.
    - ``hit``: tokens were read from cache (``cacheReadInputTokens > 0``).
      Partial re-writes of a changed suffix still count as hits.
    - ``miss_ttl_expired``: nothing read, cache re-written, and the gap since
      the previous call exceeded the cache TTL — unavoidable.
    - ``miss_avoidable``: nothing read, cache re-written, previous call was
      within the TTL — the prefix must have changed. This is the bug class
      the fingerprints exist to diagnose.
    - ``uncached``: no cache activity at all (caching disabled, non-Bedrock
      provider, or prompt below the minimum cacheable length).
    """

    FIRST_WRITE = "first_write"
    HIT = "hit"
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
) -> CacheStatus:
    """Classify one model call's cache outcome.

    Args:
        cache_read_tokens: ``cacheReadInputTokens`` for this call.
        cache_write_tokens: ``cacheWriteInputTokens`` for this call.
        previous_call_exists: Whether the session has an earlier call row.
        gap_seconds: Seconds since the previous call row's timestamp; None
            when unknown (treated conservatively as expired, not avoidable).
    """
    if cache_read_tokens > 0:
        return CacheStatus.HIT
    if cache_write_tokens <= 0:
        return CacheStatus.UNCACHED
    if not previous_call_exists:
        return CacheStatus.FIRST_WRITE
    if gap_seconds is None or gap_seconds > CACHE_TTL_SECONDS:
        return CacheStatus.MISS_TTL_EXPIRED
    return CacheStatus.MISS_AVOIDABLE


def compute_wasted_usd(
    cache_status: CacheStatus,
    cache_write_tokens: int,
    previous_cached_prefix_tokens: Optional[int],
    pricing_snapshot: Optional[Mapping[str, Any]],
) -> float:
    """USD wasted by an avoidable cache re-write; 0.0 for every other status.

    The waste is the re-written portion of the prefix that was already cached
    on the previous call (``min(cacheWrite, previous cacheRead + cacheWrite)``;
    falls back to the full cacheWrite when the previous split is unknown),
    priced at the cache-write premium over the cache-read rate the tokens
    *should* have cost.

    Args:
        cache_status: Result of :func:`classify_cache_status`.
        cache_write_tokens: ``cacheWriteInputTokens`` for this call.
        previous_cached_prefix_tokens: Previous call's cacheRead + cacheWrite
            token total, or None when unavailable.
        pricing_snapshot: The row's ``pricingSnapshot`` dict (camelCase keys).
    """
    if cache_status is not CacheStatus.MISS_AVOIDABLE:
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
        rewritten = min(cache_write_tokens, previous_cached_prefix_tokens)

    return (rewritten / 1_000_000) * premium_per_mtok
