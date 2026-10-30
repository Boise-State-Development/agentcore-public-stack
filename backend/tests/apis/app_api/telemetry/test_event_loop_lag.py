"""Event-loop lag watchdog: the drift measurement against a fake clock."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import pytest

from apis.app_api.telemetry import event_loop_lag as mod
from apis.app_api.telemetry.event_loop_lag import EventLoopLagMonitor


class FakeLoop:
    """A clock and a sleep that agree with each other.

    ``sleep(d)`` advances the clock by ``d`` plus the next scripted ``lag``
    (seconds), standing in for synchronous work that held the loop past the
    requested wake-up. An empty script means a perfectly punctual loop.
    """

    def __init__(self, lags: Optional[List[float]] = None) -> None:
        self.now = 1000.0
        self.lags = list(lags or [])

    def clock(self) -> float:
        return self.now

    async def sleep(self, duration: float) -> None:
        lag = self.lags.pop(0) if self.lags else 0.0
        self.now += duration + lag


Emitted = Tuple[str, Dict[str, float], Optional[dict], Optional[dict]]


def _monitor(loop: FakeLoop, emitted: List[Emitted], **overrides) -> EventLoopLagMonitor:
    return EventLoopLagMonitor(
        clock=loop.clock,
        sleep=loop.sleep,
        emit=lambda ns, m, p, u: emitted.append((ns, m, p, u)),
        **overrides,
    )


def test_punctual_loop_measures_zero_lag() -> None:
    loop = FakeLoop()
    emitted: List[Emitted] = []
    monitor = _monitor(loop, emitted)

    assert asyncio.run(monitor.sample()) == 0.0
    assert emitted == []


def test_lag_is_the_time_past_the_requested_wake_up_in_ms() -> None:
    loop = FakeLoop(lags=[0.25])
    monitor = _monitor(loop, [])

    assert asyncio.run(monitor.sample()) == pytest.approx(250.0)


def test_emits_the_window_maximum_at_most_once_per_emit_interval() -> None:
    # The window is 10 s of wall time, not ten samples: a lagged sample eats
    # into it. 1 s samples with 1.8 s of lag spread over them pass the 10 s
    # mark on the ninth wake-up (9 s slept + 1.8 s late = 10.8 s).
    loop = FakeLoop(lags=[0.0, 0.3, 0.0, 1.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.4])
    emitted: List[Emitted] = []
    monitor = _monitor(loop, emitted)

    async def run_samples(n: int) -> None:
        for _ in range(n):
            await monitor.sample()

    asyncio.run(run_samples(8))
    assert emitted == [], "nothing is emitted before the window is 10 s old (9.8 s so far)"

    asyncio.run(run_samples(1))
    assert len(emitted) == 1
    namespace, metrics, properties, units = emitted[0]
    assert namespace == mod.EVENT_LOOP_LAG_NAMESPACE == "AgentCoreStack/TurnLatency"
    assert metrics == {"EventLoopLagMs": pytest.approx(1500.0)}
    assert properties == {"samples": 9, "service": "app-api"}
    assert units == {"EventLoopLagMs": "Milliseconds"}

    # The next window starts clean: two more samples (one of them 400 ms late)
    # are 2.4 s into it, so nothing new is emitted and the 1,500 ms maximum
    # does not carry over.
    asyncio.run(run_samples(2))
    assert len(emitted) == 1


def test_window_resets_after_a_flush() -> None:
    # A 2 s stall on the first sample closes the first window after eight
    # wake-ups (8 s slept + 2 s late). The second window then runs from t=10
    # and closes on the tenth wake-up after that (10 s slept + 0.8 s late).
    loop = FakeLoop(lags=[2.0] + [0.0] * 9 + [0.1] * 10)
    emitted: List[Emitted] = []
    monitor = _monitor(loop, emitted)

    async def run_samples(n: int) -> None:
        for _ in range(n):
            await monitor.sample()

    asyncio.run(run_samples(20))
    assert [m["EventLoopLagMs"] for _, m, _, _ in emitted] == [pytest.approx(2000.0), pytest.approx(100.0)]
    assert [p["samples"] for _, _, p, _ in emitted] == [8, 10]


def test_a_single_sample_over_one_second_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    loop = FakeLoop(lags=[0.9, 1.2])
    monitor = _monitor(loop, [])

    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        asyncio.run(monitor.sample())
        assert caplog.records == [], "900 ms is under the threshold"
        asyncio.run(monitor.sample())

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "1200 ms" in caplog.records[0].getMessage()


def test_defaults_match_the_alarm_contract() -> None:
    # The CDK alarm reads Maximum over 1-minute periods with a 2,000 ms
    # threshold; these are what make that alarm mean what it says.
    assert mod.SAMPLE_INTERVAL_SECONDS == 1.0
    assert mod.EMIT_INTERVAL_SECONDS == 10.0
    assert mod.WARN_THRESHOLD_MS == 1000.0
    assert mod.EVENT_LOOP_LAG_METRIC == "EventLoopLagMs"


def test_run_survives_a_failing_sample() -> None:
    calls = {"n": 0}

    async def flaky_sleep(_: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        if calls["n"] >= 3:
            raise asyncio.CancelledError()

    monitor = EventLoopLagMonitor(sleep=flaky_sleep, emit=lambda *a: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.run())
    assert calls["n"] == 3, "the failed sample was logged and sampling continued"
