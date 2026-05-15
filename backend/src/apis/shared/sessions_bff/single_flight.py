"""Per-session single-flight primitive — session-resolve path coalescing.

`get_session_lock` in `lock.py` only serializes the Cognito refresh exchange.
It does NOT coalesce the upstream unseal -> `SessionCache.get` ->
`SessionRepository.get` -> `needs_refresh` sequence. When Angular's ~8-endpoint
page-load fan-out hits a cold cache window, each coroutine independently
observes the miss and each runs its own blocking `get_item`, producing ~N
DynamoDB round-trips per cache window per session.

The primitive in this module addresses that gap with a per-session
`asyncio.Future`: the first caller (the "leader") registers a Future under the
session id, runs the loader, and stores the result/exception on the Future.
Concurrent callers that arrive while the leader is still running (the
"followers") find the existing Future and simply `await` it, sharing the
leader's single DynamoDB call.

This is a separate primitive from `get_session_lock`. The existing lock scope
around the Cognito exchange is preserved end-to-end — this single-flight sits
upstream of it.
"""

from __future__ import annotations

import asyncio
from threading import Lock as _ThreadLock
from typing import Awaitable, Callable, Dict, Optional, Tuple

from apis.shared.sessions_bff.models import SessionRecord

# Module-level registry of in-flight resolves keyed by `session_id`.
# Unlike `lock.py`, we use a plain `dict` rather than a `WeakValueDictionary`
# because an `asyncio.Future` that is only referenced by its awaiters would
# otherwise be collected if every awaiter was garbage-collected before
# resolution — the leader is responsible for removing its entry in a
# `finally` block, which keeps lifetime management explicit.
_inflight: Dict[str, "asyncio.Future[Tuple[Optional[SessionRecord], bool]]"] = {}
_registry_guard = _ThreadLock()


async def resolve_once(
    session_id: str,
    loader_coro_factory: Callable[
        [], Awaitable[Tuple[Optional[SessionRecord], bool]]
    ],
) -> Tuple[Optional[SessionRecord], bool]:
    """Run `loader_coro_factory()` at most once per concurrent `session_id`.

    Leader semantics: the first caller for a given `session_id` creates a new
    `asyncio.Future`, registers it under the thread-lock-guarded registry,
    runs the loader, sets the result or exception on the Future, removes the
    entry from the registry, and returns the value.

    Follower semantics: any caller that finds an existing Future `await`s it
    and returns its value, sharing the leader's single loader invocation.

    Exception propagation: an exception raised by the loader is set on the
    Future so it propagates to the leader AND to every follower currently
    awaiting. The registry entry is always removed before the leader returns
    (success or failure), so any subsequent call after the failure starts a
    fresh leader.

    Isolation: distinct `session_id`s do not share a Future — the registry is
    keyed by `session_id`.
    """
    # Fast path: look for an existing Future without holding the thread lock.
    existing = _inflight.get(session_id)
    if existing is not None:
        return await existing

    # Slow path: register a new Future under the thread lock, double-checking
    # so two coroutines on different threads can't race-create two Futures.
    loop = asyncio.get_event_loop()
    with _registry_guard:
        existing = _inflight.get(session_id)
        if existing is not None:
            # Another caller won the race — fall through to follower path.
            future = existing
            is_leader = False
        else:
            future = loop.create_future()
            _inflight[session_id] = future
            is_leader = True

    if not is_leader:
        return await future

    # Leader path — run the loader, set the result/exception, and clean up.
    try:
        result = await loader_coro_factory()
    except BaseException as exc:  # noqa: BLE001 — we must propagate everything
        if not future.done():
            future.set_exception(exc)
            # Mark the exception as retrieved on the leader's side. Followers
            # still observe it when they `await` the Future; this only
            # silences the "Future exception was never retrieved" warning
            # emitted when no follower ever attached.
            future.exception()
        with _registry_guard:
            # Only clear our own entry — another leader may have taken over
            # after we set the exception, though in practice that's only
            # possible if every follower has already consumed the Future.
            if _inflight.get(session_id) is future:
                del _inflight[session_id]
        raise
    else:
        if not future.done():
            future.set_result(result)
        with _registry_guard:
            if _inflight.get(session_id) is future:
                del _inflight[session_id]
        return result


def _reset_for_tests() -> None:
    """Test-only escape hatch — drop all tracked in-flight Futures."""
    with _registry_guard:
        _inflight.clear()
