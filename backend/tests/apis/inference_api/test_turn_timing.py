"""Pre-stream turn timing (docs/specs/agent-state-feedback.md).

This exists to answer one question — which stage owns the 3.75s before the
first SSE byte — so what is worth pinning is that it cannot lie about that
(stages in order, deltas not cumulative totals) and cannot break the turn it
measures.
"""

import json
import logging

from apis.inference_api.chat.turn_timing import TurnPrelude, _metric_name


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

    def test_a_flat_prelude_carries_no_groups_key(self):
        """A group of one is noise — the number is already in `stages`."""
        prelude = TurnPrelude()
        prelude.mark("rag")

        assert "groups" not in _emitted(prelude)


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


class TestGroups:
    """Sub-stages sum back into the stage they decompose.

    docs/specs/turn-latency-preamble.md splits `preamble` into five. The only
    measurement anyone has of the pre-stream window is stated in terms of the
    single coarse number, so the split has to keep reproducing it or it
    discards its own baseline.
    """

    def test_dotted_substages_sum_into_their_prefix(self, monkeypatch):
        clock = iter([0.0, 0.1, 0.4, 1.0, 1.1])
        monkeypatch.setattr(
            "apis.inference_api.chat.turn_timing.time.perf_counter",
            lambda: next(clock),
        )

        prelude = TurnPrelude()  # consumes 0.0
        prelude.mark("preamble.ownership")  # 100ms
        prelude.mark("preamble.quota")  # 300ms
        prelude.mark("rag")  # 600ms

        payload = _emitted(prelude)  # total_ms consumes 1.1

        assert payload["groups"] == {"preamble": 400}

    def test_the_substages_stay_visible_alongside_the_group(self):
        """The group is an addition, not a replacement: the whole point of the
        split is knowing WHICH sub-stage owns the time."""
        prelude = TurnPrelude()
        prelude.mark("preamble.ownership")
        prelude.mark("preamble.quota")

        payload = _emitted(prelude)

        assert list(payload["stages"]) == ["preamble.ownership", "preamble.quota"]

    def test_undotted_stages_are_not_grouped(self, monkeypatch):
        clock = iter([0.0, 0.2, 0.5, 0.6])
        monkeypatch.setattr(
            "apis.inference_api.chat.turn_timing.time.perf_counter",
            lambda: next(clock),
        )

        prelude = TurnPrelude()
        prelude.mark("preamble.files")  # 200ms
        prelude.mark("agent_build")  # 300ms

        assert _emitted(prelude)["groups"] == {"preamble": 200}

    def test_a_group_never_breaks_the_turn_it_measures(self, monkeypatch):
        """Same fail-soft contract as `mark`: a stage name that is not a string
        must cost a log line, not the turn."""
        prelude = TurnPrelude()
        prelude._marks.append((None, 5.0))  # type: ignore[arg-type]

        prelude.emit(session_id="s", stream_kind="agent")  # must not raise


class TestMetricNames:
    """The EMF metric name is derived from the stage name, not looked up.

    A hand-kept table drifts silently: the dashboard widget renders an empty
    graph, which is indistinguishable from "that stage never ran".
    """

    def test_dotted_and_snake_stages_become_camel_case_ms(self):
        assert _metric_name("preamble.session_state") == "PreambleSessionStateMs"
        assert _metric_name("preamble.ownership") == "PreambleOwnershipMs"
        assert _metric_name("agent_build") == "AgentBuildMs"
        assert _metric_name("rag") == "RagMs"

    def test_a_group_prefix_renders_the_same_either_way(self):
        """`groups.preamble` and a flat `preamble` mark must not disagree —
        they are the same quantity, so the same name is correct."""
        assert _metric_name("preamble") == "PreambleMs"


