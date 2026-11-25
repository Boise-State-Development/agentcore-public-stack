"""In-process LRU cache for `SessionRecord` lookups.

The cache window is bounded by the refresh leeway: a record is only safe to
serve from cache as long as its access token still has more than the leeway
window left. We size the TTL to match so a cache hit is always a record that
the refresh middleware would *not* refresh anyway.

This is not a security boundary — it's a hot-path optimization for
many-tabs-on-one-page bursts where the same session id hits the BFF dozens
of times per second. The repository remains the source of truth.

Logout: `/auth/logout` deletes the DDB row, invalidates this process's entry
at once, and publishes a revocation (`SessionRepository.revoke`). Every other
app-api process drops its entry within `revocations.POLL_INTERVAL_SECONDS` via
`run_revocation_watcher`. While the feed cannot be read, the bound falls back
to one TTL window.
"""

from __future__ import annotations

from threading import Lock
from typing import Optional

from cachetools import TTLCache

from .config import BFFConfig
from .models import SessionRecord


class SessionCache:
    """Bounded TTL cache keyed by session id."""

    def __init__(self, *, max_size: int = 1024, ttl_seconds: int = 60) -> None:
        # Floor TTL at 1s; cachetools rejects 0/negative.
        self._cache: TTLCache = TTLCache(maxsize=max_size, ttl=max(1, ttl_seconds))

    def get(self, session_id: str) -> Optional[SessionRecord]:
        return self._cache.get(session_id)

    def set(self, record: SessionRecord) -> None:
        self._cache[record.session_id] = record

    def invalidate(self, session_id: str) -> None:
        self._cache.pop(session_id, None)

    def clear(self) -> None:
        self._cache.clear()


# Process-wide singleton so the refresh middleware, the logout route and the
# revocation watcher share the same cache. The middleware seeds it on every
# successful resolve; logout and the watcher call `invalidate`.
_default_cache: Optional[SessionCache] = None
_default_cache_lock = Lock()


def get_default_cache() -> SessionCache:
    global _default_cache
    if _default_cache is not None:
        return _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = SessionCache(
                ttl_seconds=BFFConfig.from_env().refresh_leeway_seconds
            )
    return _default_cache


def _reset_default_cache_for_tests() -> None:
    """Drop the singleton so tests can rebuild it under their own fixtures."""
    global _default_cache
    with _default_cache_lock:
        _default_cache = None
