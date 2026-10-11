"""A schedule publishing its runs' artifacts to the project's Outputs (Shared Projects 3.2d).

A schedule that shares its runs and publishes its outputs also shares each
artifact a delivered run made with "Project members", exactly as 3.3's share
does (both rows and the ``OUTPUT#`` pointer), and unshares its previous run's,
so Outputs hold the latest edition. An artifact the creator shared by hand is
never touched. Real moto tables and bucket; only the two conversation reads
are stubbed (by the delivery tests' helper).
"""

from __future__ import annotations

import boto3
import pytest

from apis.shared.projects.schedule_delivery import ScheduleDelivery
from apis.shared.shares.artifact_shares import output_pointer, shares_for_artifact, write_share_rows
from apis.shared.shares.snapshot_store import ShareSnapshotStore
from tests.shared.test_project_schedule_delivery import (  # noqa: F401 (fixtures)
    AUTHOR,
    BUCKET,
    REGION,
    SHARES_TABLE,
    RecordingAudit,
    RecordingNotifications,
    _deliver,
    _schedule,
    env,
    project,
    projects,
    sessions_table,
)

ARTIFACTS = "test-artifacts"


@pytest.fixture()
def artifacts(env):
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=ARTIFACTS,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[{
            "IndexName": "SessionIndex",
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    return boto3.resource("dynamodb", region_name=REGION).Table(ARTIFACTS)


@pytest.fixture()
def audit():
    return RecordingAudit()


@pytest.fixture()
def delivery(projects, artifacts, audit):
    return ScheduleDelivery(
        repository=projects.repository,
        table=boto3.resource("dynamodb", region_name=REGION).Table(SHARES_TABLE),
        snapshot_store=ShareSnapshotStore(bucket_name=BUCKET, s3_client=boto3.client("s3", region_name=REGION)),
        artifacts_table=artifacts,
        notifications=RecordingNotifications(),
        audit=audit,
    )


def _make(artifacts, session_id: str, artifact_id: str, title: str, version: int = 1, user_id: str = AUTHOR.user_id) -> None:
    """An artifact's HEAD row, as the writer leaves it (only HEAD rows carry the session index keys)."""
    artifacts.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": f"ARTIFACT#{artifact_id}#HEAD",
        "GSI1PK": f"SESSION#{session_id}",
        "GSI1SK": f"2026-10-0{version}T00:00:00Z#{artifact_id}",
        "artifact_id": artifact_id,
        "user_id": user_id,
        "version": version,
        "title": title,
        "content_type": "text/markdown",
        "session_id": session_id,
    })


def _outputs(projects, project) -> dict:
    return {o.artifact_id: o for o in projects.repository.list_outputs(project.project_id)}


def _shares(artifacts, artifact_id: str) -> list:
    return shares_for_artifact(artifacts, AUTHOR.user_id, artifact_id)


def test_a_delivered_run_publishes_its_artifacts_as_project_outputs(delivery, project, projects, artifacts, audit):
    schedule = _schedule(project, publish_outputs=True)
    _make(artifacts, "headless-run1", "art-report", "Weekly report", version=2)

    assert _deliver(delivery, schedule, "headless-run1", project.project_id)

    [share] = _shares(artifacts, "art-report")
    assert (share["access_level"], share["project_id"], share["schedule_id"], share["version"]) == (
        "project", project.project_id, schedule.schedule_id, 2,
    )
    assert share["owner_id"] == AUTHOR.user_id
    lookup = artifacts.get_item(Key={"PK": f"SHARE#{share['share_id']}", "SK": "META"})["Item"]
    assert lookup["project_id"] == project.project_id
    output = _outputs(projects, project)["art-report"]
    assert (output.share_id, output.title, output.schedule_id) == (share["share_id"], "Weekly report", schedule.schedule_id)
    [record] = [r for r in audit.records if r["action"] == "project.output_shared"]
    assert record["reason"] == "schedule"
    assert record["actor"].email == AUTHOR.email
    assert record["after"] == {"artifactId": "art-report", "version": 2, "title": "Weekly report", "scheduleId": schedule.schedule_id}


