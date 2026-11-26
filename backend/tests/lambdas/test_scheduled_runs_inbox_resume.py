"""Scheduled-runs worker, a pause answered from the inbox (docs/specs/answer-from-inbox.md §3.5–3.6).

A run pauses (2.1's path), app-api claims its inbox row and writes the start audit row, and the
worker gets ``{"kind": "inbox_resume", …}``. Every row of §3.6's table, the start mark that makes
a retried event spend no second model call, and a late event for a claim since retaken.

moto DynamoDB with the real scheduled_prompts, watch_state, sessions, notifications and audit
code; only ``run_agent_headless`` (the harness's HTTP/SSE boundary) is stubbed.
"""

from __future__ import annotations

import json

import pytest

from apis.shared.harness.governance import RunAuditRecorder
from apis.shared.harness.models import OAuthConsentRequired
from apis.shared.notifications.service import NotificationService
from apis.shared.scheduled_prompts.inbox_answer import stale_claim_before
from apis.shared.scheduled_prompts.runs import list_schedule_runs
from apis.shared.scheduled_prompts.service import get_scheduled_prompt
from apis.shared.scheduled_prompts.watch_state import WatchState, get_watch_state, save_watch_state
from lambdas.scheduled_runs_worker import worker
from tests.lambdas.test_scheduled_runs_needs_user import APPROVAL, QUESTION, _pause, _stub_paused
from tests.lambdas.test_scheduled_runs_watch import (
    EMAIL,
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

RESUME = "run-resume00001"


@pytest.fixture()
def inbox(sessions_metadata_table, monkeypatch):
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    _make_projects_table()


async def _paused(monkeypatch, table, *interrupts, mode: str = "standard"):
    """A schedule whose run paused, and its one needs-user inbox row."""
    schedule = await _schedule(mode=mode)
    if mode == "watch":
        save_watch_state(
            USER_ID, schedule.schedule_id, WatchState(cursors={"since": "a"}, seen=["k1"]), expected_version=None
        )
    _stub_paused(monkeypatch, table, *(interrupts or (APPROVAL,)))
    await _run(schedule)
    (row,) = _inbox("agent_needs_approval") + _inbox("agent_needs_input")
    return schedule, row


def _claim(row, resume_run_id: str = RESUME, decision: str = "approved"):
    """What app-api's respond does before its invoke: the claim and the start audit row."""
    claim = NotificationService().claim_resolution(
        EMAIL, row["notificationId"], resume_run_id=resume_run_id, decision=decision,
        stale_before=stale_claim_before(),
    )
    assert claim is not None
    RunAuditRecorder().record_start(
        run_id=resume_run_id, user_id=USER_ID, session_id=SESSION, trigger="inbox_respond", prompt="",
        extra={"resumesRunId": "run-abc", "decisions": [decision]},
    )
    return claim


def _event(row, resume_run_id: str = RESUME, responses=None) -> dict:
    return {
        "kind": "inbox_resume",
        "userId": USER_ID,
        "email": EMAIL,
        "notificationId": row["notificationId"],
        "resumeRunId": resume_run_id,
        "sessionId": SESSION,
        "scheduleId": row["payload"]["scheduleId"],
        "responses": responses or [{"interruptId": "int-1", "response": "approved"}],
    }


def _row(notification_id: str) -> dict:
    return NotificationService().table.get_item(
        Key={"PK": f"INBOX#{EMAIL}", "SK": f"NOTIF#{notification_id}"}
    )["Item"]


def _audit(table, run_id: str = RESUME) -> dict:
    return table.get_item(Key={"PK": f"USER#{USER_ID}", "SK": f"RUN#{run_id}"}).get("Item") or {}


def _clear_pause(table):
    table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"S#{SESSION}"},
        UpdateExpression="REMOVE pendingInterrupts, pausedTurn",
    )


def _resumed(monkeypatch, table, outcome, *, during=None):
    """The resume's run: the Runtime clears the breadcrumbs it answered, unless told otherwise."""

    def clear_then(during=during):
        _clear_pause(table)
        if during is not None:
            during()

    return _stub(monkeypatch, outcome, create_session=False, during=clear_then)


