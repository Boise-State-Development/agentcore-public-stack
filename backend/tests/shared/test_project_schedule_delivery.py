"""Delivering a scheduled run to its project (Shared Projects 3.2b).

A schedule set to deliver to its project shares each completed run's task with
it, as its creator would by hand: the same row, body and ``SHARED_TASK#`` pointer
(the shared writer), noted "Scheduled: <label>" and tagged with the schedule. It
keeps only its newest few deliveries shared, and never unshares anything else.
Real moto tables and bucket; only the two conversation reads are stubbed.
"""

from __future__ import annotations

import asyncio
import json
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest

from apis.shared.projects.schedule_delivery import ScheduleDelivery
from apis.shared.scheduled_prompts.service import create_scheduled_prompt
from apis.shared.shares.snapshot_store import ShareSnapshotStore
from tests.apis.app_api.shares.test_project_shares import (  # noqa: F401 (fixtures)
    AUTHOR,
    BUCKET,
    OWNER,
    REGION,
    SHARES_TABLE,
    VIEWER,
    _session,
    env,
    project,
    projects,
)

SESSIONS = "test-sessions-metadata"


@pytest.fixture(autouse=True)
def sessions_table(env, monkeypatch):
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", SESSIONS)
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SESSIONS,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
        BillingMode="PAY_PER_REQUEST",
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.records: List[dict] = []

    def record(self, **record) -> None:
        self.records.append(record)


class RecordingNotifications:
    def __init__(self) -> None:
        self.sent: List[dict] = []

    def notify_many(self, recipients, **kwargs) -> int:
        recipients = sorted(set(recipients) - {kwargs["actor"].email})
        self.sent.append({"recipients": recipients, **kwargs})
        return len(recipients)


class NoArtifacts:
    def query(self, **kwargs):
        return {"Items": []}


@pytest.fixture()
def delivery(projects):
    return ScheduleDelivery(
        repository=projects.repository,
        table=boto3.resource("dynamodb", region_name=REGION).Table(SHARES_TABLE),
        snapshot_store=ShareSnapshotStore(bucket_name=BUCKET, s3_client=boto3.client("s3", region_name=REGION)),
        artifacts_table=NoArtifacts(),
        notifications=RecordingNotifications(),
        audit=RecordingAudit(),
    )


def _schedule(project, **overrides):
    kwargs = dict(
        user_id=AUTHOR.user_id,
        label="Weekly status",
        prompt_text="Summarize the week",
        cadence="daily",
        hour_local=9,
        timezone_name="America/Boise",
        assistant_id=project.harness_agent_id,
        project_id=project.project_id,
        owner_email=AUTHOR.email,
        deliver_to="project",
    )
    kwargs.update(overrides)
    return asyncio.run(create_scheduled_prompt(**kwargs))


def _deliver(delivery, schedule, session_id: str, project_id):
    message = MagicMock()
    message.model_dump.return_value = {"role": "assistant", "content": [{"text": "All quiet this week."}]}
    with patch(
        "apis.shared.sessions.metadata.get_session_metadata",
        new=AsyncMock(return_value=_session(session_id, project_id, title="Weekly status")),
    ), patch(
        "apis.shared.sessions.messages.get_messages",
        new=AsyncMock(return_value=MagicMock(messages=[message], tool_summaries=[{"batchId": "b1", "summary": "Read 2 tasks"}])),
    ):
        return delivery.deliver(schedule, session_id)


def _row(share_id: str) -> dict:
    return boto3.resource("dynamodb", region_name=REGION).Table(SHARES_TABLE).get_item(Key={"share_id": share_id}).get("Item")


def _bodies() -> List[str]:
    listed = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET)
    return [o["Key"] for o in listed.get("Contents", [])]


def test_a_run_is_shared_with_the_project_as_its_creator(delivery, project, projects):
    schedule = _schedule(project)

    share_id = _deliver(delivery, schedule, "headless-run1", project.project_id)

    row = _row(share_id)
    assert row["access_level"] == "project"
    assert row["project_id"] == project.project_id
    assert row["owner_id"] == AUTHOR.user_id
    assert row["note"] == "Scheduled: Weekly status"
    assert row["schedule_id"] == schedule.schedule_id
    body = json.loads(
        boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key=row["body_ref"]["bucket_key"])["Body"].read()
    )
    assert body["messages"][0]["content"][0]["text"] == "All quiet this week."
    assert body["toolSummaries"] == [{"batchId": "b1", "summary": "Read 2 tasks"}]
    [pointer] = projects.repository.list_shared_tasks(project.project_id)
    assert (pointer.share_id, pointer.schedule_id, pointer.note) == (share_id, schedule.schedule_id, "Scheduled: Weekly status")
    [record] = delivery._audit.records
    assert record["action"] == "project.task_shared" and record["reason"] == "schedule"
    assert record["after"]["scheduleId"] == schedule.schedule_id
    # notifyMembers is off by default.
    assert delivery._notifications.sent == []


def test_notify_members_tells_everyone_but_the_creator(delivery, project):
    schedule = _schedule(project, notify_members=True)

    share_id = _deliver(delivery, schedule, "headless-run1", project.project_id)

    [sent] = delivery._notifications.sent
    assert sent["kind"] == "project_task_shared"
    assert sent["recipients"] == sorted([OWNER.email, VIEWER.email])
    assert sent["payload"]["shareId"] == share_id


@pytest.mark.parametrize(
    "case",
    ["owner_delivery", "creator_demoted", "run_outside_the_project"],
)
def test_runs_that_are_not_shared(delivery, project, projects, case):
    schedule = _schedule(project, deliver_to="owner" if case == "owner_delivery" else "project")
    if case == "creator_demoted":
        projects.repository.update_member_role(project.project_id, AUTHOR.email, "viewer", "2026-10-10T00:00:00Z")
    session_project = None if case == "run_outside_the_project" else project.project_id

    assert _deliver(delivery, schedule, "headless-run1", session_project) is None
    assert projects.repository.list_shared_tasks(project.project_id) == []
    assert _bodies() == []


def test_a_schedule_keeps_only_its_newest_deliveries(delivery, project, projects, monkeypatch):
    monkeypatch.setenv("PROJECTS_SCHEDULE_RUNS_KEPT", "2")
    schedule = _schedule(project)
    # The creator's own share of another task, which pruning must never touch.
    hand = projects.repository
    from apis.shared.projects.models import SharedTask

    hand.put_shared_task(SharedTask(
        project_id=project.project_id, session_id="s-hand", share_id="sh-hand", owner_id=AUTHOR.user_id,
        owner_email=AUTHOR.email, title="By hand", shared_at="2000-01-01T00:00:00Z",
    ))

    first = _deliver(delivery, schedule, "headless-run1", project.project_id)
    second = _deliver(delivery, schedule, "headless-run2", project.project_id)
    third = _deliver(delivery, schedule, "headless-run3", project.project_id)

    listed = {t.share_id for t in projects.repository.list_shared_tasks(project.project_id)}
    assert listed == {second, third, "sh-hand"}
    assert _row(first) is None
    assert len(_bodies()) == 2


def test_a_failure_leaves_the_run_unshared_and_never_raises(delivery, project, projects):
    schedule = _schedule(project)
    projects.repository.put_shared_task = MagicMock(side_effect=RuntimeError("boom"))

    assert _deliver(delivery, schedule, "headless-run1", project.project_id) is None
    # The writer rolled the row and body back with the failed pointer.
    assert _bodies() == []