class TestMetrics:
    """One EMF record per turn, so the fleet has percentiles.

    The log line answers "where did THIS turn go"; only an aggregate can
    answer "did the change help", which is the question
    docs/specs/turn-latency-preamble.md exists to make answerable.
    """

    def test_emits_every_stage_plus_the_group_and_the_total(self, monkeypatch):
        record = _emf_record(monkeypatch, ["preamble.ownership", "preamble.quota", "agent_build"])

        assert set(record["metrics"]) == {
            "PreludeTotalMs",
            "PreambleOwnershipMs",
            "PreambleQuotaMs",
            "PreambleMs",
            "AgentBuildMs",
        }

    def test_every_metric_is_milliseconds(self, monkeypatch):
        record = _emf_record(monkeypatch, ["preamble.quota"])

        assert set(record["units"].values()) == {"Milliseconds"}

    def test_the_group_total_rides_along_with_its_substages(self, monkeypatch):
        """Both, not either: the sub-stages answer the new question and the
        group keeps the pre-split baseline comparable."""
        record = _emf_record(monkeypatch, ["preamble.ownership", "preamble.quota"])

        assert "PreambleMs" in record["metrics"]
        assert "PreambleOwnershipMs" in record["metrics"]

    def test_turn_shape_rides_as_properties_not_dimensions(self, monkeypatch):
        """Dimensions multiply metric streams AND invite reading a p99 off a
        slice too thin to have one. The Logs Insights widgets slice instead."""
        record = _emf_record(
            monkeypatch,
            ["preamble.quota"],
            extra={"isResume": True, "deferredBuild": False, "hasAssistant": True},
        )

        assert record["properties"]["isResume"] is True
        assert record["properties"]["deferredBuild"] is False
        assert record["properties"]["streamKind"] == "agent"

    def test_carries_no_message_content(self, monkeypatch):
        """Same content-free rule as the log line and the cost rows."""
        record = _emf_record(monkeypatch, ["preamble.quota"])

        assert set(record["properties"]) <= {
            "streamKind",
            "sessionId",
            "isResume",
            "deferredBuild",
            "hasAssistant",
        }

    def test_the_kill_switch_suppresses_only_the_metrics(self, monkeypatch, caplog):
        """The marks stay ungated — they cost a perf_counter. The metrics cost
        log ingestion and custom-metric charges, hence the switch."""
        monkeypatch.setenv("TURN_LATENCY_METRICS_ENABLED", "false")
        calls = []
        monkeypatch.setattr(
            "apis.shared.observability.emf.emit_emf_metrics",
            lambda **kw: calls.append(kw),
        )

        prelude = TurnPrelude()
        prelude.mark("preamble.quota")
        with caplog.at_level(logging.INFO, logger="apis.inference_api.chat.turn_timing"):
            prelude.emit(session_id="s", stream_kind="agent")

        assert calls == []
        assert [r for r in caplog.records if r.getMessage().startswith("turn_prelude ")]

    def test_an_empty_flag_value_leaves_metrics_on(self, monkeypatch):
        """House rule: a workflow env var can materialize as "" and that must
        not read as off."""
        monkeypatch.setenv("TURN_LATENCY_METRICS_ENABLED", "")

        assert _emf_record(monkeypatch, ["preamble.quota"])["metrics"]

    def test_a_metrics_failure_never_costs_the_log_line(self, monkeypatch, caplog):
        """Two independent readers of the same turn; neither may take the
        other down, and neither may take the turn down."""
        def _boom(**_kwargs):
            raise RuntimeError("cloudwatch exploded")

        monkeypatch.setattr(
            "apis.shared.observability.emf.emit_emf_metrics", _boom
        )

        prelude = TurnPrelude()
        prelude.mark("preamble.quota")
        with caplog.at_level(logging.INFO, logger="apis.inference_api.chat.turn_timing"):
            prelude.emit(session_id="s", stream_kind="agent")  # must not raise

        assert [r for r in caplog.records if r.getMessage().startswith("turn_prelude ")]


def _emf_record(monkeypatch, stages, *, extra=None):
    """Capture the kwargs the prelude hands to `emit_emf_metrics`."""
    captured = {}
    monkeypatch.setattr(
        "apis.shared.observability.emf.emit_emf_metrics",
        lambda **kw: captured.update(kw),
    )

    prelude = TurnPrelude()
    for stage in stages:
        prelude.mark(stage)
    prelude.emit(session_id="s", stream_kind="agent", extra=extra)
    return captured


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
