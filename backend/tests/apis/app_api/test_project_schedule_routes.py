"""``/projects/{id}/schedules`` (Shared Projects 3.2).

The authorization matrix, then the rules that make a project schedule different
from a personal one: editors create, every member sees, only the creator changes
or resumes (it runs as them), editors may pause or delete anyone's and the creator
hears about it, and the personal ``/schedules`` surface sends edits back here.
"""

from __future__ import annotations

import asyncio

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from boto3.dynamodb.conditions import Key

import apis.app_api.projects.schedule_routes as schedule_routes
from apis.app_api.schedules.routes import router as personal_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from tests.apis.app_api.test_projects_routes import (  # noqa: F401 (fixtures)
    EDITOR,
    OTHER,
    OWNER,
    PRINCIPALS,
    STRANGER,
    TABLE,
    VIEWER,
    directory,
    env,
    project_id,
    service,
    task_queries,
)

SESSIONS = "test-sessions-metadata"

SCHEDULE = {
    "label": "Weekly status",
    "promptText": "Summarize this week's open escalations",
    "cadence": "weekly",
    "weekday": 0,
    "hourLocal": 9,
    "timezone": "America/Boise",
}


@pytest.fixture(autouse=True)
def sessions_table(env, monkeypatch):
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", SESSIONS)
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "true")
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=SESSIONS,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI3_PK", "GSI3_SK")],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "DueScheduleIndex",
                "KeySchema": [{"AttributeName": "GSI3_PK", "KeyType": "HASH"}, {"AttributeName": "GSI3_SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


def client_for(user: User) -> TestClient:
    app = FastAPI()
    app.include_router(schedule_routes.router)
    app.include_router(personal_router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _create(project_id: str, user: User = EDITOR, **overrides) -> dict:
    response = client_for(user).post(f"/projects/{project_id}/schedules", json={**SCHEDULE, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def _inbox(email: str) -> list:
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
    items = table.query(KeyConditionExpression=Key("PK").eq(f"INBOX#{email}"))["Items"]
    return [i for i in items if i.get("kind") == "project_schedule_paused"]


class RecordingAudit:
    """Keeps every record a route makes; the audit table itself is covered in test_projects_trail."""

    configured = False

    def __init__(self) -> None:
        self.records: list = []

    def record(self, **record) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def audit(service) -> RecordingAudit:
    service.audit = RecordingAudit()
    return service.audit


def _audit_actions(service, project_id: str) -> list:
    return [r["action"] for r in service.audit.records if r["target_id"] == project_id]


@pytest.mark.parametrize(
    "principal,expected",
    [("owner", 201), ("editor", 201), ("viewer", 403), ("stranger", 404), ("other_project_member", 404)],
)
def test_who_can_create(project_id, principal, expected):
    response = client_for(PRINCIPALS[principal]).post(f"/projects/{project_id}/schedules", json=SCHEDULE)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    "principal,expected",
    [("owner", 200), ("editor", 200), ("viewer", 200), ("stranger", 404), ("other_project_member", 404)],
)
def test_who_can_see(project_id, principal, expected):
    _create(project_id)
    response = client_for(PRINCIPALS[principal]).get(f"/projects/{project_id}/schedules")
    assert response.status_code == expected, response.text
    if expected == 200:
        assert [s["label"] for s in response.json()["schedules"]] == ["Weekly status"]


def test_it_runs_on_the_projects_assistant_as_its_creator(project_id, service):
    created = _create(project_id)

    mine = client_for(EDITOR).get("/schedules").json()["schedules"]

    assert [s["scheduleId"] for s in mine] == [created["scheduleId"]]
    assert mine[0]["projectId"] == project_id
    assert mine[0]["assistantId"] == service.repository.get_project(project_id).harness_agent_id
    assert mine[0]["enabledTools"] is None
    assert created["isMine"] and created["canEdit"] and created["canManage"]
    assert created["createdByEmail"] == EDITOR.email
    assert "project.schedule_created" in _audit_actions(service, project_id)


def test_an_agent_or_tools_cannot_be_named(project_id):
    response = client_for(EDITOR).post(f"/projects/{project_id}/schedules", json={**SCHEDULE, "assistantId": "ast-x"})
    assert response.status_code == 422


def test_a_viewer_sees_it_but_not_the_creators_task(project_id, service, sessions_table):
    created = _create(project_id)
    boto3.resource("dynamodb", region_name="us-east-1").Table(SESSIONS).update_item(
        Key={"PK": f"USER#{EDITOR.user_id}", "SK": f"SCHEDPROMPT#{created['scheduleId']}"},
        UpdateExpression="SET lastRunSessionId = :s",
        ExpressionAttributeValues={":s": "headless-abc"},
    )

    seen = client_for(VIEWER).get(f"/projects/{project_id}/schedules/{created['scheduleId']}").json()
    own = client_for(EDITOR).get(f"/projects/{project_id}/schedules/{created['scheduleId']}").json()

    assert seen["lastRunSessionId"] is None
    assert not seen["isMine"] and not seen["canEdit"] and not seen["canManage"]
    assert own["lastRunSessionId"] == "headless-abc"


def test_only_the_creator_changes_it(project_id, service):
    created = _create(project_id)
    path = f"/projects/{project_id}/schedules/{created['scheduleId']}"

    assert client_for(OWNER).patch(path, json={"label": "Mine now"}).status_code == 403
    response = client_for(EDITOR).patch(path, json={"label": "Weekly escalations"})

    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Weekly escalations"
    assert "project.schedule_updated" in _audit_actions(service, project_id)


def test_an_editor_pauses_someone_elses_and_the_creator_is_told(project_id, service):
    created = _create(project_id, OWNER)
    path = f"/projects/{project_id}/schedules/{created['scheduleId']}"

    assert client_for(VIEWER).post(f"{path}/pause").status_code == 403
    response = client_for(EDITOR).post(f"{path}/pause")

    assert response.status_code == 200
    assert (response.json()["state"], response.json()["stateReason"]) == ("paused_error", "paused_by_editor")
    assert [n["payload"]["reason"] for n in _inbox(OWNER.email)] == ["paused_by_editor"]
    # Only the creator may set it running again: it runs as them.
    assert client_for(EDITOR).post(f"{path}/resume").status_code == 403
    resumed = client_for(OWNER).post(f"{path}/resume")
    assert resumed.status_code == 200 and resumed.json()["state"] == "active"


def test_resume_is_refused_while_the_creator_could_not_run_it(project_id, service):
    created = _create(project_id)
    path = f"/projects/{project_id}/schedules/{created['scheduleId']}"
    assert client_for(EDITOR).post(f"{path}/pause").json()["state"] == "paused"

    service.repository.update_member_role(project_id, EDITOR.email, "viewer", "2026-10-10T00:00:00Z")
    response = client_for(EDITOR).post(f"{path}/resume")

    assert response.status_code == 409
    assert "editor" in response.json()["detail"]


def test_an_editor_deletes_someone_elses_and_the_creator_is_told(project_id, service):
    created = _create(project_id, OWNER)
    path = f"/projects/{project_id}/schedules/{created['scheduleId']}"

    assert client_for(VIEWER).delete(path).status_code == 403
    assert client_for(EDITOR).delete(path).status_code == 204

    assert client_for(OWNER).get(f"/projects/{project_id}/schedules").json()["schedules"] == []
    assert client_for(OWNER).get("/schedules").json()["schedules"] == []
    assert [n["payload"]["reason"] for n in _inbox(OWNER.email)] == ["deleted"]
    assert "project.schedule_deleted" in _audit_actions(service, project_id)


def test_run_history_is_for_every_member(project_id, service):
    created = _create(project_id)
    schedule = service.schedules.get(project_id, created["scheduleId"])
    service.schedules.record_run(schedule, "completed")
    service.schedules.record_run(schedule, "error")

    runs = client_for(VIEWER).get(f"/projects/{project_id}/schedules/{created['scheduleId']}/runs")

    assert runs.status_code == 200
    assert [r["status"] for r in runs.json()["runs"]] == ["error", "completed"]
    assert client_for(STRANGER).get(f"/projects/{project_id}/schedules/{created['scheduleId']}/runs").status_code == 404


def test_an_archived_project_takes_no_new_schedules(project_id, service):
    asyncio.run(service.update_project(project_id, OWNER, status="archived"))
    response = client_for(OWNER).post(f"/projects/{project_id}/schedules", json=SCHEDULE)
    assert response.status_code == 409


def test_a_schedule_in_another_project_is_not_found_here(project_id, service):
    other = asyncio.run(service.create_project(OTHER, "Elsewhere"))
    created = _create(other.project_id, OTHER)
    response = client_for(OWNER).get(f"/projects/{project_id}/schedules/{created['scheduleId']}")
    assert response.status_code == 404


def test_scheduled_runs_off_means_not_found(project_id, monkeypatch):
    monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "false")
    assert client_for(OWNER).get(f"/projects/{project_id}/schedules").status_code == 404


class TestPersonalSurface:
    def test_edits_and_resume_go_through_the_project(self, project_id):
        created = _create(project_id)
        path = f"/schedules/{created['scheduleId']}"
        editor = client_for(EDITOR)

        assert editor.patch(path, json={"label": "Sneaky"}).status_code == 409
        assert editor.post(f"{path}/pause").status_code == 200
        assert editor.post(f"{path}/resume").status_code == 409
        # A PATCH that only pauses is a pause, which stays open.
        assert editor.patch(path, json={"state": "paused"}).status_code == 200

    def test_deleting_it_there_also_takes_it_off_the_project(self, project_id, service):
        created = _create(project_id)

        assert client_for(EDITOR).delete(f"/schedules/{created['scheduleId']}").status_code == 204

        assert service.repository.list_schedules(project_id) == []
        assert "project.schedule_deleted" in _audit_actions(service, project_id)
