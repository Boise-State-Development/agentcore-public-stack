"""Shared observability helpers (prompt-cache economics, EMF metrics)."""

from apis.shared.observability.prompt_cache import (
    CACHE_TTL_SECONDS,
    DEFAULT_CACHE_TTL_SECONDS,
    OPENAI_RESPONSES_CACHE_TTL_SECONDS,
    cache_ttl_seconds_for,
    PARTIAL_MISS_WRITE_READ_RATIO,
    PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV,
    prompt_cache_observability_enabled,
    CacheStatus,
    classify_cache_status,
    compute_wasted_usd,
    fingerprint_canonical_json,
    fingerprint_text,
)
from apis.shared.observability.emf import (
    emit_prompt_cache_metrics,
    emit_session_cache_rollup,
)

__all__ = [
    "CACHE_TTL_SECONDS",
    "DEFAULT_CACHE_TTL_SECONDS",
    "OPENAI_RESPONSES_CACHE_TTL_SECONDS",
    "cache_ttl_seconds_for",
    "PARTIAL_MISS_WRITE_READ_RATIO",
    "PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV",
    "prompt_cache_observability_enabled",
    "CacheStatus",
    "classify_cache_status",
    "compute_wasted_usd",
    "fingerprint_canonical_json",
    "fingerprint_text",
    "emit_prompt_cache_metrics",
    "emit_session_cache_rollup",
]
