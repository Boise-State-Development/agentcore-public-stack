"""A watch run's pause answered in its conversation saves its ``report`` (#2490).

docs/specs/answer-from-inbox.md §3.6. The Runtime leaves a finished watch-run resume's
``report`` on the session's ``META`` row in the write that clears the pause
(``clear_paused_turn(watch_resume_report=…)``); the worker's hold check saves it into the
watcher's state and ends the hold in one conditional write, so the next run starts past the
finding. A bell resume ends the hold in its own save, so the record is never applied twice.

moto DynamoDB with the real scheduled_prompts, watch_state, sessions and notifications code;
only ``run_agent_headless`` is stubbed.
"""

from __future__ import annotations

import pytest

from apis.shared.scheduled_prompts.conversation_resume import (
    WATCH_RESUME_REPORT_ATTR,
    apply_conversation_resume,
    report_from_messages,
    resume_report_record,
)
from apis.shared.scheduled_prompts.watch_state import WatchState, get_watch_state, save_watch_state
from apis.shared.sessions.metadata import clear_paused_turn
from lambdas.scheduled_runs_worker import worker
from tests.lambdas.test_scheduled_runs_inbox_resume import RESUME, _claim, _event, _paused, _resumed
from tests.lambdas.test_scheduled_runs_needs_user import APPROVAL
from tests.lambdas.test_scheduled_runs_watch import (
    FINISHED,
    SESSION,
    USER_ID,
    _inbox,
    _report,
    _result,
    _run,
    _session_row,
    _stub,
)
from tests.shared.test_project_schedules import TABLE, _make_projects_table

pytestmark = pytest.mark.asyncio

#: The resumed run's report: the approved write happened, so its cursor moves and its key is seen.
RESUMED_REPORT = {
    "outcome": "notify",
    "items": [{"key": "draft-1", "title": "Reply drafted"}],
    "state": {"cursors": {"since": "b"}},
}


@pytest.fixture()
def inbox(sessions_metadata_table, monkeypatch):
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    _make_projects_table()


async def _answered_in_conversation(table, report=RESUMED_REPORT) -> None:
    """What the Runtime does when the conversation's resume finishes: one write, after the stream."""
    table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"S#{SESSION}"},
        UpdateExpression="REMOVE pendingInterrupts",
    )
    await clear_paused_turn(SESSION, USER_ID, watch_resume_report=resume_report_record(report, FINISHED))


class TestTheRuntimeRecordsTheReport:
    def test_only_the_resumed_legs_last_report_counts(self):
        before = {"role": "assistant", "content": [{"toolUse": {"name": "report", "input": {"outcome": "nothing_new"}}}]}
        resumed = [
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}]}}]},
            {"role": "assistant", "content": [{"toolUse": {"name": "report", "input": {"outcome": "notify"}}}]},
            {"role": "assistant", "content": [{"toolUse": {"name": "report", "input": RESUMED_REPORT}}]},
        ]
        assert report_from_messages(resumed) == RESUMED_REPORT
        assert report_from_messages([before]) == {"outcome": "nothing_new"}
        assert report_from_messages([{"role": "assistant", "content": [{"text": "done"}]}]) is None

    def test_a_record_past_the_ceiling_is_dropped(self):
        assert resume_report_record({"x": "y" * 40_000}, FINISHED) is None
        assert resume_report_record(None, FINISHED) == {"report": None, "finishedAt": FINISHED}

    async def test_the_record_lands_in_the_write_that_ends_the_pause(self, inbox, sessions_metadata_table, monkeypatch):
        await _paused(monkeypatch, sessions_metadata_table, mode="watch")
        await _answered_in_conversation(sessions_metadata_table)
        row = _session_row(sessions_metadata_table)
        assert "pausedTurn" not in row
        assert row[WATCH_RESUME_REPORT_ATTR]["report"]["state"]["cursors"] == {"since": "b"}


class TestConversationAnswer:
    async def test_the_next_tick_saves_the_report_and_runs_past_the_finding(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule, _ = await _paused(monkeypatch, sessions_metadata_table, mode="watch")
        held = get_watch_state(USER_ID, schedule.schedule_id)
        await _answered_in_conversation(sessions_metadata_table)
        calls = _stub(
            monkeypatch, _result(session_id="headless-run2", trace=[_report(outcome="nothing_new")])
        )

        result = await _run(schedule)

        assert result["result"] == "quiet"
        (call,) = calls
        # The run read the conversation's cursor and seen key, so it does not ask again.
        assert '"since": "b"' in call["prompt"] and "draft-1" in call["prompt"]
        state = get_watch_state(USER_ID, schedule.schedule_id)
        assert state.cursors == {"since": "b"}
        assert state.seen == ["k1", "draft-1"]
        assert state.paused_session_id is None
        # One save for the conversation's report, one for the run: neither lost to the other.
        assert state.version == held.version + 2
        # Like a bell resume, it raises no agent_item for what the user saw in the conversation.
        assert _inbox("agent_item") == []

    async def test_a_watcher_state_that_moved_on_is_not_written_over(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule, _ = await _paused(monkeypatch, sessions_metadata_table, mode="watch")
        stale = get_watch_state(USER_ID, schedule.schedule_id)
        await _answered_in_conversation(sessions_metadata_table)
        # Someone else wrote since `stale` was read (another tick, a "Start over").
        save_watch_state(
            USER_ID, schedule.schedule_id, WatchState(cursors={"since": "z"}, seen=["k9"]),
            expected_version=stale.version,
        )
        record = _session_row(sessions_metadata_table)[WATCH_RESUME_REPORT_ATTR]

        assert apply_conversation_resume(USER_ID, schedule.schedule_id, schedule.label, stale, record) is False
        state = get_watch_state(USER_ID, schedule.schedule_id)
        assert (state.cursors, state.seen, state.version) == ({"since": "z"}, ["k9"], stale.version + 1)


class TestBellAnswer:
    async def test_a_bell_resume_saves_once(self, inbox, sessions_metadata_table, monkeypatch):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table, mode="watch")
        held = get_watch_state(USER_ID, schedule.schedule_id)
        _claim(row)
        # The Runtime records the report on this resume too: it can't tell the bell's from the
        # conversation's.
        _resumed(
            monkeypatch,
            sessions_metadata_table,
            _result("completed", run_id=RESUME, trace=[_report(**RESUMED_REPORT)]),
            during=lambda: sessions_metadata_table.update_item(
                Key={"PK": f"USER#{USER_ID}", "SK": f"S#{SESSION}"},
                UpdateExpression="SET #wr = :wr",
                ExpressionAttributeNames={"#wr": WATCH_RESUME_REPORT_ATTR},
                ExpressionAttributeValues={":wr": resume_report_record(RESUMED_REPORT, FINISHED)},
            ),
        )
        await worker.run_inbox_resume(_event(row))
        after_bell = get_watch_state(USER_ID, schedule.schedule_id)
        assert after_bell.version == held.version + 1 and after_bell.paused_session_id is None

        applied = []
        monkeypatch.setattr(worker, "apply_conversation_resume", lambda *a: applied.append(a) or True)
        _stub(monkeypatch, _result(session_id="headless-run2", trace=[_report(outcome="nothing_new")]))
        await _run(schedule)

        assert applied == []
        state = get_watch_state(USER_ID, schedule.schedule_id)
        assert state.cursors == {"since": "b"} and state.seen == ["k1", "draft-1"]
        assert state.version == after_bell.version + 1
