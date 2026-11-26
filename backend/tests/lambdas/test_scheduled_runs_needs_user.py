"""Scheduled-runs worker, a run that paused on the user (docs/specs/answer-from-inbox.md §3.2).

A personal run that ends ``needs_user`` while the flag is on is recorded as done, leaves a
``needs_user`` history row and one ``agent_needs_*`` inbox row, and holds a watcher until the
pause is answered or expires. With the flag off, or for a pause already gone, it is a completed
run exactly as before.

moto DynamoDB with the real scheduled_prompts, watch_state, sessions and notifications code;
only ``run_agent_headless`` is stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.scheduled_prompts.runs import list_schedule_runs
from apis.shared.scheduled_prompts.service import get_scheduled_prompt
from apis.shared.scheduled_prompts.watch_state import WatchState, get_watch_state, save_watch_state
from tests.lambdas.test_scheduled_runs_watch import (
    SESSION,
    USER_ID,
    _inbox,
    _report,
    _result,
    _run,
    _schedule,
    _session_row,
    _stub,
)
from tests.shared.test_project_schedules import TABLE, _make_projects_table

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def inbox(sessions_metadata_table, monkeypatch):
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    _make_projects_table()


def _in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


APPROVAL = {
    "interruptId": "int-1",
    "kind": "tool_approval",
    "toolName": "send_message",
    "toolInput": json.dumps({"to": "someone", "body": "the drafted reply"}),
    "message": "Approve sending?",
    "createdAt": "2026-11-25T01:00:00Z",
}
QUESTION = {
    "interruptId": "int-2",
    "kind": "user_question",
    "questions": json.dumps([
        {"header": "Calendar", "question": "Which calendar should I use?", "options": [{"label": "Work"}]},
        {"header": "Time", "question": "When?", "options": [{"label": "Now"}]},
    ]),
    "createdAt": "2026-11-25T01:00:00Z",
}


def _pause(table, *interrupts, session_id: str = SESSION, expires_in_days: float = 7) -> None:
    """Put the breadcrumbs and the snapshot on the session's row, as the Runtime does at a pause."""
    table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"S#{session_id}"},
        UpdateExpression="SET pendingInterrupts = :p, pausedTurn = :t",
        ExpressionAttributeValues={
            ":p": list(interrupts),
            ":t": {"expiresAt": _in(expires_in_days), "enabledTools": []},
        },
    )


def _stub_paused(monkeypatch, table, *interrupts, expires_in_days: float = 7, **overrides):
    ids = [i["interruptId"] for i in interrupts]
    return _stub(
        monkeypatch,
        _result("needs_user", pending_interrupt_ids=ids, **overrides),
        during=lambda: _pause(table, *interrupts, expires_in_days=expires_in_days) if interrupts else None,
    )


class TestNeedsUser:
    async def test_a_paused_run_is_done_and_waits_in_the_bell(self, inbox, sessions_metadata_table, monkeypatch):
        schedule = await _schedule(mode="standard")
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL)
        result = await _run(schedule)

        assert result == {
            "scheduleId": schedule.schedule_id,
            "result": "needs_user",
            "sessionId": SESSION,
            "promptCount": 1,
        }
        record = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert (record.last_run_status, record.consecutive_failures) == ("completed", 0)
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert (run.status, run.session_id, run.prompt_count) == ("needs_user", SESSION, 1)
        assert run.expires_at and run.expires_at.endswith("Z")

        (row,) = _inbox("agent_needs_approval")
        assert "actorEmail" not in row and "resolution" not in row
        payload = row["payload"]
        assert payload == {
            "scheduleId": schedule.schedule_id,
            "runId": "run-abc",
            "runFinishedAt": run.finished_at,
            "sessionId": SESSION,
            "label": "Inbox watcher",
            "expiresAt": run.expires_at,
            "answerable": True,
            "prompts": [{"interruptId": "int-1", "kind": "tool_approval", "toolName": "send_message"}],
        }
        # Never the drafted reply, the approval message or anything else from the tool input.
        assert "drafted reply" not in json.dumps(payload, default=str)

    async def test_a_question_is_needs_input_with_a_bounded_headline(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule = await _schedule(mode="standard")
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL, QUESTION)
        await _run(schedule)

        assert _inbox("agent_needs_approval") == []
        (row,) = _inbox("agent_needs_input")
        prompts = row["payload"]["prompts"]
        assert prompts[1] == {
            "interruptId": "int-2",
            "kind": "user_question",
            "headline": "Which calendar should I use? (+1 more)",
        }
        assert "Work" not in json.dumps(prompts)

    async def test_a_browser_sign_in_is_not_answerable_from_the_inbox(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule = await _schedule(mode="standard")
        login = {
            "interruptId": "int-3",
            "kind": "browser_login",
            "reason": "Sign in to the portal so I can read your grades",
            "createdAt": "2026-11-25T01:00:00Z",
        }
        _stub_paused(monkeypatch, sessions_metadata_table, login)
        await _run(schedule)

        (row,) = _inbox("agent_needs_input")
        assert row["payload"]["answerable"] is False
        assert row["payload"]["prompts"][0]["headline"].startswith("Sign in to the portal")

    async def test_a_pause_already_gone_is_a_completed_run(self, inbox, sessions_metadata_table, monkeypatch):
        schedule = await _schedule(mode="standard")
        _stub_paused(monkeypatch, sessions_metadata_table)  # ids reported, no breadcrumbs left
        result = await _run(schedule)

        assert result["result"] == "completed"
        assert [r.status for r in list_schedule_runs(USER_ID, schedule.schedule_id)] == ["completed"]
        assert _inbox("agent_needs_approval") == _inbox("agent_needs_input") == []

    async def test_an_expired_pause_is_a_completed_run(self, inbox, sessions_metadata_table, monkeypatch):
        schedule = await _schedule(mode="standard")
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL, expires_in_days=-1)
        assert (await _run(schedule))["result"] == "completed"
        assert _inbox("agent_needs_approval") == []

    @pytest.mark.parametrize("value", [None, "", "false"])
    async def test_with_the_flag_off_it_is_exactly_a_completed_run(
        self, inbox, sessions_metadata_table, monkeypatch, value
    ):
        schedule = await _schedule(mode="standard")
        if value is None:
            monkeypatch.delenv("PROACTIVE_RUNS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", value)
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL)
        result = await _run(schedule)

        assert result == {"scheduleId": schedule.schedule_id, "result": "completed", "sessionId": SESSION}
        record = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert (record.last_run_status, record.last_run_session_id) == ("completed", SESSION)
        assert list_schedule_runs(USER_ID, schedule.schedule_id) == []
        assert _inbox("agent_needs_approval") == []


