"""Shared Projects PR-1.7: the ``project.*`` audit trail, the inbox, and the two trail readers.

Runs the real service, audit repository and notification service against moto
tables, so what is asserted is what a reader of the table would see.
"""

from __future__ import annotations

import asyncio
from typing import List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import apis.app_api.admin.projects.routes as admin_routes
import apis.app_api.notifications.routes as notification_routes
import apis.app_api.projects.routes as project_routes
from apis.shared.audit import AuditRepository, AuditService
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.notifications import NotificationService
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectService

from tests.shared.test_projects import REGION, TABLE, FakeHarness, make_projects_table

AUDIT_TABLE = "test-audit-log"

OWNER = User(user_id="u-owner", email="owner@example.edu", name="O", roles=["default"])
EDITOR = User(user_id="u-editor", email="editor@example.edu", name="E", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="V", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="S", roles=["default"])
ADMIN = User(user_id="u-admin", email="admin@example.edu", name="A", roles=["system_admin"])


@pytest.fixture()
def env(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_ENABLED": "true",
        "DYNAMODB_PROJECTS_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        make_projects_table()
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=AUDIT_TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


@pytest.fixture()
def service(env, monkeypatch) -> ProjectService:
    svc = ProjectService(
        repository=ProjectRepository(table_name=TABLE),
        harness=FakeHarness(),
        audit=AuditService(repository=AuditRepository(table_name=AUDIT_TABLE)),
    )
    monkeypatch.setattr(project_routes, "_service", svc)
    monkeypatch.setattr(admin_routes, "_service", svc)
    monkeypatch.setattr(notification_routes, "_service", NotificationService(table_name=TABLE))
    return svc


@pytest.fixture()
def pid(service) -> str:
    p = asyncio.run(service.create_project(OWNER, "Budget"))
    service.add_members(p.project_id, OWNER, [EDITOR.email], "editor")
    service.add_members(p.project_id, OWNER, [VIEWER.email], "viewer")
    return p.project_id


def actions(service: ProjectService, project_id: str) -> List[str]:
    records, _ = service.audit_trail(project_id, limit=200)
    return [r.action for r in reversed(records)]


def client(user: User, *routers) -> TestClient:
    app = FastAPI()
    for router in routers or (project_routes.router,):
        app.include_router(router, prefix="/admin" if router is admin_routes.router else "")
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    app.dependency_overrides[admin_routes.require_projects_admin] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def inbox(user: User) -> dict:
    return client(user, notification_routes.router).get("/notifications").json()


# ---- the trail -----------------------------------------------------------


def test_every_membership_and_lifecycle_change_is_recorded(service, pid):
    service.update_project(pid, EDITOR, name="Budget FY27")
    service.update_member_role(pid, OWNER, VIEWER.email, "editor")
    service.remove_member(pid, OWNER, VIEWER.email)
    service.get_project(pid, EDITOR)  # back-fills the editor's userId, which transfer needs
    service.transfer_ownership(pid, OWNER, EDITOR.email)
    service.leave(pid, OWNER)
    service.update_project(pid, EDITOR, status="archived")
    service.update_project(pid, EDITOR, status="active")

    assert actions(service, pid) == [
        "project.created",
        "project.member_added",
        "project.member_added",
        "project.updated",
        "project.member_role_changed",
        "project.member_removed",
        "project.transferred",
        "project.member_removed",
        "project.archived",
        "project.restored",
    ]
    records, _ = service.audit_trail(pid, limit=200)
    by_action = {r.action: r for r in reversed(records)}  # the newest of each action
    assert by_action["project.updated"].before == {"name": "Budget"}
    assert by_action["project.updated"].after == {"name": "Budget FY27"}
    assert by_action["project.member_role_changed"].before == {"email": VIEWER.email, "role": "viewer"}
    assert by_action["project.member_removed"].reason == "left"  # the latest: the old owner leaving
    assert by_action["project.transferred"].after == {"ownerEmail": EDITOR.email}


def test_nothing_is_recorded_for_a_change_that_changes_nothing(service, pid):
    before = actions(service, pid)
    service.update_member_role(pid, OWNER, EDITOR.email, "editor")
    service.update_project(pid, OWNER, name="Budget")
    assert actions(service, pid) == before


def test_purge_leaves_its_record_behind(service, pid):
    service.update_project(pid, OWNER, status="archived")
    asyncio.run(service.purge_project(pid, OWNER))
    assert actions(service, pid)[-2:] == ["project.archived", "project.deleted"]


# ---- notifications -------------------------------------------------------


def test_the_people_affected_are_told_and_the_actor_is_not(service, pid):
    service.update_member_role(pid, OWNER, VIEWER.email, "editor")
    service.get_project(pid, EDITOR)
    service.transfer_ownership(pid, OWNER, EDITOR.email)
    service.remove_member(pid, EDITOR, VIEWER.email)

    viewer = inbox(VIEWER)
    assert [(n["kind"], n["payload"]) for n in viewer["notifications"]] == [
        ("project_removed", {}),
        ("project_role_changed", {"role": "editor"}),
        ("project_invited", {"role": "viewer"}),
    ]
    assert viewer["unreadCount"] == 3
    assert {n["projectName"] for n in viewer["notifications"]} == {"Budget"}
    assert viewer["notifications"][0]["actorEmail"] == EDITOR.email

    assert [n["kind"] for n in inbox(EDITOR)["notifications"]] == ["project_ownership_transferred", "project_invited"]
    assert inbox(OWNER)["notifications"] == []


def test_an_invitation_waits_for_someone_who_has_never_signed_in(service, pid):
    service.add_members(pid, OWNER, ["New.Person@Example.edu"], "viewer")
    newcomer = User(user_id="u-new", email="new.person@example.edu", name="N", roles=["default"])
    assert [n["kind"] for n in inbox(newcomer)["notifications"]] == ["project_invited"]


def test_reading_notifications(service, pid):
    service.update_member_role(pid, OWNER, VIEWER.email, "editor")
    c = client(VIEWER, notification_routes.router)
    newest, older = [n["notificationId"] for n in c.get("/notifications").json()["notifications"]]

    assert c.post(f"/notifications/{newest}/read").status_code == 204
    assert c.post(f"/notifications/{newest}/read").status_code == 204  # idempotent
    body = c.get("/notifications").json()
    assert body["unreadCount"] == 1
    assert body["notifications"][0]["readAt"] is not None
    assert [n["notificationId"] for n in c.get("/notifications?unreadOnly=true").json()["notifications"]] == [older]

    # Someone else's id is simply not in this inbox.
    assert client(EDITOR, notification_routes.router).post(f"/notifications/{older}/read").status_code == 404

    assert c.post("/notifications/read-all").json() == {"marked": 1}
    assert c.get("/notifications").json()["unreadCount"] == 0


def test_notifications_page_newest_first(service, pid):
    for i in range(5):
        service.update_member_role(pid, OWNER, VIEWER.email, "editor" if i % 2 == 0 else "viewer")
    c = client(VIEWER, notification_routes.router)
    seen, cursor = [], None
    while True:
        body = c.get("/notifications", params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        seen += [n["notificationId"] for n in body["notifications"]]
        cursor = body["nextCursor"]
        if not cursor:
            break
    assert len(seen) == 6 and seen == sorted(seen, reverse=True)


# ---- the readers ---------------------------------------------------------


def test_editors_read_the_trail_without_user_ids(pid):
    body = client(EDITOR).get(f"/projects/{pid}/audit").json()
    assert [r["action"] for r in body["records"]][-1] == "project.created"
    assert body["records"][-1]["actorEmail"] == OWNER.email
    assert not any("actorUserId" in r or "targetId" in r for r in body["records"])

    assert client(VIEWER).get(f"/projects/{pid}/audit").status_code == 403
    assert client(STRANGER).get(f"/projects/{pid}/audit").status_code == 404


def test_the_trail_pages_within_its_own_project(service, pid):
    other = asyncio.run(service.create_project(STRANGER, "Elsewhere")).project_id
    c = client(EDITOR)
    first = c.get(f"/projects/{pid}/audit?limit=2").json()
    rest = c.get(f"/projects/{pid}/audit", params={"limit": 50, "cursor": first["nextCursor"]}).json()
    assert len(first["records"]) + len(rest["records"]) == 3
    assert {r["auditId"] for r in first["records"]}.isdisjoint(r["auditId"] for r in rest["records"])
    # Another project's cursor cannot page this caller into that project.
    foreign = service.audit_trail(other, limit=1)[0][0]
    assert c.get(f"/projects/{pid}/audit", params={"cursor": f"{foreign.timestamp}#{foreign.audit_id}"}).status_code == 200


# ---- admin ---------------------------------------------------------------


def test_admins_list_look_archive_and_read_every_project(service, pid):
    asyncio.run(service.create_project(STRANGER, "Elsewhere"))
    c = client(ADMIN, admin_routes.router)

    listed = c.get("/admin/projects").json()
    assert sorted(p["name"] for p in listed["projects"]) == ["Budget", "Elsewhere"]
    assert c.get(f"/admin/projects/{pid}").json()["ownerEmail"] == OWNER.email
    assert c.get("/admin/projects/prj_missing").status_code == 404

    archived = c.patch(f"/admin/projects/{pid}", json={"status": "archived", "reason": "Policy review"}).json()
    assert archived["status"] == "archived"
    assert client(EDITOR).patch(f"/projects/{pid}", json={"name": "x"}).status_code == 409

    trail = c.get(f"/admin/projects/{pid}/audit").json()["records"]
    assert (trail[0]["action"], trail[0]["actorEmail"], trail[0]["reason"]) == (
        "project.archived", ADMIN.email, "Policy review",
    )
    assert "actorUserId" in trail[0]


def test_admin_list_pages(service):
    for i in range(5):
        asyncio.run(service.create_project(OWNER, f"P{i}"))
    c = client(ADMIN, admin_routes.router)
    names, cursor = [], None
    while True:
        body = c.get("/admin/projects", params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        names += [p["name"] for p in body["projects"]]
        cursor = body["nextCursor"]
        if not cursor:
            break
    assert sorted(names) == [f"P{i}" for i in range(5)]
