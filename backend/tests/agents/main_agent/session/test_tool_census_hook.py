"""Tests for ToolCensusHook — the content-free per-call tool tally.

Properties, in order of how expensive they are to get wrong:

1. **Attribution.** A tool that runs during model-call cycle N belongs to
   coordinator call index N-1. Off by one and every tool lands on the wrong
   cost row, and the trajectory view lies about which call requested what.
2. **Per-turn reset.** The interrupt path unwinds without a turn end; without
   the ``BeforeInvocationEvent`` reset the next turn inherits a stale tally.
3. **Non-drained reads.** The coordinator reads each call's tally once at turn
   end; a read must not empty it (the same call could be read twice on a
   retry) and must not alias the hook's state.
4. **Failure counting** matches ``AgentStatusHook``: an exception OR a result
   with ``status == "error"``.
5. Fail-soft: the kill switch yields ``None`` everywhere, a malformed event
   never raises, and the per-call distinct-tool cap holds.
"""

from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.hooks.tool_census import _MAX_TOOLS_PER_CALL, ToolCensusHook


@pytest.fixture(autouse=True)
def census_enabled(monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)


@pytest.fixture
def hook():
    return ToolCensusHook()


def _after(name="list_courses", *, exception=None, status="success"):
    event = MagicMock()
    event.tool_use = {"toolUseId": "t", "name": name, "input": {}}
    event.exception = exception
    event.result = {"status": status, "content": []}
    return event


def _turn(hook: ToolCensusHook, *cycles):
    """Drive a turn: each element of ``cycles`` is the list of after-tool
    events that run after that model call."""
    hook._on_turn_start(MagicMock())
    for events in cycles:
        hook._on_before_model_call(MagicMock())
        for event in events:
            hook._on_after_tool_call(event)


def test_tools_are_attributed_to_the_model_call_that_requested_them(hook):
    _turn(
        hook,
        [_after("list_courses"), _after("list_courses"), _after("get_syllabus")],  # after call 0
        [],                                                                       # call 1 ran no tools
        [_after("calculator", status="error")],                                   # after call 2
    )
    assert hook.tally_for_call(0) == {
        "list_courses": {"calls": 2, "errors": 0},
        "get_syllabus": {"calls": 1, "errors": 0},
    }
    assert hook.tally_for_call(1) is None
    assert hook.tally_for_call(2) == {"calculator": {"calls": 1, "errors": 1}}
    assert hook.tally_for_call(3) is None  # the final answer call requested nothing


def test_a_new_turn_forgets_the_previous_one(hook):
    _turn(hook, [_after("a")])
    assert hook.tally_for_call(0) is not None
    _turn(hook, [])
    assert hook.tally_for_call(0) is None


def test_reads_do_not_drain_and_do_not_alias(hook):
    _turn(hook, [_after("a")])
    first = hook.tally_for_call(0)
    first["a"]["calls"] = 99  # mutate the copy
    second = hook.tally_for_call(0)
    assert second == {"a": {"calls": 1, "errors": 0}}


def test_failures_count_on_exception_or_error_status(hook):
    _turn(hook, [
        _after("t", exception=RuntimeError("boom")),
        _after("t", status="error"),
        _after("t"),
    ])
    assert hook.tally_for_call(0) == {"t": {"calls": 3, "errors": 2}}


def test_kill_switch_records_nothing_and_reads_none(monkeypatch):
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    hook = ToolCensusHook()
    _turn(hook, [_after("a")])
    assert hook.tally_for_call(0) is None
    assert hook._tally == {}


def test_empty_string_flag_means_on(monkeypatch):
    # A GitHub Actions variable that is unset arrives as "", which must not disable.
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "")
    hook = ToolCensusHook()
    _turn(hook, [_after("a")])
    assert hook.tally_for_call(0) == {"a": {"calls": 1, "errors": 0}}


def test_malformed_events_never_raise(hook):
    hook._on_turn_start(MagicMock())
    hook._on_before_model_call(MagicMock())
    bad = MagicMock()
    bad.tool_use = "not-a-dict"
    hook._on_after_tool_call(bad)
    nameless = MagicMock()
    nameless.tool_use = {"toolUseId": "t"}
    hook._on_after_tool_call(nameless)
    assert hook.tally_for_call(0) is None


def test_distinct_tool_cap_holds_but_known_tools_keep_counting(hook):
    hook._on_turn_start(MagicMock())
    hook._on_before_model_call(MagicMock())
    for i in range(_MAX_TOOLS_PER_CALL + 5):
        hook._on_after_tool_call(_after(f"tool_{i}"))
    hook._on_after_tool_call(_after("tool_0"))
    tally = hook.tally_for_call(0)
    assert len(tally) == _MAX_TOOLS_PER_CALL
    assert tally["tool_0"]["calls"] == 2
    assert f"tool_{_MAX_TOOLS_PER_CALL + 1}" not in tally


def test_an_app_form_waiting_for_the_user_is_not_an_error(hook):
    """GitHub's App stub is `isError` on purpose while its form is open."""
    from types import SimpleNamespace

    event = _after("issue_write", status="error")
    event.selected_tool = SimpleNamespace(
        mcp_tool=SimpleNamespace(meta={"ui": {"resourceUri": "ui://github/issue_write"}})
    )
    event.result["content"] = [
        {"text": "An interactive form has been shown to the user for creating a new issue"}
    ]

    _turn(hook, [event])

    assert hook.tally_for_call(0) == {"issue_write": {"calls": 1, "errors": 0}}