def test_each_run_replaces_the_previous_runs_outputs(delivery, project, projects, artifacts):
    schedule = _schedule(project, publish_outputs=True)
    _make(artifacts, "headless-run1", "art-week1", "Week 1")
    _deliver(delivery, schedule, "headless-run1", project.project_id)
    _make(artifacts, "headless-run2", "art-week2", "Week 2")

    _deliver(delivery, schedule, "headless-run2", project.project_id)

    assert set(_outputs(projects, project)) == {"art-week2"}
    assert _shares(artifacts, "art-week1") == []
    assert len(_shares(artifacts, "art-week2")) == 1


def test_a_run_without_artifacts_leaves_the_last_edition(delivery, project, projects, artifacts):
    schedule = _schedule(project, publish_outputs=True)
    _make(artifacts, "headless-run1", "art-week1", "Week 1")
    _deliver(delivery, schedule, "headless-run1", project.project_id)

    _deliver(delivery, schedule, "headless-run2", project.project_id)

    assert set(_outputs(projects, project)) == {"art-week1"}


def test_a_hand_share_of_an_earlier_edition_survives(delivery, project, projects, artifacts):
    schedule = _schedule(project, publish_outputs=True)
    _make(artifacts, "headless-run1", "art-week1", "Week 1")
    _deliver(delivery, schedule, "headless-run1", project.project_id)
    # The creator shares week 1 with the project themselves, before the schedule's share.
    hand = {
        "share_id": "hand-1", "artifact_id": "art-week1", "version": 1, "owner_id": AUTHOR.user_id,
        "owner_email": AUTHOR.email, "access_level": "project", "title": "Week 1", "content_type": "text/markdown",
        "session_id": "headless-run1", "project_id": project.project_id, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    write_share_rows(artifacts, hand)
    _make(artifacts, "headless-run2", "art-week2", "Week 2")

    _deliver(delivery, schedule, "headless-run2", project.project_id)

    assert [s["share_id"] for s in _shares(artifacts, "art-week1")] == ["hand-1"]
    outputs = _outputs(projects, project)
    assert set(outputs) == {"art-week1", "art-week2"}
    assert (outputs["art-week1"].share_id, outputs["art-week1"].schedule_id) == ("hand-1", None)


def test_other_schedules_outputs_are_left_alone(delivery, project, projects, artifacts):
    first = _schedule(project, publish_outputs=True, label="Digest")
    second = _schedule(project, publish_outputs=True, label="Roster")
    _make(artifacts, "headless-a1", "art-digest", "Digest")
    _deliver(delivery, first, "headless-a1", project.project_id)
    _make(artifacts, "headless-b1", "art-roster", "Roster")

    _deliver(delivery, second, "headless-b1", project.project_id)

    assert set(_outputs(projects, project)) == {"art-digest", "art-roster"}


@pytest.mark.parametrize("overrides", [{"publish_outputs": False}, {"publish_outputs": True, "deliver_to": "owner"}])
def test_nothing_is_published_without_the_option_or_without_sharing(delivery, project, projects, artifacts, overrides):
    schedule = _schedule(project, **overrides)
    _make(artifacts, "headless-run1", "art-report", "Weekly report")

    _deliver(delivery, schedule, "headless-run1", project.project_id)

    assert _outputs(projects, project) == {}
    assert _shares(artifacts, "art-report") == []


def test_a_pointer_that_fails_unshares_the_artifact(delivery, project, projects, artifacts, monkeypatch):
    schedule = _schedule(project, publish_outputs=True)
    _make(artifacts, "headless-run1", "art-report", "Weekly report")

    def refuse(_output):
        raise RuntimeError("throttled")

    monkeypatch.setattr(projects.repository, "put_output", refuse)

    # The run's task is still delivered; only the output is undone.
    assert _deliver(delivery, schedule, "headless-run1", project.project_id)
    assert _shares(artifacts, "art-report") == []
    lookups = artifacts.scan()["Items"]
    assert not [r for r in lookups if str(r["PK"]).startswith("SHARE#")]


def test_the_pointer_carries_the_schedule_only_when_one_published_it():
    share = {"project_id": "p", "artifact_id": "a", "share_id": "s", "version": 3, "owner_id": "u", "owner_email": "e", "created_at": "t"}
    assert output_pointer({**share, "schedule_id": "sched-1"}).schedule_id == "sched-1"
    assert output_pointer(share).schedule_id is None
