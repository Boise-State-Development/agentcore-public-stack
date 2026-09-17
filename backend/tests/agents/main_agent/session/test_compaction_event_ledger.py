"""`TurnBasedSessionManager.record_compaction_event` / `drain_compaction_events`
— the queue the `ContextLedgerHook` drains onto the next model call's cost row."""

import pytest

from agents.main_agent.session.turn_based_session_manager import (
    _MAX_PENDING_COMPACTION_EVENTS,
    COMPACTION_EVENT_KINDS,
)


@pytest.fixture(autouse=True)
def diagnostics_enabled(monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)


def test_events_carry_numbers_only_and_drain_once(make_session_manager):
    manager = make_session_manager()
    manager.record_compaction_event(
        "applied", checkpoint=12, summaryTokens=900.7, retainedMessages=30,
        summary="never persisted", flag=True,
    )
    drained = manager.drain_compaction_events()
    assert drained == [{"kind": "applied", "checkpoint": 12, "summaryTokens": 900, "retainedMessages": 30}]
    assert manager.drain_compaction_events() == []


def test_unknown_kinds_are_ignored_and_the_queue_is_bounded(make_session_manager):
    manager = make_session_manager()
    manager.record_compaction_event("mystery", checkpoint=1)
    assert manager.drain_compaction_events() == []
    for _ in range(_MAX_PENDING_COMPACTION_EVENTS + 3):
        manager.record_compaction_event("forced")
    assert len(manager.drain_compaction_events()) == _MAX_PENDING_COMPACTION_EVENTS


def test_reserved_kinds_exist_for_the_scheduling_policy():
    assert {"applied", "checkpoint", "forced", "floor_unreachable"} <= COMPACTION_EVENT_KINDS


def test_kill_switch_records_nothing(make_session_manager, monkeypatch):
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    manager = make_session_manager()
    manager.record_compaction_event("applied", checkpoint=3)
    assert manager.drain_compaction_events() == []
