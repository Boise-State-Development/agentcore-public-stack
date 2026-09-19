"""Pre-stream turn timing (docs/specs/agent-state-feedback.md).

This exists to answer one question — which stage owns the 3.75s before the
first SSE byte — so what is worth pinning is that it cannot lie about that
(stages in order, deltas not cumulative totals) and cannot break the turn it
measures.
"""

import json
import logging

from apis.inference_api.chat.turn_timing import TurnPrelude


class TestMarks:
    def test_stages_are_recorded_in_order(self):
        prelude = TurnPrelude()
        for stage in ("preamble", "rag", "tools", "agent_build"):
            prelude.mark(stage)

        payload = _emitted(prelude)

        assert list(payload["stages"].keys()) == [
            "preamble",
            "rag",
            "tools",
            "agent_build",
        ]

    def test_each_stage_is_a_delta_not_a_running_total(self, monkeypatch):
        """The slowest stage is the answer, so a cumulative number would point
        at the last stage every time."""
        clock = iter([0.0, 1.0, 1.5, 4.5, 4.6])
        monkeypatch.setattr(
            "apis.inference_api.chat.turn_timing.time.perf_counter",
            lambda: next(clock),
        )

        prelude = TurnPrelude()  # consumes 0.0
        prelude.mark("preamble")  # 1.0 -> 1000ms
        prelude.mark("rag")  # 1.5 -> 500ms
        prelude.mark("agent_build")  # 4.5 -> 3000ms

        payload = _emitted(prelude)  # total_ms consumes 4.6

        assert payload["stages"] == {
            "preamble": 1000,
            "rag": 500,
            "agent_build": 3000,
        }
        assert payload["totalMs"] == 4600

    def test_total_covers_the_whole_prelude(self, monkeypatch):
        clock = iter([0.0, 2.0, 7.0])
        monkeypatch.setattr(
            "apis.inference_api.chat.turn_timing.time.perf_counter",
            lambda: next(clock),
        )

        prelude = TurnPrelude()
        prelude.mark("preamble")

        assert _emitted(prelude)["totalMs"] == 7000


class TestStartedAt:
    """The value handed to the stream coordinator for the turn recap."""

    def test_is_wall_clock_not_perf_counter(self):
        """Mixing clock domains yields a meaningless number, not a close one.

        The coordinator subtracts this from a `time.time()` reading. A
        `perf_counter` value — seconds since an arbitrary origin — would make
        the recap read as decades, or negative.
        """
        import time

        before = time.time()
        prelude = TurnPrelude()
        after = time.time()

        assert before <= prelude.started_at <= after

    def test_marks_are_immune_to_the_wall_clock_moving(self, monkeypatch):
        """The stage deltas must not inherit the wall clock.

        NTP can step `time.time()` backwards mid-turn, which would produce a
        negative stage. The marks use `perf_counter`, so a wall clock that
        jumps a decade must change nothing.
        """
        prelude = TurnPrelude()
        monkeypatch.setattr(
            "apis.inference_api.chat.turn_timing.time.time", lambda: 0.0
        )
        prelude.mark("preamble")

        payload = _emitted(prelude)

        assert payload["stages"]["preamble"] >= 0
        assert payload["totalMs"] >= 0


class TestPayload:
    def test_carries_the_session_and_the_caller_s_extras(self):
        prelude = TurnPrelude()
        prelude.mark("preamble")

        payload = _emitted(
            prelude, session_id="sess-1", extra={"isResume": False}
        )

        assert payload["sessionId"] == "sess-1"
        assert payload["streamKind"] == "agent"
        assert payload["isResume"] is False

    def test_carries_no_message_content(self):
        """A latency record is held to the same content-free rule as the cost
        rows — there is no field here for a prompt, and none should be added."""
        prelude = TurnPrelude()
        prelude.mark("preamble")

        payload = _emitted(prelude)

        assert set(payload) == {"sessionId", "streamKind", "totalMs", "stages"}


class TestFailSoft:
    def test_a_mark_that_cannot_be_taken_never_raises(self, monkeypatch):
        prelude = TurnPrelude()

        def _boom():
            raise RuntimeError("clock exploded")

        monkeypatch.setattr(
            "apis.inference_api.chat.turn_timing.time.perf_counter", _boom
        )
        prelude.mark("preamble")  # must not raise

    def test_an_unserializable_extra_never_raises(self, caplog):
        prelude = TurnPrelude()
        prelude.mark("preamble")

        class _Hostile:
            def __repr__(self):
                raise RuntimeError("no")

        # `default=str` calls repr on the way out; the emit must swallow it
        # rather than take the turn down with it.
        prelude.emit(
            session_id="s", stream_kind="agent", extra={"bad": _Hostile()}
        )

    def test_emit_writes_exactly_one_line(self, caplog):
        prelude = TurnPrelude()
        prelude.mark("preamble")

        with caplog.at_level(logging.INFO, logger="apis.inference_api.chat.turn_timing"):
            prelude.emit(session_id="s", stream_kind="agent")

        lines = [r for r in caplog.records if r.getMessage().startswith("turn_prelude ")]
        assert len(lines) == 1


def _emitted(prelude, *, session_id="s", extra=None):
    """The JSON payload the emit would log, parsed back."""
    captured = {}

    class _Sink(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if message.startswith("turn_prelude "):
                captured.update(json.loads(message[len("turn_prelude ") :]))

    logger = logging.getLogger("apis.inference_api.chat.turn_timing")
    handler = _Sink()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        prelude.emit(session_id=session_id, stream_kind="agent", extra=extra)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return captured
