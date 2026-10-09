"""Tests for the AgentCore `/ping` liveness contract.

The invariant under test is the one that decides the platform's largest cost
line: `time_of_last_update` must stay *frozen* while the container is idle so
AgentCore's reaper can measure real idle time, and must keep moving while a
turn is actually streaming so a long turn is never reaped mid-generation.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from apis.inference_api.runtime_health import (
    RESTORE_GAP_SECONDS,
    STATUS_HEALTHY,
    STATUS_HEALTHY_BUSY,
    InvocationActivityMiddleware,
    RuntimeActivityTracker,
    ping_payload,
)


class TestIdleReporting:
    def test_idle_container_reports_healthy(self):
        tracker = RuntimeActivityTracker()
        status, _ = tracker.snapshot()
        assert status == STATUS_HEALTHY

    def test_timestamp_is_frozen_while_idle(self, monkeypatch):
        """The bug that made every microVM immortal: a moving idle timestamp.

        AgentCore measures idleness as `now - time_of_last_update`. If the
        timestamp tracks `now`, measured idle time never grows and the 900s
        reaper never fires.
        """
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()

        _, first = tracker.snapshot()
        later = _poll_for(tracker, clock, 3_600.0)
        status = tracker.snapshot()[0]

        assert status == STATUS_HEALTHY
        assert later == first, "idle timestamp must not advance"
        # This is what the reaper computes; it must exceed the 900s timeout.
        assert clock["t"] - later == pytest.approx(3_600.0)

    def test_process_start_is_the_idle_origin(self, monkeypatch):
        """A microVM that boots and never serves a turn must still be reaped.

        Polled every 2s as the platform does: a single poll after a long
        silence is what a snapshot restore looks like (see below).
        """
        clock = {"t": 500.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        stamp = _poll_for(tracker, clock, 901.0)
        assert clock["t"] - stamp > 900


def _poll_for(tracker: RuntimeActivityTracker, clock: dict, seconds: float, every: float = 2.0) -> int:
    """Advance the clock, polling every `every` seconds; return the last stamp."""
    stamp = tracker.snapshot()[1]
    elapsed = 0.0
    while elapsed < seconds:
        clock["t"] += every
        elapsed += every
        stamp = tracker.snapshot()[1]
    return stamp


class TestSnapshotRestore:
    """AgentCore Runtime V2 restores a snapshot of a booted container.

    The idle origin in that snapshot is restored into every session, however
    old the snapshot is. The first poll after the restore must not report a
    microVM that has served nothing as already past the idle timeout.
    """

    def test_restore_after_polling_restarts_the_idle_clock(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        _poll_for(tracker, clock, 30.0)  # polled, then snapshotted

        clock["t"] += 7_200.0  # restored two hours later
        status, stamp = tracker.snapshot()

        assert status == STATUS_HEALTHY
        assert stamp == clock["t"], "first poll after a restore must not look idle"

    def test_restore_before_the_first_poll_restarts_the_idle_clock(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()  # snapshotted straight after import

        clock["t"] += 7_200.0
        _, stamp = tracker.snapshot()
        assert stamp == clock["t"]

    def test_restored_microvm_is_still_reaped_when_idle(self, monkeypatch):
        """Restarting the clock must not make the restored microVM immortal."""
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        clock["t"] += 7_200.0
        restored_at = tracker.snapshot()[1]

        stamp = _poll_for(tracker, clock, 901.0)
        assert stamp == restored_at, "idle timestamp must freeze after the restart"
        assert clock["t"] - stamp > 900

    def test_restart_happens_at_most_once(self, monkeypatch):
        """Irregular polling on V1 must cost at most one extra idle period."""
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        clock["t"] += 7_200.0
        first = tracker.snapshot()[1]

        clock["t"] += 7_200.0
        _, stamp = tracker.snapshot()
        assert stamp == first
        assert clock["t"] - stamp > 900

    def test_ordinary_poll_jitter_is_not_a_restore(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        _, origin = tracker.snapshot()

        stamp = _poll_for(tracker, clock, 1_200.0, every=RESTORE_GAP_SECONDS)
        assert stamp == origin, "a gap of exactly the threshold must not restart the clock"

    def test_busy_reporting_is_unchanged_by_a_restore(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        clock["t"] += 7_200.0
        tracker.enter()

        status, stamp = tracker.snapshot()
        assert status == STATUS_HEALTHY_BUSY
        assert stamp == clock["t"]


class TestBusyReporting:
    def test_in_flight_turn_reports_healthy_busy(self):
        tracker = RuntimeActivityTracker()
        tracker.enter()
        status, _ = tracker.snapshot()
        assert status == STATUS_HEALTHY_BUSY

    def test_timestamp_refreshes_while_busy(self, monkeypatch):
        """PR #338's protection: a long turn must never be reaped mid-stream.

        A turn can run well past `idleRuntimeSessionTimeout`. Freezing the
        timestamp at turn start would leave it exposed to the reap that
        bedrock-agentcore-sdk-python#471 describes.
        """
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        tracker.enter()

        tracker.snapshot()
        clock["t"] += 1_800.0
        status, stamp = tracker.snapshot()

        assert status == STATUS_HEALTHY_BUSY
        assert clock["t"] - stamp == 0, "busy timestamp must track now"

    def test_idle_clock_restarts_when_the_turn_finishes(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()

        tracker.enter()
        clock["t"] += 120.0
        tracker.snapshot()
        tracker.exit()

        clock["t"] += 10.0
        status, stamp = tracker.snapshot()
        assert status == STATUS_HEALTHY
        # Idle is measured from when the turn ended, not from process start.
        assert clock["t"] - stamp == pytest.approx(10.0)

        later = _poll_for(tracker, clock, 3_000.0)
        assert later == stamp, "timestamp must freeze again once idle"

    def test_concurrent_turns_stay_busy_until_the_last_one_ends(self):
        tracker = RuntimeActivityTracker()
        tracker.enter()
        tracker.enter()
        tracker.exit()
        assert tracker.snapshot()[0] == STATUS_HEALTHY_BUSY
        tracker.exit()
        assert tracker.snapshot()[0] == STATUS_HEALTHY

    def test_unbalanced_exit_cannot_wedge_the_counter_negative(self):
        """A negative counter would make the next turn's exit leave it busy."""
        tracker = RuntimeActivityTracker()
        tracker.exit()
        assert tracker.in_flight == 0

        tracker.enter()
        tracker.exit()
        assert tracker.snapshot()[0] == STATUS_HEALTHY


