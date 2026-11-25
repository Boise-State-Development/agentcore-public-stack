"""Drop ended sessions from this process's cache within one poll.

`SessionCache` serves a resolved session for up to its TTL
(`refresh_leeway_seconds`, 60 s) without reading DynamoDB. Logout deletes the
row and drops the entry on the task that served it, but every other app-api
task kept serving its cached copy until the TTL, and the access token inside
still passes signature validation. Measured on dev, which runs more than one task: a signed-out
session answered 200 for about 21 s after logout.

`SessionRepository.revoke` writes one item to a revocation feed in the sessions
table. Each app-api process runs `run_revocation_watcher` from its lifespan,
which reads the feed every `POLL_INTERVAL_SECONDS` and invalidates whatever it
names. The window falls from one cache TTL to one poll.

Off the request path by construction: nothing here runs per request, so it adds
no latency to any route, `POST /chat/stream` included. The cost is one Query
per process per poll.

Each poll re-reads a fixed lookback rather than advancing a cursor, so a writer
whose clock is behind the reader's is still seen, and a cache entry re-seeded
from a read that raced the logout is dropped again on the next poll.
Invalidating twice is harmless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .cache import SessionCache, get_default_cache
from .config import BFFConfig
from .repository import SessionRepository

logger = logging.getLogger(__name__)

#: How stale another task's cached copy of an ended session can be, at most.
POLL_INTERVAL_SECONDS = 5.0
#: Slack on top of the cache TTL and the poll interval for clock skew between
#: the task that wrote a revocation and the one reading it.
_CLOCK_SKEW_MARGIN_SECONDS = 30.0


def lookback_seconds(config: BFFConfig, interval: float = POLL_INTERVAL_SECONDS) -> float:
    """Long enough to cover any cache entry that could still be live."""
    return config.refresh_leeway_seconds + interval + _CLOCK_SKEW_MARGIN_SECONDS


async def sweep_once(
    repository: SessionRepository, cache: SessionCache, *, lookback: float
) -> int:
    """Invalidate every session revoked within `lookback`. Returns the count."""
    revoked = await repository.recent_revocations(since=time.time() - lookback)
    for session_id in revoked:
        cache.invalidate(session_id)
    return len(revoked)


async def run_revocation_watcher(
    *,
    config: Optional[BFFConfig] = None,
    repository: Optional[SessionRepository] = None,
    cache: Optional[SessionCache] = None,
    interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Poll the revocation feed until cancelled. A no-op without the BFF."""
    config = config or BFFConfig.from_env()
    if not config.is_enabled():
        return
    repository = repository or SessionRepository(table_name=config.sessions_table_name)
    cache = cache or get_default_cache()
    lookback = lookback_seconds(config, interval)
    failing = False
    while True:
        try:
            await sweep_once(repository, cache, lookback=lookback)
            if failing:
                logger.info("Session revocation feed readable again")
            failing = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Log the first failure of a run, not every poll. While the feed
            # is unreadable the cache TTL is the bound again, as it was before.
            if not failing:
                logger.warning("Session revocation feed read failed: %s", exc)
            failing = True
        await asyncio.sleep(interval)
