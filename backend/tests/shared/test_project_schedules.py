"""Schedules that run in a project (Shared Projects 3.2): the shared layer.

A project schedule runs only while its project is active and its creator is
still an editor or the owner. These tests hold that rule (``run_block_reason``),
the pauses the project service makes the moment it stops holding (member
removed, left, demoted, project archived), the restore that undoes an archive
pause, and the purge that takes a deleted project's schedules with it. Real
moto tables throughout, so the pointers, the schedules in their creators'
partitions and the inbox rows are all the shapes production writes.
"""

from __future__ import annotations

import asyncio
from typing import List

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.auth.models import User
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.schedules import (
    REASON_MEMBER_REMOVED,
    REASON_NOT_EDITOR,
    REASON_PROJECT_ARCHIVED,
    REASON_PROJECT_DELETED,
    REASON_PROJECTS_DISABLED,
    ProjectSchedules,
    run_block_reason,
)
from apis.shared.projects.service import ProjectService
from apis.shared.scheduled_prompts.service import create_scheduled_prompt, get_scheduled_prompt, set_schedule_state

REGION = "us-east-1"
TABLE = "test-projects"

OWNER = User(user_id="u-owner", email="owner@example.edu", name="Olive Owner", roles=[])
EDITOR = User(user_id="u-editor", email="editor@example.edu", name="Ed Editor", roles=[])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="Vi Viewer", roles=[])


class FakeHarness:
    async def create(self, *, project_id, owner_id, owner_name, name, description) -> str:
        return f"ast-{project_id[-6:]}"

    async def delete(self, agent_id: str) -> None:
        return None

    async def rename(self, agent_id: str, *, name: str, description: str) -> None:
        return None


class NoMemory:
    """Project memory switched off: no shared space is made, and a purge has none to drop."""

    enabled = False

    def purge_space(self, space_id: str) -> None:
        return None


class NoDirectory:
    def find_by_emails(self, emails):
        return {}

    def find_by_user_ids(self, user_ids):
        return {}


def _make_projects_table() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "OwnerIndex",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "MemberIndex",
                "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )


@pytest.fixture()
def service(sessions_metadata_table, monkeypatch) -> ProjectService:
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    _make_projects_table()
    repo = ProjectRepository(table_name=TABLE)
    return ProjectService(repository=repo, harness=FakeHarness(), memory=NoMemory(), directory=NoDirectory())


def _project(service: ProjectService):
    project = asyncio.run(service.create_project(OWNER, "Enrollment Sync"))
    service.add_members(project.project_id, OWNER, [EDITOR.email], "editor")
    service.add_members(project.project_id, OWNER, [VIEWER.email], "viewer")
    return service.repository.get_project(project.project_id)


def _schedule(service: ProjectService, project, user: User = EDITOR, label: str = "Weekly status"):
    schedule = asyncio.run(
        create_scheduled_prompt(
            user_id=user.user_id,
            label=label,
            prompt_text="Summarize this week's open escalations",
            cadence="weekly",
            weekday=0,
            hour_local=9,
            timezone_name="America/Boise",
            assistant_id=project.harness_agent_id,
            project_id=project.project_id,
            owner_email=user.email,
        )
    )
    service.schedules.add_pointer(schedule)
    return schedule


def _state(schedule):
    fresh = asyncio.run(get_scheduled_prompt(schedule.user_id, schedule.schedule_id))
    return (fresh.state, fresh.state_reason) if fresh else None


def _inbox(email: str) -> List[dict]:
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    items = table.query(KeyConditionExpression=Key("PK").eq(f"INBOX#{email}"))["Items"]
    return [i for i in items if i.get("kind") == "project_schedule_paused"]


class TestRunBlockReason:
    def test_an_editor_in_an_active_project_may_run(self, service):
        project = _project(service)
        assert run_block_reason(_schedule(service, project), service.repository) is None

    def test_the_owner_may_run(self, service):
        project = _project(service)
        assert run_block_reason(_schedule(service, project, OWNER), service.repository) is None

    def test_each_reason_it_may_not(self, service, monkeypatch):
        project = _project(service)
        schedule = _schedule(service, project)

        service.repository.update_member_role(project.project_id, EDITOR.email, "viewer", project.created_at)
        assert run_block_reason(schedule, service.repository) == REASON_NOT_EDITOR

        service.repository.remove_member(project.project_id, EDITOR.email, project.created_at)
        assert run_block_reason(schedule, service.repository) == REASON_MEMBER_REMOVED

        owner_schedule = _schedule(service, project, OWNER)
        current = service.repository.get_project(project.project_id)
        service.repository.put_project(current.model_copy(update={"status": "archived"}), expected_version=current.version)
        assert run_block_reason(owner_schedule, service.repository) == REASON_PROJECT_ARCHIVED

        monkeypatch.setenv("PROJECTS_ENABLED", "false")
        assert run_block_reason(owner_schedule, service.repository) == REASON_PROJECTS_DISABLED

    def test_a_deleted_project(self, service):
        project = _project(service)
        schedule = _schedule(service, project)
        service.repository.delete_project_rows(project.project_id)
        assert run_block_reason(schedule, service.repository) == REASON_PROJECT_DELETED

    def test_a_personal_schedule_is_never_blocked(self, service):
        schedule = asyncio.run(
            create_scheduled_prompt(
                user_id=EDITOR.user_id, label="Mine", prompt_text="Hi", cadence="daily",
                hour_local=9, timezone_name="America/Boise",
            )
        )
        assert run_block_reason(schedule, service.repository) is None