class TestStartMark:
    async def test_the_resume_runs_once_with_the_answers(self, inbox, sessions_metadata_table, monkeypatch):
        _, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        calls = _resumed(monkeypatch, sessions_metadata_table, _result("completed", run_id=RESUME))

        await worker.run_inbox_resume(_event(row))

        (call,) = calls
        assert call["session_id"] == SESSION and call["run_id"] == RESUME and call["prompt"] == ""
        assert call["interrupt_responses"] == [{"interruptId": "int-1", "response": "approved"}]
        assert call["audit_started"] is True and call["mark_unread"] is False
        assert call["trigger"] == "inbox_respond"

    async def test_a_retry_after_a_started_attempt_spends_no_second_call(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        _, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        # The first attempt set its mark and died.
        assert NotificationService().mark_worker_started(EMAIL, row["notificationId"], resume_run_id=RESUME)
        calls = _resumed(monkeypatch, sessions_metadata_table, _result("completed", run_id=RESUME))

        result = await worker.run_inbox_resume(_event(row))

        assert result == {"resumeRunId": RESUME, "result": "resume_lost"}
        assert calls == []
        assert _row(row["notificationId"])["resolution"]["state"] == "retry"
        assert "readAt" not in _row(row["notificationId"])
        audit = _audit(sessions_metadata_table)
        assert (audit["status"], audit["errorDetail"]) == ("error", "resume_lost")

    async def test_a_late_event_for_a_retaken_claim_is_dropped(self, inbox, sessions_metadata_table, monkeypatch):
        _, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        # Given back and claimed again under a new run before the first event arrived.
        service = NotificationService()
        from apis.shared.notifications.models import NotificationResolution

        service.set_resolution(
            EMAIL, row["notificationId"],
            NotificationResolution(state="retry", at="2026-11-26T00:00:00Z", resume_run_id=RESUME),
            claimed_by=RESUME,
        )
        _claim(row, resume_run_id="run-resume00002")
        calls = _resumed(monkeypatch, sessions_metadata_table, _result("completed", run_id=RESUME))

        assert (await worker.run_inbox_resume(_event(row)))["result"] == "dropped"
        assert calls == []
        assert _row(row["notificationId"])["resolution"]["resumeRunId"] == "run-resume00002"
        assert _audit(sessions_metadata_table)["status"] == "error"

    async def test_a_scheduled_run_event_still_runs_the_schedule(self, inbox, sessions_metadata_table, monkeypatch):
        schedule = await _schedule(mode="standard")
        calls = _stub(monkeypatch, _result("completed"))
        result = await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})
        assert result["result"] == "completed" and len(calls) == 1