class TestHold:
    async def test_a_paused_watch_run_keeps_its_conversation_and_holds(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule = await _schedule()
        save_watch_state(
            USER_ID, schedule.schedule_id, WatchState(cursors={"since": "a"}, seen=["k1"]), expected_version=None
        )
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL)
        result = await _run(schedule)

        assert result["result"] == "needs_user"
        row = _session_row(sessions_metadata_table)
        assert row["status"] == "active" and row.get("unread") is True
        state = get_watch_state(USER_ID, schedule.schedule_id)
        # The hold is set; the cursors and seen keys are left as they were.
        assert (state.cursors, state.seen, state.version) == ({"since": "a"}, ["k1"], 2)
        assert state.paused_session_id == SESSION and state.holding()
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert state.paused_until == run.expires_at

    async def test_a_held_watcher_skips_its_run(self, inbox, sessions_metadata_table, monkeypatch):
        schedule = await _schedule()
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL)
        await _run(schedule)
        calls = _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

        result = await _run(schedule)

        assert result == {"scheduleId": schedule.schedule_id, "result": "held"}
        assert calls == []
        # No "skipped" row: only the run that paused.
        assert [r.status for r in list_schedule_runs(USER_ID, schedule.schedule_id)] == ["needs_user"]

    async def test_an_answered_pause_ends_the_hold(self, inbox, sessions_metadata_table, monkeypatch):
        schedule = await _schedule()
        _stub_paused(monkeypatch, sessions_metadata_table, APPROVAL)
        await _run(schedule)
        # Answered in the conversation: the Runtime clears the breadcrumbs.
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"S#{SESSION}"},
            UpdateExpression="REMOVE pendingInterrupts, pausedTurn",
        )
        calls = _stub(
            monkeypatch,
            _result(session_id="headless-run2", trace=[_report(outcome="nothing_new")]),
        )

        result = await _run(schedule)

        assert result["result"] == "quiet"
        assert len(calls) == 1
        state = get_watch_state(USER_ID, schedule.schedule_id)
        assert state.paused_session_id is None and state.paused_until is None

    async def test_an_expired_hold_runs_without_reading_the_session(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule = await _schedule()
        save_watch_state(USER_ID, schedule.schedule_id, WatchState(), expected_version=None)
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDSTATE#{schedule.schedule_id}"},
            UpdateExpression="SET pausedSessionId = :s, pausedUntil = :u",
            ExpressionAttributeValues={":s": "headless-old", ":u": "2026-01-01T00:00:00Z"},
        )

        async def no_read(*_args, **_kwargs):
            raise AssertionError("an expired hold reads nothing")

        monkeypatch.setattr("lambdas.scheduled_runs_worker.worker._pause_still_live", no_read)
        calls = _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

        assert (await _run(schedule))["result"] == "quiet"
        assert len(calls) == 1
        assert get_watch_state(USER_ID, schedule.schedule_id).paused_session_id is None