class TestPause:
    def test_pausing_tells_the_creator_and_the_owner_and_leaves_a_run_row(self, service):
        project = _project(service)
        schedule = _schedule(service, project)

        assert service.schedules.pause(schedule, REASON_NOT_EDITOR) is True

        assert _state(schedule) == ("paused_error", REASON_NOT_EDITOR)
        for email in (EDITOR.email, OWNER.email):
            notes = _inbox(email)
            assert len(notes) == 1
            assert notes[0]["payload"] == {
                "scheduleId": schedule.schedule_id, "label": "Weekly status", "reason": REASON_NOT_EDITOR,
                "createdBy": EDITOR.email,
            }
        runs = service.schedules.runs(project.project_id, schedule.schedule_id)
        assert [(r.status, r.reason) for r in runs] == [("paused", REASON_NOT_EDITOR)]

    def test_a_schedule_its_creator_paused_is_left_alone(self, service):
        project = _project(service)
        schedule = _schedule(service, project)
        asyncio.run(set_schedule_state(schedule.user_id, schedule.schedule_id, "paused", state_reason="Paused by user"))
        paused = asyncio.run(get_scheduled_prompt(schedule.user_id, schedule.schedule_id))

        assert service.schedules.pause(paused, REASON_MEMBER_REMOVED) is False
        assert _state(schedule) == ("paused", "Paused by user")
        assert _inbox(OWNER.email) == []


class TestStandingChanges:
    def test_removing_a_member_pauses_only_their_schedules(self, service):
        project = _project(service)
        theirs = _schedule(service, project, EDITOR, "Ed's digest")
        owners = _schedule(service, project, OWNER, "Owner's digest")

        service.remove_member(project.project_id, OWNER, EDITOR.email)

        assert _state(theirs) == ("paused_error", REASON_MEMBER_REMOVED)
        assert _state(owners) == ("active", None)
        # The owner removed them, so only the creator hears about the schedule.
        assert len(_inbox(EDITOR.email)) == 1
        assert _inbox(OWNER.email) == []

    def test_leaving_pauses_your_schedules_and_tells_the_owner(self, service):
        project = _project(service)
        schedule = _schedule(service, project)

        service.leave(project.project_id, EDITOR)

        assert _state(schedule) == ("paused_error", REASON_MEMBER_REMOVED)
        assert len(_inbox(OWNER.email)) == 1
        assert _inbox(EDITOR.email) == []

    def test_demoting_to_viewer_pauses(self, service):
        project = _project(service)
        schedule = _schedule(service, project)

        service.update_member_role(project.project_id, OWNER, EDITOR.email, "viewer")

        assert _state(schedule) == ("paused_error", REASON_NOT_EDITOR)

    def test_archiving_pauses_quietly_and_restoring_resumes(self, service):
        project = _project(service)
        schedule = _schedule(service, project)

        asyncio.run(service.update_project(project.project_id, OWNER, status="archived"))
        assert _state(schedule) == ("paused_error", REASON_PROJECT_ARCHIVED)
        # Members were just told the project is archived; no second notice.
        assert _inbox(EDITOR.email) == []

        asyncio.run(service.update_project(project.project_id, OWNER, status="active"))
        assert _state(schedule) == ("active", None)

    def test_restoring_leaves_a_schedule_whose_creator_has_since_lost_editor(self, service):
        project = _project(service)
        schedule = _schedule(service, project)
        asyncio.run(service.update_project(project.project_id, OWNER, status="archived"))
        service.repository.update_member_role(project.project_id, EDITOR.email, "viewer", project.created_at)

        asyncio.run(service.update_project(project.project_id, OWNER, status="active"))

        assert _state(schedule) == ("paused_error", REASON_NOT_EDITOR)

    def test_purging_deletes_the_projects_schedules(self, service):
        project = _project(service)
        schedule = _schedule(service, project)
        asyncio.run(service.update_project(project.project_id, OWNER, status="archived"))

        asyncio.run(service.purge_project(project.project_id, OWNER))

        assert _state(schedule) is None
        assert service.repository.list_schedules(project.project_id) == []


class TestListing:
    def test_lists_live_schedules_through_pointers_oldest_first(self, service):
        project = _project(service)
        first = _schedule(service, project, EDITOR, "First")
        second = _schedule(service, project, OWNER, "Second")

        listed = ProjectSchedules(service.repository).list(project.project_id)

        assert [s.schedule_id for s in listed] == [first.schedule_id, second.schedule_id]

    def test_delete_drops_the_schedule_pointer_and_runs(self, service):
        project = _project(service)
        schedule = _schedule(service, project)
        service.schedules.record_run(schedule, "completed")

        service.schedules.delete(schedule)

        assert _state(schedule) is None
        assert service.repository.get_schedule(project.project_id, schedule.schedule_id) is None
        assert service.schedules.runs(project.project_id, schedule.schedule_id) == []
