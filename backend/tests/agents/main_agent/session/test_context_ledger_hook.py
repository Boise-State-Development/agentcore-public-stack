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
