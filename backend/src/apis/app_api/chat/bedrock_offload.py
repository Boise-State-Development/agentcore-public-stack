"""Run api-converse's blocking boto3 Bedrock calls off the event loop.

app-api is a single uvicorn process. ``boto3``'s ``converse`` and
``converse_stream`` are synchronous: called from an ``async def`` handler
they freeze the event loop for the life of the model call (28s at the
median for the batch jobs that hit this surface), so every other route on
the task — ``/auth/session``, ``/sessions``, and ``/health`` — times out at
the ALB. The ALB then marks the target unhealthy, ECS replaces it, and the
caller's next calls land on fewer tasks. Two such bursts in 2026-10 cycled
the whole service.

Three pieces keep the loop free and bound the blast radius:

* ``InFlightCap`` — a per-process ceiling on concurrent Bedrock calls.
  ``acquire`` never waits: when the cap is full the route answers 429 with
  ``Retry-After`` at once, because a queued call would hold the client's
  connection against the ALB's 60s idle timeout for nothing. The 60/min
  per-key rate limiter measures *rate*; the bursts that took the tasks down
  stayed under it. What exhausted the task was *concurrency*, which is what
  this cap measures.
* A dedicated ``ThreadPoolExecutor`` sized to the cap. The loop's default
  executor is ``min(32, cpu_count + 4)`` threads — five or six on a small
  Fargate task — and it is shared with every other ``to_thread`` user in the
  process (session metadata writes, tool discovery). A streaming call holds
  its thread for the whole stream, so a dozen of them would otherwise stall
  unrelated work behind them. The cap gates entry, so this pool never queues.
* ``iterate_stream_in_thread`` — the call *and* the iteration run in one
  worker thread, handing events back over an ``asyncio.Queue`` with
  ``call_soon_threadsafe``. Each ``next()`` on a botocore ``EventStream`` is
  a blocking network read between chunks, so moving only the initial call
  off the loop would leave the loop frozen between every token. This is the
  bridge Strands' own ``BedrockModel.stream`` uses.

This is not on the SPA's time-to-first-token path: that is inference-api's
``/invocations``, a different container. On this surface the thread handoff
is tens of microseconds against a call measured in seconds, and during a
burst it *lowers* latency for every other app-api route.
"""

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT_ENV = "API_CONVERSE_MAX_IN_FLIGHT"
DEFAULT_MAX_IN_FLIGHT = 16

# What the 429 tells the caller to wait before retrying. One short model
# call; a slot usually frees well inside this.
RETRY_AFTER_SECONDS = "5"


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            return max(minimum, int(raw))
        except ValueError:
            logger.warning("invalid %s=%r; using default %d", name, raw, default)
    return default


def max_in_flight() -> int:
    """The per-process ceiling on concurrent Bedrock calls (minimum 1)."""
    return _env_int(MAX_IN_FLIGHT_ENV, DEFAULT_MAX_IN_FLIGHT, minimum=1)


class CapacityExceeded(Exception):
    """Raised by ``InFlightCap.acquire`` when every slot is taken."""


class InFlightCap:
    """A non-blocking counter of in-flight Bedrock calls.

    Deliberately not an ``asyncio.Semaphore``: the policy is reject-when-full,
    never wait, so there is nothing to await on — and a plain counter has no
    event-loop affinity, which matters for a module-level singleton that the
    test client drives from a fresh loop per request.
    """

    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def acquire(self) -> Callable[[], None]:
        """Take a slot, or raise ``CapacityExceeded``.

        Returns an idempotent release callable: the streaming path releases
        from the generator's ``finally``, which can run more than once across
        the generator's own close and the response's teardown.
        """
        with self._lock:
            if self._in_flight >= self.limit:
                raise CapacityExceeded(
                    f"{self._in_flight} Bedrock calls in flight; limit {self.limit}"
                )
            self._in_flight += 1

        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            with self._lock:
                self._in_flight -= 1

        return release


_cap: Optional[InFlightCap] = None
_executor: Optional[ThreadPoolExecutor] = None
# Re-entrant: ``get_executor`` sizes itself from ``get_in_flight_cap`` while
# holding it.
_init_lock = threading.RLock()


def get_in_flight_cap() -> InFlightCap:
    global _cap
    if _cap is None:
        with _init_lock:
            if _cap is None:
                _cap = InFlightCap(max_in_flight())
    return _cap


def get_executor() -> ThreadPoolExecutor:
    """The pool Bedrock calls run on: one thread per slot the cap can grant."""
    global _executor
    if _executor is None:
        with _init_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=get_in_flight_cap().limit,
                    thread_name_prefix="api-converse-bedrock",
                )
    return _executor


def reset_for_tests() -> None:
    """Drop the process singletons so a test can re-size the cap via env."""
    global _cap, _executor
    with _init_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _cap = None
        _executor = None


async def run_in_thread(fn: Callable[[], Any]) -> Any:
    """Run one blocking call on the Bedrock pool and await its result."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_executor(), fn)


class _StreamFailed:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException):
        self.exc = exc


_STREAM_DONE = object()


async def iterate_stream_in_thread(
    open_stream: Callable[[], Iterable[Any]],
) -> AsyncIterator[Any]:
    """Open and drain a blocking iterable on the Bedrock pool, yielding its items.

    ``open_stream`` runs in the worker thread — so the ``converse_stream``
    call itself, which blocks until Bedrock's response headers arrive, never
    touches the loop either. An exception from either the open or the
    iteration is re-raised here, at the point the consumer reads past the
    last good item.

    If the consumer stops early (client disconnected, generator closed) the
    worker is told to abandon the stream: it stops at the next chunk boundary
    and closes the underlying botocore ``EventStream`` when it has one, so the
    thread and its slot are not held to the end of a response nobody reads.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    abandoned = threading.Event()

    def _post(item: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            # The loop is gone (process shutdown mid-stream). Nothing to
            # hand the item to.
            pass

    def _worker() -> None:
        stream = None
        try:
            stream = open_stream()
            for item in stream:
                if abandoned.is_set():
                    break
                _post(item)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the loop side
            _post(_StreamFailed(exc))
            return
        finally:
            if abandoned.is_set():
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        logger.debug("closing abandoned Bedrock stream failed", exc_info=True)
        _post(_STREAM_DONE)

    # The worker swallows everything and reports through the queue, so the
    # future is never awaited: it stops at its next chunk once abandoned.
    loop.run_in_executor(get_executor(), _worker)
    try:
        while True:
            item = await queue.get()
            if item is _STREAM_DONE:
                break
            if isinstance(item, _StreamFailed):
                raise item.exc
            yield item
    finally:
        abandoned.set()
