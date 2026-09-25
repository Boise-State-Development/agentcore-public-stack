"""`ContextLedgerHook` — per model call, the window's trimmed-message count and
the compaction decisions taken since the previous call.

Same lifecycle contract as the tool census: keyed by the turn's Nth model
call, reset per turn, read (never drained) at turn end, off with the same
kill switch, and never able to break a turn.
"""

from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.hooks.context_ledger import _MAX_EVENTS_PER_CALL, ContextLedgerHook


@pytest.fixture(autouse=True)
def diagnostics_enabled(monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)


class _Manager:
    def __init__(self, events=None):
        self._events = list(events or [])

    def drain_compaction_events(self):
        events, self._events = self._events, []
        return events


def _agent(removed=None, manager=None):
    agent = MagicMock()
    agent.conversation_manager.removed_message_count = removed
    agent._session_manager = manager
    return agent


def _call(hook, agent):
    hook._on_before_model_call(MagicMock(agent=agent))


def test_records_the_window_count_and_drains_compaction_events_per_call():
    hook = ContextLedgerHook()
    manager = _Manager([{"kind": "applied", "checkpoint": 12, "summaryTokens": 900}])
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=0, manager=manager))
    _call(hook, _agent(removed=4, manager=manager))  # nothing left to drain

    assert hook.ledger_for_call(0) == {
        "windowRemovedMessages": 0,
        "compactionEvents": [{"kind": "applied", "checkpoint": 12, "summaryTokens": 900}],
    }
    assert hook.ledger_for_call(1) == {"windowRemovedMessages": 4}
    assert hook.ledger_for_call(2) is None


def test_a_new_turn_forgets_the_previous_one():
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=7))
    hook._on_turn_start(MagicMock())
    assert hook.ledger_for_call(0) is None


def test_reads_do_not_drain_and_do_not_alias():
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=1, manager=_Manager([{"kind": "checkpoint"}])))
    first = hook.ledger_for_call(0)
    first["compactionEvents"].append({"kind": "tampered"})
    assert hook.ledger_for_call(0)["compactionEvents"] == [{"kind": "checkpoint"}]


def test_no_window_manager_and_no_session_manager_records_nothing():
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    agent = MagicMock()
    agent.conversation_manager = None
    agent._session_manager = None
    _call(hook, agent)
    assert hook.ledger_for_call(0) is None


def test_kill_switch_records_nothing_and_reads_none(monkeypatch):
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=3, manager=_Manager([{"kind": "applied"}])))
    assert hook.ledger_for_call(0) is None


def test_event_list_is_bounded_and_malformed_counts_never_raise():
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    many = [{"kind": "applied"}] * (_MAX_EVENTS_PER_CALL + 5)
    _call(hook, _agent(removed="not-a-number", manager=_Manager(many)))
    entry = hook.ledger_for_call(0)
    assert "windowRemovedMessages" not in entry
    assert len(entry["compactionEvents"]) == _MAX_EVENTS_PER_CALL


def test_a_raising_manager_is_swallowed():
    class Broken:
        def drain_compaction_events(self):
            raise RuntimeError("boom")

    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=2, manager=Broken()))
    assert hook.ledger_for_call(0) == {"windowRemovedMessages": 2}


# --- Post-turn decisions land on the call that triggered them ---------------
#
# `update_after_turn` runs after the turn's last model call. Its decisions
# (checkpoint / forced / floor_unreachable) used to wait in the manager's queue
# for the NEXT call on the SAME manager instance; a 2026-09-25 prod readout
# found ~43% of cuts never got one (new microVM, agent-cache miss, or the
# session was never resumed). They now attach to the turn's last call.


def test_post_turn_events_attach_to_the_turns_last_call():
    hook = ContextLedgerHook()
    manager = _Manager([{"kind": "applied", "checkpoint": 4}])
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=0, manager=manager))  # drains `applied`
    _call(hook, _agent(removed=0, manager=manager))
    manager._events = [
        {"kind": "floor_unreachable", "checkpoint": 30, "inputTokens": 120_000},
        {"kind": "checkpoint", "checkpoint": 30, "inputTokens": 120_000},
    ]

    hook.record_post_turn_events(manager)

    assert hook.ledger_for_call(0)["compactionEvents"] == [{"kind": "applied", "checkpoint": 4}]
    assert hook.ledger_for_call(1) == {
        "windowRemovedMessages": 0,
        "compactionEvents": [
            {"kind": "floor_unreachable", "checkpoint": 30, "inputTokens": 120_000},
            {"kind": "checkpoint", "checkpoint": 30, "inputTokens": 120_000},
        ],
    }
    assert manager.drain_compaction_events() == []  # nothing left for a later call


def test_post_turn_events_append_to_what_the_call_already_carries_and_stay_bounded():
    hook = ContextLedgerHook()
    manager = _Manager([{"kind": "applied"}])
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(manager=manager))
    manager._events = [{"kind": "checkpoint"}] * (_MAX_EVENTS_PER_CALL + 3)

    hook.record_post_turn_events(manager)

    events = hook.ledger_for_call(0)["compactionEvents"]
    assert events[0] == {"kind": "applied"}
    assert len(events) == _MAX_EVENTS_PER_CALL


def test_post_turn_events_create_the_entry_when_the_call_recorded_nothing():
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    agent = MagicMock()
    agent.conversation_manager = None
    agent._session_manager = None
    _call(hook, agent)
    assert hook.ledger_for_call(0) is None

    hook.record_post_turn_events(_Manager([{"kind": "checkpoint", "checkpoint": 8}]))

    assert hook.ledger_for_call(0) == {"compactionEvents": [{"kind": "checkpoint", "checkpoint": 8}]}


def test_post_turn_events_stay_queued_when_the_turn_made_no_model_call():
    hook = ContextLedgerHook()
    manager = _Manager([{"kind": "checkpoint"}])
    hook._on_turn_start(MagicMock())

    hook.record_post_turn_events(manager)

    assert hook.ledger_for_call(0) is None
    assert manager.drain_compaction_events() == [{"kind": "checkpoint"}]


def test_post_turn_events_respect_the_kill_switch_and_never_raise(monkeypatch):
    class Broken:
        def drain_compaction_events(self):
            raise RuntimeError("boom")

    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=1))
    hook.record_post_turn_events(Broken())
    hook.record_post_turn_events(None)
    assert hook.ledger_for_call(0) == {"windowRemovedMessages": 1}

    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    manager = _Manager([{"kind": "checkpoint"}])
    hook.record_post_turn_events(manager)
    assert manager.drain_compaction_events() == [{"kind": "checkpoint"}]


def test_a_cut_is_attributed_even_when_the_next_turn_runs_on_a_fresh_manager(make_session_manager):
    """The prod failure: turn N cuts on manager A; turn N+1 runs on manager B
    (new microVM / agent-cache miss), whose queue is empty. The cut must
    already be on turn N's last call."""
    manager_a = make_session_manager()
    hook = ContextLedgerHook()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=0, manager=manager_a))
    manager_a.record_compaction_event("checkpoint", checkpoint=26, summaryTokens=760, inputTokens=111_000)

    hook.record_post_turn_events(manager_a)

    assert hook.ledger_for_call(0)["compactionEvents"] == [
        {"kind": "checkpoint", "checkpoint": 26, "summaryTokens": 760, "inputTokens": 111_000}
    ]
    manager_b = make_session_manager()
    hook._on_turn_start(MagicMock())
    _call(hook, _agent(removed=0, manager=manager_b))
    assert "compactionEvents" not in (hook.ledger_for_call(0) or {})
