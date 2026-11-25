"""A schedule the platform stops tells its owner (proactive-agent-gaps slice 0).

Before this, a personal schedule that the worker or the dispatcher paused said
nothing until its owner opened ``/schedules``. Every such pause now goes
through ``pausing.pause_schedule``: a personal schedule writes ``schedule_paused``
to its owner's inbox, and a project schedule keeps ``ProjectSchedules.pause``'s
run row and ``project_schedule_paused`` notice. That includes the runaway guard,
which used to set the state directly for both.

moto DynamoDB; only ``run_agent_headless`` (the harness's HTTP/SSE boundary) is stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import date

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.harness.auth import HeadlessAuthError
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectService
from apis.shared.scheduled_prompts.pausing import notify_schedule_paused
from apis.shared.scheduled_prompts.service import (
    create_scheduled_prompt,
    get_scheduled_prompt,
    remember_owner_email,
)
from lambdas.scheduled_runs_dispatcher import dispatcher
from lambdas.scheduled_runs_worker import worker
from tests.lambdas.test_scheduled_runs_worker import _result
from tests.shared.test_project_schedules import (
    EDITOR,
    OWNER,
    TABLE,
    FakeHarness,
    NoDirectory,
    NoMemory,
    _make_projects_table,
)

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
EMAIL = "member@example.edu"
PAST = "2000-01-01T00:00:00Z"


@pytest.fixture()
def inbox(sessions_metadata_table, monkeypatch):
    """The projects table, which holds every user's inbox."""
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_RUNS_WORKER_FUNCTION_NAME", "test-worker")
    monkeypatch.setattr(dispatcher, "_emit_metrics", lambda counts: None)
    monkeypatch.setattr(dispatcher, "_invoke_worker", lambda payload: None)
    _make_projects_table()
    return boto3.resource("dynamodb").Table(TABLE)


def _items(email: str, kind: str) -> list:
    table = boto3.resource("dynamodb").Table(TABLE)
    items = table.query(KeyConditionExpression=Key("PK").eq(f"INBOX#{email}"))["Items"]
    return [i for i in items if i.get("kind") == kind]


async def _personal_schedule(sessions_metadata_table, *, owner_email=EMAIL, due=False, max_runs_per_day=None):
    schedule = await create_scheduled_prompt(
        user_id=USER_ID,
        label="Morning Briefing",
        prompt_text="Summarize my day",
        cadence="daily",
        hour_local=9,
        timezone_name="America/Boise",
        owner_email=owner_email,
    )
    if due or max_runs_per_day is not None:
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
            UpdateExpression="SET nextRunAt = :past, GSI3_SK = :gsisk, maxRunsPerDay = :max, "
            "runsToday = :n, runsTodayDate = :today",
            ExpressionAttributeValues={
                ":past": PAST,
                ":gsisk": f"{PAST}#{schedule.schedule_id}",
                ":max": max_runs_per_day or 24,
                ":n": max_runs_per_day or 0,
                ":today": date.today().isoformat(),
            },
        )
    return await get_scheduled_prompt(USER_ID, schedule.schedule_id)


class TestWorker:
    async def test_a_failure_streak_tells_the_owner(self, inbox, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _personal_schedule(sessions_metadata_table)

        async def fake_run(**kwargs):
            return _result("error", error="transport: boom")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)
        monkeypatch.setenv("SCHEDULED_RUNS_MAX_FAILURES", "1")

        result = await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})

        assert result["reason"] == "repeated_failures"
        [notice] = _items(EMAIL, "schedule_paused")
        assert notice["payload"] == {
            "scheduleId": schedule.schedule_id,
            "label": "Morning Briefing",
            "reason": "repeated_failures",
        }
        # The owner is the recipient, so nobody is named as the actor.
        assert not notice.get("actorEmail")

    async def test_an_expired_grant_tells_the_owner_to_sign_in(
        self, inbox, sessions_metadata_table, bff_sessions_table, monkeypatch
    ):
        schedule = await _personal_schedule(sessions_metadata_table)

        async def fake_run(**kwargs):
            raise HeadlessAuthError("no active headless grant")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})

        assert [n["payload"]["reason"] for n in _items(EMAIL, "schedule_paused")] == ["reauth_required"]

    async def test_a_single_failure_says_nothing(self, inbox, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _personal_schedule(sessions_metadata_table)

        async def fake_run(**kwargs):
            return _result("error", error="transport: boom")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})

        assert _items(EMAIL, "schedule_paused") == []

    async def test_no_email_on_record_still_pauses(self, inbox, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _personal_schedule(sessions_metadata_table, owner_email=None)

        async def fake_run(**kwargs):
            raise HeadlessAuthError("no active headless grant")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        result = await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})

        assert result["result"] == "paused_error"
        paused = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert (paused.state, paused.state_reason) == ("paused_error", "reauth_required")


