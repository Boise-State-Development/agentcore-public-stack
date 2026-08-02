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
        clock["t"] += 3_600.0
        status, later = tracker.snapshot()

        assert status == STATUS_HEALTHY
        assert later == first, "idle timestamp must not advance"
        # This is what the reaper computes; it must exceed the 900s timeout.
        assert clock["t"] - later == pytest.approx(3_600.0)

    def test_process_start_is_the_idle_origin(self, monkeypatch):
        """A microVM that boots and never serves a turn must still be reaped."""
        clock = {"t": 500.0}
        monkeypatch.setattr(
            "apis.inference_api.runtime_health.time.time", lambda: clock["t"]
        )
        tracker = RuntimeActivityTracker()
        clock["t"] += 901.0

        _, stamp = tracker.snapshot()
        assert clock["t"] - stamp > 900


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

        clock["t"] += 3_000.0
        _, later = tracker.snapshot()
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
        clock["t"] += 13.0  # a representative turn
        tracker.exit()

        clock["t"] += self.IDLE_TIMEOUT - 1
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
