"""In-process LRU cache for `SessionRecord` lookups.

The cache window is bounded by the refresh leeway: a record is only safe to
serve from cache as long as its access token still has more than the leeway
window left. We size the TTL to match so a cache hit is always a record that
the refresh middleware would *not* refresh anyway.

This is not a security boundary — it's a hot-path optimization for
many-tabs-on-one-page bursts where the same session id hits the BFF dozens
of times per second. The repository remains the source of truth.

Logout caveat (Phase 3): when `/auth/logout` deletes the DDB row, in-process
caches on other tasks (and on the same task, until the TTL ticks past) will
keep serving the old record for up to `refresh_leeway_seconds`. Phase 3's
logout handler must call `cache.invalidate(session_id)` locally and accept
that other tasks lag by at most one TTL window — full coherence requires the
Phase 7 multi-task coordination work.
"""

from __future__ import annotations

from typing import Optional

from cachetools import TTLCache

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
