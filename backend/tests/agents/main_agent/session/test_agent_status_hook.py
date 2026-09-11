"""Tests for AgentStatusHook — live narration of a streaming turn.

The properties under test, in order of how expensive they are to get wrong:

1. **Per-turn reset.** The interrupt path (OAuth consent, tool approval)
   unwinds the event loop without draining. Without the ``BeforeInvocationEvent``
   reset, the next turn's first drain replays stale transitions and the SPA
   narrates tools that are not running.
2. **Drain-once semantics.** A transition handed to the stream coordinator must
   not be handed over again, or the status line stutters backwards.
3. **Duration normalization.** Strands has expressed ``duration`` as both a
   float of seconds and a ``timedelta``; a wrong number on a tool row is worse
   than no number, so anything unrecognized must degrade to ``None``.
4. **Batch capture is bounded** and carries what the summarizer needs.
5. Fail-soft: flag off narrates nothing, and a malformed event never raises.
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.hooks.agent_status import (
    _MAX_CALLS_PER_BATCH,
    _MAX_INPUT_CHARS,
    _MAX_RESULT_CHARS,
    AgentStatusHook,
    _duration_ms,
)


@pytest.fixture(autouse=True)
def status_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_STATUS_ENABLED", "true")


@pytest.fixture
def hook():
    return AgentStatusHook()


def _tool_event(name="list_courses", tool_use_id="t1", tool_input=None, **kwargs):
    event = MagicMock()
    event.tool_use = {
        "toolUseId": tool_use_id,
        "name": name,
        "input": tool_input if tool_input is not None else {"term": "fall"},
    }
    for key, value in kwargs.items():
        setattr(event, key, value)
    return event


def _after_tool_event(name="list_courses", tool_use_id="t1", **kwargs):
    defaults = {
        "duration": 0.25,
        "exception": None,
        "result": {"status": "success", "content": [{"text": "3 courses"}]},
    }
    defaults.update(kwargs)
    return _tool_event(name=name, tool_use_id=tool_use_id, **defaults)


# -- 1. per-turn reset ----------------------------------------------------


def test_turn_start_drops_state_left_by_an_interrupted_turn(hook):
    hook._on_before_model_call(MagicMock())
    hook._on_before_tool_call(_tool_event())
    hook._on_after_tool_call(_after_tool_event())
    assert hook._statuses, "precondition: the aborted turn recorded transitions"

    hook._on_turn_start(MagicMock())

    assert hook.drain_statuses() == []
    assert hook.drain_batches() == []


def test_turn_start_resets_the_cycle_counter(hook):
    hook._on_before_model_call(MagicMock())
    hook._on_before_model_call(MagicMock())
    hook._on_turn_start(MagicMock())
    hook._on_before_model_call(MagicMock())

    assert hook.drain_statuses() == [{"phase": "thinking", "cycle": 1}]


# -- 2. drain-once --------------------------------------------------------


def test_statuses_drain_once(hook):
    hook._on_before_model_call(MagicMock())
    first = hook.drain_statuses()

    assert first == [{"phase": "thinking", "cycle": 1}]
    assert hook.drain_statuses() == []


def test_cycle_increments_across_tool_round_trips(hook):
    hook._on_before_model_call(MagicMock())       # cycle 1: decide to call a tool
    hook._on_before_tool_call(_tool_event())
    hook._on_after_tool_call(_after_tool_event())
    hook._on_before_model_call(MagicMock())       # cycle 2: read results, answer

    phases = [(s["phase"], s["cycle"]) for s in hook.drain_statuses()]
    assert phases == [
        ("thinking", 1),
        ("tool_start", 1),
        ("tool_end", 1),
        ("thinking", 2),
    ]


def test_tool_start_carries_the_name_the_ui_renders(hook):
    hook._on_before_tool_call(_tool_event(name="list_assignments", tool_use_id="tu-9"))

    (status,) = hook.drain_statuses()
    assert status["phase"] == "tool_start"
    assert status["toolName"] == "list_assignments"
    assert status["toolUseId"] == "tu-9"


# -- 3. duration normalization -------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.25, 250),
        (2, 2000),
        (timedelta(milliseconds=1500), 1500),
        (None, None),
        ("quarter of a second", None),
        (object(), None),
    ],
)
def test_duration_normalizes_or_degrades_to_none(raw, expected):
    assert _duration_ms(raw) == expected


def test_negative_duration_clamps_to_zero():
    assert _duration_ms(-1.0) == 0


def test_tool_end_reports_the_measured_duration(hook):
    hook._on_after_tool_call(_after_tool_event(duration=timedelta(seconds=1.2)))

    (status,) = hook.drain_statuses()
    assert status["durationMs"] == 1200
    assert status["ok"] is True


def test_tool_failure_inside_a_successful_invocation_is_not_ok(hook):
    """Strands reports a tool-level failure as a result status, not a raise.

    The rail's red dot depends on catching both, so a result marked "error"
    must flip `ok` even though no exception reached the hook.
    """
    hook._on_after_tool_call(
        _after_tool_event(result={"status": "error", "content": [{"text": "422"}]})
    )

    (status,) = hook.drain_statuses()
    assert status["ok"] is False


def test_raised_exception_is_not_ok(hook):
    hook._on_after_tool_call(_after_tool_event(exception=RuntimeError("boom")))

    (status,) = hook.drain_statuses()
    assert status["ok"] is False


# -- 4. batch capture -----------------------------------------------------


def test_batch_closes_with_what_the_summarizer_needs(hook):
    hook._on_before_tool_call(_tool_event(name="list_courses", tool_use_id="t1"))
    hook._on_after_tool_call(_after_tool_event(name="list_courses", tool_use_id="t1"))
    hook._on_after_tools(MagicMock())

    (batch,) = hook.drain_batches()
    assert batch["batchId"] == "t1"
    assert batch["toolUseIds"] == ["t1"]
    (call,) = batch["calls"]
    assert call["toolName"] == "list_courses"
    assert call["ok"] is True
    assert "3 courses" in call["result"]
    assert "fall" in call["input"]


def test_batches_drain_once(hook):
    hook._on_after_tool_call(_after_tool_event())
    hook._on_after_tools(MagicMock())

    assert len(hook.drain_batches()) == 1
    assert hook.drain_batches() == []


def test_empty_batch_is_not_parked(hook):
    """A batch cancelled before any tool ran has nothing to summarize."""
    hook._on_after_tools(MagicMock())

    assert hook.drain_batches() == []


def test_consecutive_batches_are_separate(hook):
    hook._on_after_tool_call(_after_tool_event(tool_use_id="t1"))
    hook._on_after_tools(MagicMock())
    hook._on_after_tool_call(_after_tool_event(tool_use_id="t2"))
    hook._on_after_tools(MagicMock())

    batches = hook.drain_batches()
    assert [b["batchId"] for b in batches] == ["t1", "t2"]


def test_batch_capture_is_width_bounded(hook):
    for i in range(_MAX_CALLS_PER_BATCH + 10):
        hook._on_after_tool_call(_after_tool_event(tool_use_id=f"t{i}"))
    hook._on_after_tools(MagicMock())

    (batch,) = hook.drain_batches()
    assert len(batch["calls"]) == _MAX_CALLS_PER_BATCH


def test_oversized_payloads_are_truncated_at_capture(hook):
    """An 8MB tool result must not sit in memory for the life of the turn."""
    hook._on_after_tool_call(
        _after_tool_event(
            tool_input={"q": "x" * 50_000},
            result={"status": "success", "content": [{"text": "y" * 500_000}]},
        )
    )
    hook._on_after_tools(MagicMock())

    (batch,) = hook.drain_batches()
    (call,) = batch["calls"]
    assert len(call["input"]) <= _MAX_INPUT_CHARS + 3
    assert len(call["result"]) <= _MAX_RESULT_CHARS + 3


def test_batches_are_captured_even_with_narration_off(hook, monkeypatch):
    """Summaries and the status line are gated separately.

    A deployment can want summaries without the live line; the batch record is
    what the summarizer consumes, so it must not ride the narration flag.
    """
    monkeypatch.setenv("AGENT_STATUS_ENABLED", "false")
    hook._on_after_tool_call(_after_tool_event())
    hook._on_after_tools(MagicMock())

    assert hook.drain_statuses() == [], "the live line is off"
    (batch,) = hook.drain_batches()
    assert batch["calls"], "but the batch is still there to summarize"


# -- 5. fail-soft ---------------------------------------------------------


def test_flag_off_narrates_nothing(hook, monkeypatch):
    monkeypatch.setenv("AGENT_STATUS_ENABLED", "false")

    hook._on_before_model_call(MagicMock())
    hook._on_before_tool_call(_tool_event())
    hook._on_after_tool_call(_after_tool_event())

    assert hook.drain_statuses() == []


def test_malformed_tool_event_never_raises(hook):
    broken = MagicMock()
    broken.tool_use = "not-a-dict"

    hook._on_before_tool_call(broken)
    hook._on_after_tool_call(broken)

    assert hook.drain_statuses() == []


def test_tool_event_without_a_name_is_skipped(hook):
    """A nameless tool has nothing the UI could say about it."""
    event = MagicMock()
    event.tool_use = {"toolUseId": "t1"}

    hook._on_before_tool_call(event)

    assert hook.drain_statuses() == []


def test_status_queue_is_capped(hook):
    from agents.main_agent.session.hooks.agent_status import _MAX_QUEUED_STATUSES

    for _ in range(_MAX_QUEUED_STATUSES + 50):
        hook._on_before_model_call(MagicMock())

    assert len(hook.drain_statuses()) == _MAX_QUEUED_STATUSES
