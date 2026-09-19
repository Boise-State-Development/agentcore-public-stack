"""Agent-build stage recorder (docs/specs/turn-latency-preamble.md PR-4).

`agent_build` is the largest number left in the prelude — ~2950ms cold against
1-47ms warm — and nothing said which part of it that was. These pin the
recorder's contract, which is entirely about NOT being able to break the turn
it measures.
"""

import pytest

from apis.shared.observability import build_stages


@pytest.fixture(autouse=True)
def _no_recorder():
    """Every test starts with nothing installed, which is also the state every
    caller outside the inference-api turn path is in."""
    token = build_stages.set_stage_recorder(None)
    yield
    build_stages.reset_stage_recorder(token)


class TestRecording:
    def test_marks_reach_the_installed_recorder(self):
        seen = []
        token = build_stages.set_stage_recorder(seen.append)

        build_stages.mark_stage("tools")
        build_stages.mark_stage("hooks")

        build_stages.reset_stage_recorder(token)
        assert seen == ["tools", "hooks"]

    def test_marking_without_a_recorder_is_a_silent_no_op(self):
        """The agent is constructed from tests, the scheduled-runs Lambda and
        app-api, none of which install one. A mark there must cost nothing and
        raise nothing."""
        build_stages.mark_stage("tools")  # must not raise

    def test_reset_restores_the_previous_recorder(self):
        outer = []
        inner = []
        outer_token = build_stages.set_stage_recorder(outer.append)
        inner_token = build_stages.set_stage_recorder(inner.append)

        build_stages.mark_stage("inner")
        build_stages.reset_stage_recorder(inner_token)
        build_stages.mark_stage("outer")
        build_stages.reset_stage_recorder(outer_token)

        assert inner == ["inner"]
        assert outer == ["outer"]

    def test_after_reset_nothing_is_recorded(self):
        """A build that finishes must not keep marking. A leaked recorder would
        attribute the NEXT turn's stages to a finished prelude."""
        seen = []
        token = build_stages.set_stage_recorder(seen.append)
        build_stages.reset_stage_recorder(token)

        build_stages.mark_stage("stray")

        assert seen == []


class TestFailSoft:
    def test_a_recorder_that_raises_never_breaks_the_build(self):
        """The whole module is a measurement. A measurement that can break the
        thing it measures is worse than no measurement — the same contract
        `TurnPrelude.mark` already holds."""
        def _boom(_stage):
            raise RuntimeError("clock exploded")

        token = build_stages.set_stage_recorder(_boom)
        try:
            build_stages.mark_stage("tools")  # must not raise
        finally:
            build_stages.reset_stage_recorder(token)

    def test_a_bad_reset_token_never_raises(self):
        class _Hostile:
            pass

        build_stages.reset_stage_recorder(_Hostile())  # must not raise


class TestPreludeIntegration:
    def test_sub_stages_group_under_agent_build(self):
        """`groups.agent_build` has to reproduce the pre-split number, or the
        decomposition throws away the only series anyone has — the same
        contract the preamble split holds."""
        import json
        import logging

        from apis.inference_api.chat.turn_timing import TurnPrelude

        prelude = TurnPrelude()
        prelude.mark("preamble.quota")

        token = build_stages.set_stage_recorder(
            lambda stage: prelude.mark(f"agent_build.{stage}")
        )
        for stage in ("prompt", "registry", "session_mgr", "tools"):
            build_stages.mark_stage(stage)
        build_stages.reset_stage_recorder(token)
        prelude.mark("agent_build.rest")

        captured = {}

        class _Sink(logging.Handler):
            def emit(self, record):
                message = record.getMessage()
                if message.startswith("turn_prelude "):
                    captured.update(json.loads(message[len("turn_prelude ") :]))

        logger = logging.getLogger("apis.inference_api.chat.turn_timing")
        handler = _Sink()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            prelude.emit(session_id="s", stream_kind="agent")
        finally:
            logger.removeHandler(handler)

        assert "agent_build" in captured["groups"]
        assert "preamble" in captured["groups"]
        assert [k for k in captured["stages"] if k.startswith("agent_build.")] == [
            "agent_build.prompt",
            "agent_build.registry",
            "agent_build.session_mgr",
            "agent_build.tools",
            "agent_build.rest",
        ]
