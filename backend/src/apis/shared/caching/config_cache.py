"""TTL + single-flight cache for tenant-global config item lists.

The SPA's first load reads several catalogs that are the *same for everyone*
on the deployment — the model catalog, the tool catalog, the system-prompt
list, the connector list. Each read was an uncached DynamoDB scan (or, for
connectors, a GSI query) that also re-parsed every row. A classroom signing in
together ran one of each per student.

Two properties matter here, and the second is the one that actually shows up
under a burst:

**TTL.** A catalog changes when an admin edits it, which is rare, so a short
window collapses ~all of the reads.

**Single flight.** A cold cache with 300 simultaneous requests would otherwise
produce 300 concurrent scans — the stampede lands at exactly the moment the
burst does, which is the case this exists to prevent. One loader runs per key;
everyone else awaits it.

## What is cached: raw items, not parsed objects

Entries hold the **raw DynamoDB items**, and callers re-parse on every read.

That is deliberate, and it is not a missed optimization. Callers mutate the
objects these lists produce: ``hydrate_model_roles`` documents itself as
"models to hydrate (mutated in place and returned)" and writes
``allowed_app_roles`` onto each model, and ``list_tools_with_roles`` does the
same to ``ToolDefinition``. Caching parsed objects would hand every caller a
reference to one shared instance, so an admin opening the models page would
write derived, display-only role fields onto the objects subsequently served
to every user — and per CLAUDE.md ``allowedAppRoles`` is precisely the field
that must never be mistaken for a grant.

Re-parsing costs microseconds of CPU against a network round trip of ~50-100ms
(measured medians: ``/models`` 46ms, ``/tools/`` 91ms), so the saving that
matters is kept while the shared-mutable-state class of bug is designed out
entirely — including for callers added later, who cannot be audited in advance.

The list itself is shallow-copied on read so a caller cannot append to or sort
the cached list. The item dicts are shared; parsers treat them as read-only.

## Scope: per process, like the rest of our caches

This is an in-process cache, matching ``AppRoleCache`` and the roles-version
watermark. A write invalidates only the task that served it, so a second ECS
task can serve a stale catalog until its entry expires. That is why the default
TTL is short (60s) rather than the 5-10 minutes ``AppRoleCache`` uses for
authorization data: an admin edit should not appear to "not take" for minutes.
Bounding it to a minute keeps the burst win (a classroom arrives inside a few
seconds) while keeping admin feedback close to immediate.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60

# Cache keys. Named here rather than passed as bare strings at each call site
# so a typo cannot silently create a second, never-invalidated entry.
MANAGED_MODELS = "managed_models"
TOOL_CATALOG = "tool_catalog"
SYSTEM_PROMPTS = "system_prompts"

# Providers are read two ways — the enabled-only GSI query that `/connectors/`
# uses on first load, and the full scan the admin console uses. They are
# separate entries because they return different rows, and BOTH are dropped on
# any provider write: `enabled` is part of the GSI partition key, so flipping it
# moves a provider between the two result sets.
OAUTH_PROVIDERS_ENABLED = "oauth_providers:enabled"
OAUTH_PROVIDERS_ALL = "oauth_providers:all"

Items = List[dict]
Loader = Callable[[], Awaitable[Items]]


def _ttl_seconds() -> int:
    """Cache lifetime, from ``CONFIG_CACHE_TTL_SECONDS``.

    A non-numeric or negative value falls back to the default rather than
    raising: a malformed env var should degrade to sane caching, not break
    every catalog read on the deployment.
    """
    raw = os.environ.get("CONFIG_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "CONFIG_CACHE_TTL_SECONDS=%r is not an integer; using %ds",
            raw,
            DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS
    if value < 0:
        logger.warning(
            "CONFIG_CACHE_TTL_SECONDS=%d is negative; using %ds",
            value,
            DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS
    return value


class ConfigListCache:
    """TTL cache with single-flight loading, holding raw DynamoDB items."""

    def __init__(self) -> None:
        # key -> (expires_at_monotonic, items)
        self._entries: Dict[str, tuple[float, Items]] = {}
        # key -> lock serialising loads for that key. Created lazily; an
        # asyncio.Lock built at import time would be fine on 3.10+ but the
        # per-key dict has to be lazy regardless.
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _live_entry(self, key: str) -> Optional[Items]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, items = entry
        if time.monotonic() >= expires_at:
            return None
        return items

    async def get_or_load(self, key: str, loader: Loader) -> Items:
        """Return cached items for ``key``, loading via ``loader`` on a miss.

        Concurrent callers that miss together run ``loader`` exactly once; the
        rest wait on the same load and read the result.

        A loader that raises propagates to every waiter and caches nothing, so
        the next request retries rather than inheriting a failure. An expired
        entry is not served as a fallback: these catalogs drive authorization
        surfaces, and serving one the store may have since changed is worse
        than surfacing the error the caller already knows how to handle.
        """
        from apis.shared.feature_flags import config_cache_enabled

        if not config_cache_enabled():
            return await loader()

        cached = self._live_entry(key)
        if cached is not None:
            return list(cached)

        async with self._lock_for(key):
            # Re-check: another coroutine may have loaded while we queued.
            cached = self._live_entry(key)
            if cached is not None:
                return list(cached)

            items = await loader()
            self._entries[key] = (time.monotonic() + _ttl_seconds(), items)
            logger.debug("Config cache filled %s (%d items)", key, len(items))
            return list(items)

    def invalidate(self, key: str) -> None:
        """Drop ``key`` so the next read reloads from DynamoDB.

        Called from the write paths themselves rather than from admin routes,
        so a new mutation cannot forget to invalidate.
        """
        if self._entries.pop(key, None) is not None:
            logger.debug("Config cache invalidated %s", key)

    def clear(self) -> None:
        """Drop every entry. For tests and process-wide invalidation."""
        self._entries.clear()


_cache: Optional[ConfigListCache] = None


def get_config_cache() -> ConfigListCache:
    """Return the process-wide cache instance."""
    global _cache
    if _cache is None:
        _cache = ConfigListCache()
    return _cache


async def get_or_load(key: str, loader: Loader) -> Items:
    """Module-level convenience wrapper over the process-wide cache."""
    return await get_config_cache().get_or_load(key, loader)


def invalidate(key: str) -> None:
    """Module-level convenience wrapper over the process-wide cache."""
    get_config_cache().invalidate(key)


def invalidate_oauth_providers() -> None:
    """Drop both provider entries.

    Always both: ``enabled`` is part of the GSI partition key backing the
    enabled-only read, so a write that flips it changes what BOTH the query and
    the scan return.
    """
    cache = get_config_cache()
    cache.invalidate(OAUTH_PROVIDERS_ENABLED)
    cache.invalidate(OAUTH_PROVIDERS_ALL)
