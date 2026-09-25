"""``/projects`` routes: the authorization matrix (shared-projects §5, §10; PR-1.2).

Every route × five principals — owner, editor, viewer, a stranger, and a member of a
*different* project — asserting the exact status code. The last principal is the
cross-project isolation check: a role in project B must behave like no role at all in
project A, including being told "not found" rather than "forbidden".

Unauthenticated access (401 on every route) is covered repo-wide by
``tests/routes/test_pbt_auth_sweep.py``, which walks the real app.
"""

from __future__ import annotations

import asyncio
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import apis.app_api.projects.routes as project_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectService

from tests.shared.test_projects import TABLE, FakeHarness, make_projects_table

OWNER = User(user_id="u-owner", email="owner@example.edu", name="O", roles=["default"])
EDITOR = User(user_id="u-editor", email="editor@example.edu", name="E", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="V", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="S", roles=["default"])
OTHER = User(user_id="u-other", email="other@example.edu", name="X", roles=["default"])
THIRD = "third@example.edu"

PRINCIPALS: Dict[str, User] = {
    "owner": OWNER,
    "editor": EDITOR,
    "viewer": VIEWER,
    "stranger": STRANGER,
    "other_project_member": OTHER,
}


@pytest.fixture()
def env(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        make_projects_table()
        yield


@pytest.fixture()
def task_queries(monkeypatch) -> list:
    """Stand-in for the sessions-table query behind ``/tasks``; records who asked for what."""
    calls: list = []

    async def fake_list_project_sessions(user_id, project_id, limit=50, next_token=None):
        calls.append((user_id, project_id, limit, next_token))
        return [], None

    monkeypatch.setattr(project_routes, "list_project_sessions", fake_list_project_sessions)
    return calls


class FakeDirectory:
    """Knows the owner, the editor and one person outside the project."""

    def __init__(self) -> None:
        self.queries: list = []

    async def search(self, query, limit):
        from apis.shared.directory import DirectoryPerson

        self.queries.append((query, limit))
        people = [
            DirectoryPerson(email=OWNER.email, name="O"),
            DirectoryPerson(email=EDITOR.email, name="E"),
            DirectoryPerson(email="outsider@example.edu", name="Out Sider"),
        ]
        return [p for p in people if query.lower() in p.email or query.lower() in p.name.lower()][:limit]


@pytest.fixture()
def directory(monkeypatch) -> FakeDirectory:
    fake = FakeDirectory()
    monkeypatch.setattr(project_routes, "get_directory", lambda: fake)
    return fake


class FakeMemory:
    """Hands out space ids; the spaces themselves are covered in test_project_memory_spaces."""

    enabled = True

    def __init__(self) -> None:
        self.created: list = []

    def create_space(self, *, project_id, scope, owner_id, owner_email, name, user_id=None) -> str:
        self.created.append((scope, project_id, user_id))
        return f"spc_fake{len(self.created)}"

    def rename_space(self, space_id, name) -> None:
        pass

    def purge_space(self, space_id) -> None:
        pass


@pytest.fixture()
def service(env, monkeypatch, task_queries, directory) -> ProjectService:
    svc = ProjectService(repository=ProjectRepository(table_name=TABLE), harness=FakeHarness(), memory=FakeMemory())
    monkeypatch.setattr(project_routes, "_service", svc)
    return svc


@pytest.fixture()
def project_id(service) -> str:
    """Project A (owner + editor + two viewers); project B, where OTHER is an editor."""
    a = asyncio.run(service.create_project(OWNER, "Project A"))
    service.add_members(a.project_id, OWNER, [EDITOR.email], "editor")
    service.add_members(a.project_id, OWNER, [VIEWER.email, THIRD], "viewer")
    b = asyncio.run(service.create_project(STRANGER, "Project B"))
    service.add_members(b.project_id, STRANGER, [OTHER.email], "editor")
    return a.project_id


def client_for(user: User) -> TestClient:
    app = FastAPI()
    app.include_router(project_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


#         method,  path (relative to /projects/{id}), body, expected by principal
MATRIX = [
    ("GET", "", None,
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
    ("PATCH", "", {"name": "Renamed"},
     {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("PATCH", "", {"status": "archived"},
     {"owner": 200, "editor": 403, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("PATCH", "", {"editorsManageMembers": False},
     {"owner": 200, "editor": 403, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("DELETE", "", None,  # active project: archive first
     {"owner": 409, "editor": 403, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("POST", "/transfer", {"email": EDITOR.email},  # editor has not signed in yet
     {"owner": 409, "editor": 403, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("GET", "/members", None,
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
    ("POST", "/members", {"emails": ["new@example.edu"], "role": "viewer"},
     {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("PATCH", f"/members/{THIRD}", {"role": "editor"},
     {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("DELETE", f"/members/{THIRD}", None,
     {"owner": 204, "editor": 204, "viewer": 403, "stranger": 404, "other_project_member": 404}),
    ("DELETE", "/members/me", None,
     {"owner": 409, "editor": 204, "viewer": 204, "stranger": 404, "other_project_member": 404}),
    ("GET", "/directory?q=out", None,
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
    ("GET", "/tasks", None,
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
    ("GET", "/shared-tasks", None,
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
    ("GET", "/memory", None,
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
    ("POST", "/memory/mine", None,  # a viewer keeps their own memory too
     {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404, "other_project_member": 404}),
]


@pytest.mark.parametrize("principal", list(PRINCIPALS))
@pytest.mark.parametrize("method,suffix,body,expected", MATRIX, ids=[f"{m} {s or '/'} {b or ''}" for m, s, b, _ in MATRIX])
def test_authorization_matrix(project_id, principal, method, suffix, body, expected):
    response = client_for(PRINCIPALS[principal]).request(method, f"/projects/{project_id}{suffix}", json=body)
    assert response.status_code == expected[principal], response.text


def test_create_returns_the_owner_view(service):
    response = client_for(OWNER).post("/projects", json={"name": "New project", "description": "d"})
    assert response.status_code == 201
    body = response.json()
    assert (body["role"], body["ownerEmail"], body["status"], body["memberCount"]) == (
        "owner", OWNER.email, "active", 0,
    )
    assert body["harnessAgentId"].startswith("ast-")
    assert "ownerId" not in body


def test_list_shows_each_project_once_with_the_callers_role(project_id):
    names = {p["name"]: p["role"] for p in client_for(EDITOR).get("/projects").json()["projects"]}
    assert names == {"Project A": "editor"}
    assert client_for(OTHER).get("/projects").json()["projects"][0]["name"] == "Project B"


def test_members_lists_owner_first_and_reports_manage_rights(project_id):
    body = client_for(VIEWER).get(f"/projects/{project_id}/members").json()
    assert body["members"][0] == {
        "email": OWNER.email, "role": "owner", "hasSignedIn": True, "createdAt": body["members"][0]["createdAt"],
    }
    assert [m["email"] for m in body["members"][1:]] == sorted([EDITOR.email, VIEWER.email, THIRD])
    assert body["canManage"] is False
    assert client_for(EDITOR).get(f"/projects/{project_id}/members").json()["canManage"] is True


def test_bulk_add_reports_each_bucket(project_id):
    body = client_for(OWNER).post(
        f"/projects/{project_id}/members",
        json={"emails": ["a@example.edu", VIEWER.email, "nope"], "role": "editor"},
    ).json()
    assert [m["email"] for m in body["added"]] == ["a@example.edu"]
    assert body["alreadyMembers"] == [VIEWER.email]
    assert body["invalid"] == ["nope"]
    assert body["overCapacity"] == []


def test_archive_then_delete_purges(project_id):
    owner = client_for(OWNER)
    assert owner.patch(f"/projects/{project_id}", json={"status": "archived"}).status_code == 200
    assert owner.post(f"/projects/{project_id}/members", json={"emails": ["x@example.edu"]}).status_code == 409
    assert owner.delete(f"/projects/{project_id}").status_code == 204
    assert owner.get(f"/projects/{project_id}").status_code == 404


def test_transfer_after_the_editor_has_visited(project_id):
    assert client_for(EDITOR).get(f"/projects/{project_id}").status_code == 200
    response = client_for(OWNER).post(f"/projects/{project_id}/transfer", json={"email": EDITOR.email})
    assert response.status_code == 200
    assert (response.json()["ownerEmail"], response.json()["role"]) == (EDITOR.email, "editor")


def test_feature_flag_off_is_a_404_for_a_signed_in_user(project_id, monkeypatch):
    monkeypatch.setenv("PROJECTS_ENABLED", "false")
    assert client_for(OWNER).get("/projects").status_code == 404
    assert client_for(OWNER).get(f"/projects/{project_id}").status_code == 404


def test_tasks_lists_only_the_callers_own_sessions_in_the_project(project_id, task_queries):
    response = client_for(VIEWER).get(f"/projects/{project_id}/tasks?limit=5&nextToken=abc")
    assert response.status_code == 200
    assert response.json() == {"sessions": []}
    assert task_queries == [(VIEWER.user_id, project_id, 5, "abc")]


def test_tasks_never_queries_for_a_non_member(project_id, task_queries):
    assert client_for(STRANGER).get(f"/projects/{project_id}/tasks").status_code == 404
    assert task_queries == []


def test_shared_tasks_hide_user_ids_and_mark_the_callers_own(project_id, service):
    from apis.shared.projects.models import SharedTask

    for sid, owner, email, at in (("s1", EDITOR, EDITOR.email, "2026-09-01"), ("s2", VIEWER, VIEWER.email, "2026-09-02")):
        service.repository.put_shared_task(SharedTask(
            project_id=project_id, session_id=sid, share_id=f"sh-{sid}", owner_id=owner.user_id,
            owner_email=email, title=f"Task {sid}", shared_at=at,
        ))
    tasks = client_for(VIEWER).get(f"/projects/{project_id}/shared-tasks").json()["tasks"]
    assert [(t["shareId"], t["sharedByEmail"], t["isMine"], t["shareUrl"]) for t in tasks] == [
        ("sh-s2", VIEWER.email, True, "/shared/sh-s2"),
        ("sh-s1", EDITOR.email, False, "/shared/sh-s1"),
    ]
    assert not any("ownerId" in t or "sessionId" in t for t in tasks)


def test_directory_marks_people_already_in_the_project(project_id):
    people = client_for(EDITOR).get(f"/projects/{project_id}/directory?q=example").json()["people"]
    assert [(p["email"], p["memberRole"], p["hasSignedIn"]) for p in people] == [
        (OWNER.email, "owner", True),
        (EDITOR.email, "editor", True),
        ("outsider@example.edu", None, True),
    ]
    assert not any("userId" in p for p in people)


def test_directory_offers_an_unknown_email_so_it_can_be_invited(project_id, directory):
    people = client_for(OWNER).get(f"/projects/{project_id}/directory?q=New.Person@Example.edu").json()["people"]
    assert people == [{"email": "new.person@example.edu", "name": "", "hasSignedIn": False, "memberRole": None}]

    # A member who has never signed in is unknown to the directory but still marked.
    people = client_for(OWNER).get(f"/projects/{project_id}/directory?q={THIRD}").json()["people"]
    assert [(p["email"], p["memberRole"]) for p in people] == [(THIRD, "viewer")]


def test_directory_fallback_respects_the_limit_and_only_takes_real_emails(project_id):
    people = client_for(OWNER).get(f"/projects/{project_id}/directory?q=not-an-email").json()["people"]
    assert people == []
    # "r@example.edu" is a substring of all three known emails and a valid address itself.
    people = client_for(OWNER).get(f"/projects/{project_id}/directory?q=r@example.edu&limit=2").json()["people"]
    assert [p["email"] for p in people] == [OWNER.email, "r@example.edu"]


def test_directory_never_searches_for_a_non_member(project_id, directory):
    assert client_for(STRANGER).get(f"/projects/{project_id}/directory?q=a").status_code == 404
    assert directory.queries == []


def test_memory_reports_the_shared_space_and_only_the_callers_own(project_id, service):
    shared = service.repository.get_project(project_id).shared_space_id
    assert client_for(VIEWER).get(f"/projects/{project_id}/memory").json() == {
        "sharedSpaceId": shared, "personalSpaceId": None, "role": "viewer",
    }

    mine = client_for(VIEWER).post(f"/projects/{project_id}/memory/mine").json()["spaceId"]
    assert client_for(VIEWER).post(f"/projects/{project_id}/memory/mine").json()["spaceId"] == mine
    assert client_for(VIEWER).get(f"/projects/{project_id}/memory").json()["personalSpaceId"] == mine
    assert client_for(EDITOR).get(f"/projects/{project_id}/memory").json()["personalSpaceId"] is None


def test_no_personal_memory_in_an_archived_project(project_id):
    client_for(OWNER).patch(f"/projects/{project_id}", json={"status": "archived"})
    assert client_for(EDITOR).post(f"/projects/{project_id}/memory/mine").status_code == 409
    assert client_for(EDITOR).get(f"/projects/{project_id}/memory").status_code == 200
