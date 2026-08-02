"""AgentCore Runtime liveness reporting for `GET /ping`.

AgentCore's idle reaper decides whether a microVM is idle from the
``time_of_last_update`` field this container returns on ``/ping`` — it
measures idleness as ``now - time_of_last_update`` and terminates the
microVM once that exceeds ``idleRuntimeSessionTimeout`` (900s by default;
see the Runtime's ``lifecycleConfiguration``). The platform polls ``/ping``
roughly every two seconds.

That makes the field a contract, not a heartbeat. Returning ``time.time()``
on every poll pins the measured idle time near zero forever, so the reaper
can never fire and every microVM instead runs to ``maxLifetime`` — 8 hours
— no matter how long ago the last turn finished. We shipped exactly that in
PR #338 and it made AgentCore Runtime memory the single largest line on the
platform bill: prod microVMs averaged 493 minutes of life against ~1.2%
CPU utilization, and on a representative day 88 of 89 microVMs served one
turn apiece and then sat idle for eight hours.

The upstream SDK's contract (``bedrock_agentcore.runtime.app``:
``get_current_ping_status``) is that ``time_of_last_update`` is *sticky* —
it advances only when the reported *status* changes, so a container that
stays ``Healthy`` lets idle time accrue and be reaped on schedule. In-flight
work is signalled by reporting ``HealthyBusy`` instead, not by moving the
timestamp. This module implements that contract, with two deliberate
deviations noted below.

Behaviour:

* Idle container → ``Healthy``, timestamp frozen at the moment the last turn
  finished (or at process start, if none ever did). Idle time accrues and
  the reaper fires on schedule.
* Turn in flight → ``HealthyBusy``, **and the timestamp refreshes on every
  poll** (deviation 1). It guarantees a long agentic turn is never reaped
  mid-stream even if the platform ignores the status field and reads only
  the timestamp — which is the failure mode PR #338 was fixing
  (bedrock-agentcore-sdk-python#471). Freezing the timestamp at turn start,
  as the SDK does, would leave a turn running past
  ``idleRuntimeSessionTimeout`` exposed to that reap.
* Turn finishes → back to ``Healthy``, timestamp stamped at completion, and
  the idle clock starts from there.

Deviation 2 is that transitions are stamped by ``enter()``/``exit()`` rather
than sampled when a poll happens to observe them. The SDK infers the
transition inside its ping handler, so a turn shorter than the ~2s poll
interval is never observed as activity at all and leaves the idle clock
running from process start — which can strand a just-served session one poll
away from a reap.

The net effect is that the reaper is armed exactly when the container is
genuinely idle and disarmed exactly while it is genuinely working.

`InvocationActivityMiddleware` is what marks work in flight. It is pure ASGI
rather than `BaseHTTPMiddleware` because the unit of work is the *streamed
response body*, not the handler call: `BaseHTTPMiddleware` hands back control
as soon as the handler returns a `StreamingResponse`, which for this service
is before the agent has produced a single token. A pure-ASGI middleware's
``await self.app(...)`` does not return until the body is fully sent (or the
client disconnects), so a plain ``try/finally`` around it spans the real turn
and cannot leak the counter on disconnect or error.
"""

from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable, MutableMapping

STATUS_HEALTHY = "Healthy"
STATUS_HEALTHY_BUSY = "HealthyBusy"

# `/ping` is the reaper's own poll and must never count as activity — it is
# the one request that arrives continuously on a completely idle container.
_NON_ACTIVITY_PATHS = frozenset({"/ping"})


class RuntimeActivityTracker:
    """Tracks in-flight agent work and reports the AgentCore ping contract.

    Mutation happens only from the event loop thread between awaits, so the
    counter needs no lock: every increment and decrement is a single
    non-suspending statement.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self._status = STATUS_HEALTHY
        # Process start is the correct origin: a microVM that boots and never
        # receives a turn should be reaped `idleRuntimeSessionTimeout` after
        # it came up, not held open indefinitely.
        self._status_since = time.time()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def enter(self) -> None:
        self._in_flight += 1
        self._status = STATUS_HEALTHY_BUSY
        self._status_since = time.time()

    def exit(self) -> None:
        # Clamp rather than assert: a miscounted decrement must never wedge
        # the container into a permanent `HealthyBusy`, which would recreate
        # the immortal-microVM bug this module exists to prevent.
        if self._in_flight > 0:
            self._in_flight -= 1
        if self._in_flight == 0:
            self._status = STATUS_HEALTHY
            # The true "idle since" moment. Stamping here rather than letting
            # the next poll infer it matters: a turn shorter than the ~2s poll
            # interval would otherwise never be observed as activity at all,
            # and the idle clock would still be running from process start —
            # so a fast turn could be followed almost immediately by a reap,
            # destroying a warm session the next turn was about to reuse.
            self._status_since = time.time()

    def snapshot(self) -> tuple[str, int]:
        """Return `(status, time_of_last_update)` for a `/ping` response."""
        status = STATUS_HEALTHY_BUSY if self._in_flight else STATUS_HEALTHY
        self._status = status
        # Keep advancing while busy so a long turn cannot be reaped
        # mid-stream. While `Healthy` the timestamp stays put — frozen at the
        # `exit()` above — so idle time accrues and the reaper can fire.
        if status == STATUS_HEALTHY_BUSY:
            self._status_since = time.time()
        return status, int(self._status_since)


# Module-level singleton: one tracker per container, which is the granularity
# AgentCore reaps at (one microVM per runtime session).
tracker = RuntimeActivityTracker()


def ping_payload() -> dict[str, Any]:
    """Build the `GET /ping` body AgentCore Runtime expects."""
    status, time_of_last_update = tracker.snapshot()
    return {
        "status": status,
        "time_of_last_update": time_of_last_update,
        "version": os.environ.get("APP_VERSION", "unknown"),
    }


Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
Scope = MutableMapping[str, Any]


class InvocationActivityMiddleware:
    """Marks the container busy for the full lifetime of any real request.

    Every path except `/ping` counts, so a route added later holds the
    session open while it actually runs without needing to be registered
    here. WebSocket connections (voice mode) count for as long as they stay
    open, which is correct — an open voice session is live work.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in _NON_ACTIVITY_PATHS:
            await self.app(scope, receive, send)
            return

        tracker.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            tracker.exit()
