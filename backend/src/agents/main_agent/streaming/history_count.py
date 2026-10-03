"""This turn's message-index base, counted off the critical path.

Per-message metadata (cost, usage, ``displayText``, the artifact anchor) is
keyed by a message's position in the session's full history — the same
``enumerate`` index ``get_messages_from_cloud`` re-derives on reload. So every
turn needs the number of messages that existed before it, counted across
every agent that writes to the session (an ``@``-mention's second agent,
voice, synthetic messages), which only a read of the history can give: a
count held on a session manager instance goes stale the moment another
instance writes (the #741/#751 rule in CLAUDE.md).

That read is a paginated ``ListEvents`` with payloads, and it used to sit on
the head of every turn, ahead of the model call: ~62ms + 2.25ms per stored
event, ~390ms at 100+ events (docs/specs/turn-path-ttft.md §5 P4). Nothing
needs the number before the user's message is appended, and most consumers
need it only after the model answers. So the read starts at the head of the
turn on a worker thread and each consumer awaits it where it is used.

**The cutoff.** Started concurrently, the read races this turn's own writes —
the user's message is persisted while it runs. Every persisted message carries
the ``created_at`` it was built with (the SDK derives the event timestamp from
it), and this turn's messages are all built after the head of the turn, so
counting only messages created before that instant gives the count the
serial read gave, whether or not this turn's writes have landed yet.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

_POOL: Optional[ThreadPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    """The process-wide pool the count runs on, built on first use."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="history-count")
    return _POOL


def count_created_before(messages: Optional[Iterable[Any]], before: Optional[datetime]) -> int:
    """How many of ``messages`` were created strictly before ``before``.

    ``before=None`` counts them all. A message whose ``created_at`` is missing
    or unparseable is counted: anything already stored when the read ran and
    not stamped by this turn belongs to the history before it.
    """
    if not messages:
        return 0
    if before is None:
        return sum(1 for _ in messages)
    count = 0
    for message in messages:
        created = getattr(message, "created_at", None)
        if not created:
            count += 1
            continue
        try:
            stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            count += 1
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < before:
            count += 1
    return count


class HistoryCount:
    """The number of messages in the session before this turn, resolved lazily.

    Build one per turn with :meth:`start` (or :meth:`resolved` for a count
    that needs no read) and ``await resolve()`` wherever the number is used.
    The first resolve logs ``history count prefetch waitedMs= lookupMs=``;
    ``waitedMs=0`` means the read finished before anyone needed it.
    """

    def __init__(
        self,
        *,
        value: Optional[int] = None,
        future: Optional["Future[int]"] = None,
        fallback: int = 0,
    ) -> None:
        self._value = value
        self._future = future
        self._fallback = fallback
        self._lookup_ms: Optional[float] = None

    @classmethod
    def resolved(cls, value: int) -> "HistoryCount":
        return cls(value=value)

    @classmethod
    def start(
        cls,
        count: Callable[[datetime], int],
        *,
        fallback: int,
        now: Optional[datetime] = None,
    ) -> "HistoryCount":
        """Submit ``count(cutoff)`` to the pool, with the cutoff taken now.

        ``fallback`` is used if the read raises; capture it before this turn
        appends anything (a maintained count read later would include this
        turn's own messages).
        """
        cutoff = now or datetime.now(timezone.utc)
        instance = cls(fallback=fallback)

        def _run() -> int:
            started = time.perf_counter()
            try:
                return count(cutoff)
            finally:
                instance._lookup_ms = (time.perf_counter() - started) * 1000

        try:
            instance._future = _pool().submit(_run)
        except RuntimeError:
            # The pool is shut down (interpreter exit). Count inline.
            try:
                instance._value = count(cutoff)
            except Exception:  # noqa: BLE001 - a count must not break a turn
                instance._value = fallback
        return instance

    @property
    def value(self) -> Optional[int]:
        """The count if already resolved, else None. Never waits."""
        return self._value

    async def resolve(self) -> int:
        if self._value is not None:
            return self._value
        future = self._future
        if future is None:
            self._value = self._fallback
            return self._value
        waited_from = time.perf_counter()
        try:
            value = await asyncio.wrap_future(future)
        except Exception as e:  # noqa: BLE001 - a count must not break a turn
            logger.warning(
                "history count prefetch failed, using maintained count %d: %s: %s",
                self._fallback, type(e).__name__, e,
            )
            value = self._fallback
        if self._value is None:
            self._value = value
            logger.info(
                "history count prefetch waitedMs=%d lookupMs=%d count=%d",
                round((time.perf_counter() - waited_from) * 1000),
                round(self._lookup_ms or 0),
                value,
            )
        return self._value