class TestFinish:
    async def test_finished(self, inbox, sessions_metadata_table, monkeypatch):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"S#{SESSION}"}, UpdateExpression="REMOVE unread"
        )
        _resumed(
            monkeypatch, sessions_metadata_table,
            _result("completed", run_id=RESUME, usage={"usage": {"inputTokens": 100, "outputTokens": 7}}),
        )

        summary = await worker.run_inbox_resume(_event(row))

        assert summary["outcome"] == "finished"
        resolution = _row(row["notificationId"])["resolution"]
        assert (resolution["state"], resolution["decision"], resolution["outcome"]) == (
            "answered", "approved", "finished",
        )
        assert _row(row["notificationId"]).get("readAt")
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert (run.status, run.resolution, run.resume_outcome, run.resume_run_id) == (
            "needs_user", "approved", "finished", RESUME,
        )
        # The resume's tokens are added to the paused run's; the row no longer expires.
        assert (run.input_tokens, run.output_tokens) == (2100, 47)
        assert run.expires_at is None
        assert _session_row(sessions_metadata_table).get("unread") is True
        # A resume never moves the schedule.
        record = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert (record.state, record.last_run_session_id) == ("active", SESSION)

    async def test_a_finished_watch_run_saves_its_cursors_and_ends_the_hold(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table, mode="watch")
        assert get_watch_state(USER_ID, schedule.schedule_id).holding()
        _claim(row)
        report = _report(
            outcome="notify",
            items=[{"key": "k2", "title": "New thing", "urgency": "normal"}],
            state={"cursors": {"since": "b"}},
        )
        _resumed(monkeypatch, sessions_metadata_table, _result("completed", run_id=RESUME, trace=[report]))

        await worker.run_inbox_resume(_event(row))

        state = get_watch_state(USER_ID, schedule.schedule_id)
        assert state.cursors == {"since": "b"}
        assert "k2" in state.seen and "k1" in state.seen
        assert state.paused_session_id is None and not state.holding()
        # No agent_item rows after a resume (settled question 3); the conversation is kept.
        assert _inbox("agent_item") == []
        assert _session_row(sessions_metadata_table)["status"] == "active"

    async def test_waiting_again(self, inbox, sessions_metadata_table, monkeypatch):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table, mode="watch")
        _claim(row)
        second = {**QUESTION, "interruptId": "int-9"}
        _resumed(
            monkeypatch, sessions_metadata_table,
            _result("needs_user", run_id=RESUME, pending_interrupt_ids=["int-9"]),
            during=lambda: _pause(sessions_metadata_table, second, expires_in_days=6),
        )

        summary = await worker.run_inbox_resume(_event(row))

        assert summary["outcome"] == "waiting_again"
        assert _row(row["notificationId"])["resolution"]["outcome"] == "waiting_again"
        (again,) = _inbox("agent_needs_input")
        assert again["payload"]["runId"] == "run-abc"
        assert again["payload"]["prompts"][0]["interruptId"] == "int-9"
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert (run.resume_outcome, run.prompt_count) == ("waiting_again", 1)
        assert run.expires_at == again["payload"]["expiresAt"]
        state = get_watch_state(USER_ID, schedule.schedule_id)
        assert state.paused_until == run.expires_at and state.holding()

    @pytest.mark.parametrize(
        ("reason", "state"), [("unknown_interrupt", "answered_elsewhere"), ("expired", "expired")]
    )
    async def test_the_runtime_refused_the_resume(self, inbox, sessions_metadata_table, monkeypatch, reason, state):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        _stub(
            monkeypatch,
            _result("error", run_id=RESUME, error="gone", error_kind="resume_unavailable", error_reason=reason),
            create_session=False,
        )

        await worker.run_inbox_resume(_event(row))

        assert _row(row["notificationId"])["resolution"]["state"] == state
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.resolution is None

    @pytest.mark.parametrize(
        "overrides",
        [{"http_status": 409, "error": "HTTP 409: busy"}, {"error": "transport: ConnectError: no route"}],
    )
    async def test_nothing_resumed_gives_the_buttons_back(self, inbox, sessions_metadata_table, monkeypatch, overrides):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        _stub(monkeypatch, _result("error", run_id=RESUME, **overrides), create_session=False)

        await worker.run_inbox_resume(_event(row))

        stored = _row(row["notificationId"])
        assert stored["resolution"]["state"] == "retry" and "readAt" not in stored
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.resolution is None

    @pytest.mark.parametrize("status", ["error", "timeout"])
    async def test_failed(self, inbox, sessions_metadata_table, monkeypatch, status):
        schedule, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        _resumed(monkeypatch, sessions_metadata_table, _result(status, run_id=RESUME, error="model failed"))

        await worker.run_inbox_resume(_event(row))

        assert _row(row["notificationId"])["resolution"]["outcome"] == "failed"
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.resume_outcome == "failed"
        record = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert record.consecutive_failures == 0 and record.state == "active"

    async def test_a_consent_on_resume_waits_again_on_a_row_naming_it(
        self, inbox, sessions_metadata_table, monkeypatch
    ):
        _, row = await _paused(monkeypatch, sessions_metadata_table)
        _claim(row)
        consent = {"interruptId": "int-o", "kind": "oauth", "providerId": "github", "createdAt": "2026-11-26T00:00:00Z"}
        _resumed(
            monkeypatch, sessions_metadata_table,
            _result(
                "oauth_required", run_id=RESUME,
                oauth_required=[OAuthConsentRequired(provider_id="github", authorization_url="https://x")],
            ),
            during=lambda: _pause(sessions_metadata_table, consent),
        )

        await worker.run_inbox_resume(_event(row))

        assert _row(row["notificationId"])["resolution"]["outcome"] == "waiting_again"
        (again,) = _inbox("agent_needs_input")
        assert again["payload"]["answerable"] is False
        assert again["payload"]["prompts"] == [
            {"interruptId": "int-o", "kind": "oauth", "serverName": "github", "headline": "Connect github to continue"}
        ]

    async def test_the_answers_are_never_logged(self, inbox, sessions_metadata_table, monkeypatch, caplog):
        _, row = await _paused(monkeypatch, sessions_metadata_table, QUESTION)
        _claim(row, decision="answered")
        _resumed(monkeypatch, sessions_metadata_table, _result("completed", run_id=RESUME))
        answer = {"answers": {"Calendar": {"selected": ["Work-secret-label"]}}}
        with caplog.at_level("DEBUG"):
            await worker.run_inbox_resume(_event(row, responses=[{"interruptId": "int-2", "response": answer}]))
        assert "Work-secret-label" not in caplog.text
        assert "Work-secret-label" not in json.dumps(_row(row["notificationId"]), default=str)
