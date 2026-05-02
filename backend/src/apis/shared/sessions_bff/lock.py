"""Per-session asyncio lock registry — refresh-token storm coalescing.

Without this, N tabs that boot up at the same moment trigger N parallel
refresh-token exchanges with Cognito. Cognito rotation invalidates the
previous refresh token on each exchange, so N-1 of those tabs lose their
session.

Holding a per-session lock around the refresh path serializes the exchanges
within a single app-api task. Across tasks (multi-replica deployments) the
storm is still possible — full mitigation moves to Phase 7 (a DDB conditional
write or a short-lived "refresh in flight" marker). For Phase 2 the in-process
lock matches the storm shape we've actually seen during dev (multi-tab on one
laptop hitting one task) and is the lightweight solution called out in the
project memory.
"""

from __future__ import annotations

import asyncio
from threading import Lock as _ThreadLock
from typing import Dict
from weakref import WeakValueDictionary

# We use a WeakValueDictionary so locks don't leak indefinitely as session
# ids churn. A lock is collected once nothing holds a reference, which is
# correct because a held lock keeps itself alive via the awaiter's frame.
_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()
_registry_guard = _ThreadLock()


def get_session_lock(session_id: str) -> asyncio.Lock:
    """Return the per-session lock, creating it on first request.

    Safe to call concurrently — the registry insert is guarded by a thread
    lock so two coroutines on different threads can't race-create different
    `asyncio.Lock` objects for the same session id.
    """
    existing = _locks.get(session_id)
    if existing is not None:
        return existing
    with _registry_guard:
        existing = _locks.get(session_id)
        if existing is not None:
            return existing
        new_lock = asyncio.Lock()
        _locks[session_id] = new_lock
        return new_lock


def _reset_for_tests() -> None:
    """Test-only escape hatch — drop all tracked locks."""
    with _registry_guard:
        _locks.clear()