class TestPingPayload:
    def test_payload_shape(self):
        payload = ping_payload()
        assert payload["status"] in (STATUS_HEALTHY, STATUS_HEALTHY_BUSY)
        assert isinstance(payload["time_of_last_update"], int)
        assert "version" in payload


def _build_app(tracker_probe: list) -> Starlette:
    async def invocations(request):
        async def body():
            # Observed from inside the streamed body — the window that
            # `BaseHTTPMiddleware` would have already exited.
            tracker_probe.append(request.app.state.tracker.snapshot()[0])
            yield b"data: chunk\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    async def ping(request):
        tracker_probe.append(request.app.state.tracker.snapshot()[0])
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True})

    async def boom(request):
        raise RuntimeError("handler exploded")

    async def voice(websocket):
        await websocket.accept()
        tracker_probe.append(websocket.app.state.tracker.snapshot()[0])
        await websocket.close()

    app = Starlette(
        routes=[
            Route("/invocations", invocations, methods=["POST"]),
            Route("/ping", ping),
            Route("/boom", boom, methods=["POST"]),
            WebSocketRoute("/voice/stream", voice),
        ]
    )
    app.add_middleware(InvocationActivityMiddleware)
    return app


class TestInvocationActivityMiddleware:
    @pytest.fixture(autouse=True)
    def _isolated_tracker(self, monkeypatch):
        """Swap the module singleton so tests don't share counter state."""
        tracker = RuntimeActivityTracker()
        monkeypatch.setattr("apis.inference_api.runtime_health.tracker", tracker)
        self.tracker = tracker

    def _client(self):
        probe = []
        app = _build_app(probe)
        app.state.tracker = self.tracker
        return TestClient(app), probe

    def test_busy_for_the_duration_of_the_streamed_body(self):
        """The middleware must span the SSE body, not just the handler call."""
        client, probe = self._client()
        with client.stream("POST", "/invocations") as response:
            list(response.iter_bytes())
        assert probe == [STATUS_HEALTHY_BUSY]

    def test_counter_released_after_the_response_completes(self):
        client, _ = self._client()
        with client.stream("POST", "/invocations") as response:
            list(response.iter_bytes())
        assert self.tracker.in_flight == 0
        assert self.tracker.snapshot()[0] == STATUS_HEALTHY

    def test_ping_itself_is_not_activity(self):
        """`/ping` arrives every ~2s forever; counting it would pin busy on."""
        client, probe = self._client()
        client.get("/ping")
        assert probe == [STATUS_HEALTHY]
        assert self.tracker.in_flight == 0

    def test_counter_released_when_the_handler_raises(self):
        client, _ = self._client()
        with pytest.raises(RuntimeError):
            client.post("/boom")
        assert self.tracker.in_flight == 0

    def test_open_websocket_counts_as_busy(self):
        client, probe = self._client()
        with client.websocket_connect("/voice/stream"):
            pass
        assert probe == [STATUS_HEALTHY_BUSY]
        assert self.tracker.in_flight == 0


class TestReaperEndToEnd:
    """The whole point: an idle container becomes reapable, a busy one doesn't."""

    IDLE_TIMEOUT = 900

    def _idle_seconds(self, tracker, now):
        return now - tracker.snapshot()[1]

    def test_container_becomes_reapable_after_a_single_turn(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()

        tracker.enter()
        _poll_for(tracker, clock, 13.0)  # a representative turn
        tracker.exit()

        # Polled every 2s while idle, as the platform does.
        _poll_for(tracker, clock, self.IDLE_TIMEOUT - 2)
        clock["t"] += 1
        assert self._idle_seconds(tracker, clock["t"]) < self.IDLE_TIMEOUT

        clock["t"] += 2
        assert self._idle_seconds(tracker, clock["t"]) > self.IDLE_TIMEOUT

    def test_long_turn_never_becomes_reapable(self, monkeypatch):
        clock = {"t": 1_000.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        tracker.enter()

        for _ in range(40):  # 40 * 2s polls across a 20-minute turn
            clock["t"] += 30.0
            assert self._idle_seconds(tracker, clock["t"]) < self.IDLE_TIMEOUT
            assert tracker.snapshot()[0] == STATUS_HEALTHY_BUSY
