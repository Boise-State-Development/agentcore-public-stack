"""Event-loop lag watchdog for app-api.

app-api runs one uvicorn worker, so anything synchronous on the event loop
stalls every request on the task at once, including the ALB's ``/health``
probe. On 2026-10-30 an agent deletion ran a full S3 Vectors index scan per
document on the loop for about three and a half minutes; the ALB marked the
task unhealthy and ECS replaced it, and the only evidence afterwards was the
gap in the access log. Nothing measured the loop itself.

This module does. A background task asks the loop to wake it after
``SAMPLE_INTERVAL_SECONDS`` and measures how late the wake-up is. A healthy
loop is late by a few milliseconds; a loop blocked by synchronous work is late
by however long the work took. The worst lag seen is emitted as the
``EventLoopLagMs`` EMF metric at most once per ``EMIT_INTERVAL_SECONDS``, and a
single sample over ``WARN_THRESHOLD_MS`` logs a WARNING with the measured lag,
which is the log line to read next to a stopped task's timeline.

Zero cost on the turn path: nothing here runs per request. The task wakes once
a second, does arithmetic, and writes one log line every ten seconds. A blocked
loop cannot run this task either, which is the point: the lag it measures is
exactly the time the loop was unavailable to everyone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Same namespace app-api's other latency telemetry uses (telemetry/routes.py,
# telemetry/ttft_probe.py), so the alarm construct has one namespace to read.
EVENT_LOOP_LAG_NAMESPACE = "AgentCoreStack/TurnLatency"
EVENT_LOOP_LAG_METRIC = "EventLoopLagMs"

SAMPLE_INTERVAL_SECONDS = 1.0
EMIT_INTERVAL_SECONDS = 10.0
WARN_THRESHOLD_MS = 1000.0

EmitFn = Callable[[str, Dict[str, float], Optional[dict], Optional[dict]], None]


def _default_emit(
    namespace: str,
    metrics: Dict[str, float],
    properties: Optional[dict],
    units: Optional[dict],
) -> None:
    from apis.shared.observability.emf import emit_emf_metrics

    emit_emf_metrics(namespace, metrics, properties=properties, units=units)


class EventLoopLagMonitor:
    """Measures ``asyncio.sleep`` drift and reports the worst lag per window.

    ``clock`` and ``sleep`` are injectable so the measurement can be tested
    against a fake clock without waiting on wall time.
    """

    def __init__(
        self,
        *,
        sample_interval: float = SAMPLE_INTERVAL_SECONDS,
        emit_interval: float = EMIT_INTERVAL_SECONDS,
        warn_threshold_ms: float = WARN_THRESHOLD_MS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        emit: EmitFn = _default_emit,
    ) -> None:
        self._sample_interval = sample_interval
        self._emit_interval = emit_interval
        self._warn_threshold_ms = warn_threshold_ms
        self._clock = clock
        self._sleep = sleep
        self._emit = emit
        self._window_max_ms = 0.0
        self._window_samples = 0
        self._window_started_at: Optional[float] = None

    async def sample(self) -> float:
        """Sleep one interval and return how late the wake-up was, in ms.

        The lag is folded into the current window; the window is emitted and
        reset once it has been open for ``emit_interval``.
        """
        started = self._clock()
        if self._window_started_at is None:
            self._window_started_at = started
        await self._sleep(self._sample_interval)
        woke = self._clock()
        lag_ms = max(0.0, (woke - started - self._sample_interval) * 1000.0)

        self._window_samples += 1
        if lag_ms > self._window_max_ms:
            self._window_max_ms = lag_ms
        if lag_ms > self._warn_threshold_ms:
            logger.warning(
                "Event loop blocked for %.0f ms (a %.0f ms sleep woke %.0f ms late); "
                "every request on this task, including /health, waited that long",
                lag_ms,
                self._sample_interval * 1000.0,
                lag_ms,
            )

        if woke - self._window_started_at >= self._emit_interval:
            self._flush(woke)
        return lag_ms

    def _flush(self, now: float) -> None:
        self._emit(
            EVENT_LOOP_LAG_NAMESPACE,
            {EVENT_LOOP_LAG_METRIC: round(self._window_max_ms, 1)},
            {"samples": self._window_samples, "service": "app-api"},
            {EVENT_LOOP_LAG_METRIC: "Milliseconds"},
        )
        self._window_max_ms = 0.0
        self._window_samples = 0
        self._window_started_at = now

    async def run(self) -> None:
        """Sample forever. Cancel the task to stop; never raises otherwise."""
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a watchdog must outlive its own bugs
                logger.exception("Event loop lag sample failed; continuing")
                await self._sleep(self._sample_interval)


async def run_event_loop_lag_monitor() -> None:
    """Entry point for the app-api lifespan task."""
    await EventLoopLagMonitor().run()