class TestRunawayGuard:
    async def test_a_personal_schedule_tells_its_owner(self, inbox, sessions_metadata_table):
        schedule = await _personal_schedule(sessions_metadata_table, max_runs_per_day=3)

        counts = await dispatcher.dispatch_once()

        assert counts["PausedRunaway"] == 1
        paused = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert paused.state_reason == "max_runs_per_day_exceeded"
        assert [n["payload"]["reason"] for n in _items(EMAIL, "schedule_paused")] == ["max_runs_per_day_exceeded"]

    async def test_a_project_schedule_gets_a_run_row_and_a_project_notice(self, inbox, sessions_metadata_table):
        service = ProjectService(
            repository=ProjectRepository(table_name=TABLE),
            harness=FakeHarness(),
            memory=NoMemory(),
            directory=NoDirectory(),
        )
        project = await service.create_project(OWNER, "Enrollment Sync")
        await asyncio.to_thread(service.add_members, project.project_id, OWNER, [EDITOR.email], "editor")
        schedule = await create_scheduled_prompt(
            user_id=EDITOR.user_id,
            label="Weekly status",
            prompt_text="Summarize the week",
            cadence="daily",
            hour_local=9,
            timezone_name="America/Boise",
            assistant_id=project.harness_agent_id,
            project_id=project.project_id,
            owner_email=EDITOR.email,
        )
        await asyncio.to_thread(service.schedules.add_pointer, schedule)
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{EDITOR.user_id}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
            UpdateExpression="SET nextRunAt = :past, GSI3_SK = :gsisk, maxRunsPerDay = :max, "
            "runsToday = :n, runsTodayDate = :today",
            ExpressionAttributeValues={
                ":past": PAST,
                ":gsisk": f"{PAST}#{schedule.schedule_id}",
                ":max": 2,
                ":n": 2,
                ":today": date.today().isoformat(),
            },
        )

        counts = await dispatcher.dispatch_once()

        assert counts["PausedRunaway"] == 1
        runs = service.schedules.runs(project.project_id, schedule.schedule_id)
        assert [(r.status, r.reason) for r in runs] == [("paused", "max_runs_per_day_exceeded")]
        assert [n["payload"]["reason"] for n in _items(OWNER.email, "project_schedule_paused")] == [
            "max_runs_per_day_exceeded"
        ]
        # A project schedule's notice is the project's kind, never the personal one.
        assert _items(EDITOR.email, "schedule_paused") == []


class TestOwnerEmail:
    async def test_recorded_once_and_never_overwritten(self, inbox, sessions_metadata_table):
        schedule = await _personal_schedule(sessions_metadata_table, owner_email=None)

        assert remember_owner_email(USER_ID, schedule.schedule_id, EMAIL) is True
        assert remember_owner_email(USER_ID, schedule.schedule_id, "someone-else@example.edu") is False

        assert (await get_scheduled_prompt(USER_ID, schedule.schedule_id)).owner_email == EMAIL

    async def test_a_missing_schedule_is_not_created(self, inbox, sessions_metadata_table):
        assert remember_owner_email(USER_ID, "sched-000000000000", EMAIL) is False
        assert await get_scheduled_prompt(USER_ID, "sched-000000000000") is None


class TestNotifyNeverRaises:
    async def test_a_failing_inbox_write_is_swallowed(self, sessions_metadata_table):
        schedule = await _personal_schedule(sessions_metadata_table)

        class Exploding:
            def notify_own(self, **kwargs):
                raise RuntimeError("inbox down")

        assert notify_schedule_paused(schedule, "repeated_failures", Exploding()) is False
