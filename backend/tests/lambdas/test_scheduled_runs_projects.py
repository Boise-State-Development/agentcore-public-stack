"""Scheduled runs in a project (Shared Projects 3.2): the dispatcher and worker.

The dispatcher refuses a project schedule whose creator has lost their standing
and pauses it, telling the creator and the owner; the project service normally
does that first, so this is the backstop. The worker leaves a run row on the
project for its members after each run, and its pauses come with a notice.
"""

from __future__ import annotations

import asyncio

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectService
from apis.shared.scheduled_prompts.service import create_scheduled_prompt, get_scheduled_prompt
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

PAST = "2000-01-01T00:00:00Z"


@pytest.fixture()
def service(sessions_metadata_table, monkeypatch) -> ProjectService:
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_RUNS_WORKER_FUNCTION_NAME", "test-worker")
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setattr(dispatcher, "_emit_metrics", lambda counts: None)
    _make_projects_table()
    return ProjectService(
        repository=ProjectRepository(table_name=TABLE), harness=FakeHarness(), memory=NoMemory(), directory=NoDirectory()
    )


@pytest.fixture()
def invoked(monkeypatch) -> list:
    payloads: list = []
    monkeypatch.setattr(dispatcher, "_invoke_worker", payloads.append)
    return payloads


async def _due_project_schedule(service: ProjectService, sessions_metadata_table):
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
        UpdateExpression="SET nextRunAt = :past, GSI3_SK = :gsisk",
        ExpressionAttributeValues={":past": PAST, ":gsisk": f"{PAST}#{schedule.schedule_id}"},
    )
    return project, await get_scheduled_prompt(EDITOR.user_id, schedule.schedule_id)


def _notices(email: str) -> list:
    table = boto3.resource("dynamodb").Table(TABLE)
    items = table.query(KeyConditionExpression=Key("PK").eq(f"INBOX#{email}"))["Items"]
    return [i["payload"]["reason"] for i in items if i.get("kind") == "project_schedule_paused"]


class TestDispatcher:
    async def test_a_creator_in_good_standing_is_dispatched(self, service, sessions_metadata_table, invoked):
        _, schedule = await _due_project_schedule(service, sessions_metadata_table)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert invoked == [{"scheduleId": schedule.schedule_id, "userId": EDITOR.user_id}]

    async def test_a_removed_creator_is_paused_not_run(self, service, sessions_metadata_table, invoked):
        project, schedule = await _due_project_schedule(service, sessions_metadata_table)
        # Behind the project service's back, so only the dispatcher can catch it.
        service.repository.remove_member(project.project_id, EDITOR.email, "2026-10-10T00:00:00Z")

        counts = await dispatcher.dispatch_once()

        assert counts["PausedProject"] == 1
        assert invoked == []
        paused = await get_scheduled_prompt(EDITOR.user_id, schedule.schedule_id)
        assert (paused.state, paused.state_reason) == ("paused_error", "member_removed")
        assert _notices(EDITOR.email) == ["member_removed"]
        assert _notices(OWNER.email) == ["member_removed"]

    async def test_projects_switched_off_pauses_it(self, service, sessions_metadata_table, invoked, monkeypatch):
        _, schedule = await _due_project_schedule(service, sessions_metadata_table)
        monkeypatch.setenv("PROJECTS_ENABLED", "false")

        await dispatcher.dispatch_once()

        assert invoked == []
        paused = await get_scheduled_prompt(EDITOR.user_id, schedule.schedule_id)
        assert paused.state_reason == "projects_disabled"


class TestWorker:
    async def test_a_run_leaves_a_row_for_members(self, service, sessions_metadata_table, bff_sessions_table, monkeypatch):
        project, schedule = await _due_project_schedule(service, sessions_metadata_table)
        seen: dict = {}

        async def fake_run(**kwargs):
            seen.update(kwargs)
            return _result("completed")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": EDITOR.user_id})

        assert seen["rag_assistant_id"] == project.harness_agent_id
        runs = service.schedules.runs(project.project_id, schedule.schedule_id)
        assert [(r.status, r.reason) for r in runs] == [("completed", None)]

    async def test_a_failure_streak_pauses_with_a_notice(
        self, service, sessions_metadata_table, bff_sessions_table, monkeypatch
    ):
        project, schedule = await _due_project_schedule(service, sessions_metadata_table)

        async def fake_run(**kwargs):
            return _result("error", error="transport: boom")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)
        monkeypatch.setenv("SCHEDULED_RUNS_MAX_FAILURES", "1")

        result = await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": EDITOR.user_id})

        assert result["reason"] == "repeated_failures"
        runs = service.schedules.runs(project.project_id, schedule.schedule_id)
        assert sorted((r.status, r.reason) for r in runs) == [("error", None), ("paused", "repeated_failures")]
        assert _notices(OWNER.email) == ["repeated_failures"]
