"""A project's settings routes against a real harness record (shared-projects PR-1.5a).

Instructions, model, tools and skills live on the project's harness Agent; these
routes are its only write path, and every save cuts a version.
"""

from __future__ import annotations

import asyncio

from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.app_api.projects.harness_settings as harness_settings
import apis.app_api.projects.routes as project_routes
from apis.app_api.agent_designer.services.binding_validation import BindingValidationError
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

from tests.shared.test_project_harness_rules import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    STRANGER,
    VIEWER,
    project,
    projects_table,
)


class ValidationRecorder:
    """Stands in for ``validate_agent_write``: records what a save asked to validate."""

    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.refuse: set = set()

    async def __call__(self, user, *, bindings=None, model_settings=None, **_):
        self.calls.append({
            "user": user.user_id,
            "refs": [b.ref for b in bindings] if bindings is not None else None,
            "model": model_settings.model_id if model_settings else None,
        })
        for b in bindings or []:
            if b.ref in self.refuse:
                raise BindingValidationError(f"You can't use {b.ref}", status_code=403)


@pytest.fixture()
def validation(monkeypatch) -> ValidationRecorder:
    recorder = ValidationRecorder()
    monkeypatch.setattr(harness_settings, "validate_agent_write", recorder)
    return recorder


@pytest.fixture()
def pid(project, validation, monkeypatch) -> str:
    service, created = project
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    monkeypatch.setattr(project_routes, "_service", service)
    return created.project_id


def client(user: User) -> TestClient:
    app = FastAPI()
    app.include_router(project_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


MATRIX = [
    ("GET", "/instructions", None, {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404}),
    ("PUT", "/instructions", {"instructions": "Be brief."}, {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404}),
    ("GET", "/model", None, {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404}),
    ("PUT", "/model", {"modelConfig": {"modelId": "m-1"}}, {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404}),
    ("GET", "/tools", None, {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404}),
    ("PUT", "/tools", {"bindings": [{"ref": "web_search"}]}, {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404}),
    ("GET", "/skills", None, {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404}),
    ("PUT", "/skills", {"bindings": [{"ref": "sk-1"}]}, {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404}),
    ("GET", "/instructions/versions", None, {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404}),
]
PRINCIPALS = {"owner": OWNER, "editor": EDITOR, "viewer": VIEWER, "stranger": STRANGER}


@pytest.mark.parametrize("principal", list(PRINCIPALS))
@pytest.mark.parametrize("method,suffix,body,expected", MATRIX, ids=[f"{m} {s}" for m, s, _, _ in MATRIX])
def test_authorization_matrix(pid, principal, method, suffix, body, expected):
    response = client(PRINCIPALS[principal]).request(method, f"/projects/{pid}{suffix}", json=body)
    assert response.status_code == expected[principal], response.text


def test_an_archived_project_is_read_only(pid, project):
    service, _ = project
    asyncio.run(service.update_project(pid, OWNER, status="archived"))
    assert client(OWNER).put(f"/projects/{pid}/instructions", json={"instructions": "x"}).status_code == 409
    body = client(OWNER).get(f"/projects/{pid}/instructions").json()
    assert body["canEdit"] is False


def test_each_save_is_a_version_and_the_first_keeps_the_starting_state(pid):
    editor = client(EDITOR)
    first = editor.put(f"/projects/{pid}/instructions", json={"instructions": "Cite sources."}).json()
    assert (first["instructions"], first["version"], first["canEdit"]) == ("Cite sources.", 2, True)

    editor.put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "web_search"}]})
    history = client(VIEWER).get(f"/projects/{pid}/instructions/versions").json()["versions"]
    assert [(v["version"], v["createdByEmail"], v["changes"]) for v in history] == [
        (3, EDITOR.email, ["bindings"]),
        (2, EDITOR.email, ["instructions"]),
        (1, None, history[2]["changes"]),  # the state the project was created with
    ]


def test_a_save_that_changes_nothing_cuts_no_version(pid):
    editor = client(EDITOR)
    editor.put(f"/projects/{pid}/instructions", json={"instructions": "Same."})
    again = editor.put(f"/projects/{pid}/instructions", json={"instructions": "Same."}).json()
    assert again["version"] == 2
    assert len(client(EDITOR).get(f"/projects/{pid}/instructions/versions").json()["versions"]) == 2


def test_a_rename_reaches_the_harness_and_stays_out_of_the_settings_history(pid, project):
    """``PATCH /projects/{id}`` renames the harness too (the chat breadcrumb reads its
    name), without cutting a version. A version snapshot does carry the name, so the
    first save after a rename must still report only what that save changed."""
    from apis.shared.assistants.service import get_assistant_with_access_check

    _, created = project
    editor = client(EDITOR)
    editor.put(f"/projects/{pid}/instructions", json={"instructions": "Cite sources."})
    assert editor.patch(f"/projects/{pid}", json={"name": "Enrollment Sync FY27"}).status_code == 200

    harness, _ = asyncio.run(get_assistant_with_access_check(created.harness_agent_id, VIEWER.user_id, VIEWER.email))
    assert harness.name == "Enrollment Sync FY27"
    assert len(editor.get(f"/projects/{pid}/instructions/versions").json()["versions"]) == 2

    editor.put(f"/projects/{pid}/instructions", json={"instructions": "Cite every source."})
    history = editor.get(f"/projects/{pid}/instructions/versions").json()["versions"]
    assert history[0]["changes"] == ["instructions"]
    detail = editor.get(f"/projects/{pid}/instructions/versions/3").json()
    assert detail["changes"] == ["instructions"]
    assert [c["field"] for c in detail["fieldChanges"]] == ["instructions"]


def test_version_detail_diffs_against_the_one_before(pid):
    editor = client(EDITOR)
    editor.put(f"/projects/{pid}/instructions", json={"instructions": "Line one.\nLine two."})
    editor.put(f"/projects/{pid}/instructions", json={"instructions": "Line one.\nLine 2."})

    detail = client(VIEWER).get(f"/projects/{pid}/instructions/versions/3").json()
    assert detail["instructions"] == "Line one.\nLine 2."
    assert detail["changes"] == ["instructions"]
    assert detail["fieldChanges"][0]["field"] == "instructions" and detail["fieldChanges"][0]["behavior"] is True
    assert detail["instructionsDiff"][:2] == ["--- version 2", "+++ version 3"]
    assert "-Line two." in detail["instructionsDiff"] and "+Line 2." in detail["instructionsDiff"]
    assert "createdBy" not in detail

    assert client(VIEWER).get(f"/projects/{pid}/instructions/versions/9").status_code == 404
    assert client(STRANGER).get(f"/projects/{pid}/instructions/versions/1").status_code == 404


def test_tools_and_skills_replace_only_their_own_kind(pid):
    editor = client(EDITOR)
    editor.put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "web_search"}, {"ref": "calculator"}]})
    editor.put(f"/projects/{pid}/skills", json={"bindings": [{"ref": "sk-1"}]})
    editor.put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "calculator"}]})

    assert [b["ref"] for b in client(VIEWER).get(f"/projects/{pid}/tools").json()["bindings"]] == ["calculator"]
    assert [b["ref"] for b in client(VIEWER).get(f"/projects/{pid}/skills").json()["bindings"]] == ["sk-1"]
    detail = client(VIEWER).get(f"/projects/{pid}/instructions/versions/4").json()
    assert ([t["ref"] for t in detail["tools"]], [s["ref"] for s in detail["skills"]]) == (["calculator"], ["sk-1"])


def test_only_what_a_save_adds_is_checked_against_the_saver(pid, validation):
    client(OWNER).put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "owner_only_tool"}]})
    validation.refuse.add("owner_only_tool")

    # The editor keeps the owner's tool and adds one of their own: only the new one is checked.
    response = client(EDITOR).put(
        f"/projects/{pid}/tools", json={"bindings": [{"ref": "owner_only_tool"}, {"ref": "web_search"}]}
    )
    assert response.status_code == 200, response.text
    assert validation.calls[-1] == {"user": EDITOR.user_id, "refs": ["web_search"], "model": None}

    # Instruction edits check nothing, however the bindings look.
    client(EDITOR).put(f"/projects/{pid}/instructions", json={"instructions": "New."})
    assert validation.calls[-1]["refs"] is None


def test_a_refused_binding_or_model_saves_nothing(pid, validation):
    validation.refuse.add("secret_tool")
    response = client(EDITOR).put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "secret_tool"}]})
    assert (response.status_code, response.json()["detail"]) == (403, "You can't use secret_tool")
    assert client(EDITOR).get(f"/projects/{pid}/tools").json() == {"bindings": [], "version": None, "canEdit": True}


def test_model_round_trips_and_is_validated_for_the_saver(pid, validation):
    body = client(EDITOR).put(f"/projects/{pid}/model", json={"modelConfig": {"modelId": "m-1", "params": {"temperature": 0.2}}}).json()
    assert body["modelConfig"] == {"modelId": "m-1", "provider": None, "params": {"temperature": 0.2}}
    assert validation.calls[-1]["model"] == "m-1"
    assert client(VIEWER).get(f"/projects/{pid}/model").json()["modelConfig"]["modelId"] == "m-1"


def test_the_agent_routes_refuse_to_edit_a_harness(pid, project, monkeypatch):
    """Edits there would skip the project's version history."""
    import apis.app_api.agent_designer.routes as agent_routes
    import apis.app_api.assistants.routes as assistant_routes

    _, created = project
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")
    for router in (assistant_routes.router, agent_routes.router):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_from_session] = lambda: OWNER
        response = TestClient(app, raise_server_exceptions=False).put(
            f"{router.prefix}/{created.harness_agent_id}", json={"instructions": "sneaky"}
        )
        assert response.status_code == 409, (router.prefix, response.text)
        assert "belongs to a project" in response.json()["detail"]
    assert client(OWNER).get(f"/projects/{pid}/instructions").json()["instructions"] != "sneaky"


class AuditRecorder:
    configured = True

    def __init__(self) -> None:
        self.records: List[dict] = []

    def record(self, **kw):
        self.records.append(kw)


def test_each_settings_save_is_on_the_projects_audit_trail(pid, project, monkeypatch):
    service, _ = project
    trail = AuditRecorder()
    monkeypatch.setattr(service, "audit", trail)

    client(EDITOR).put(f"/projects/{pid}/instructions", json={"instructions": "Be brief."})
    client(EDITOR).put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "web_search"}]})
    client(EDITOR).put(f"/projects/{pid}/model", json={"modelConfig": {"modelId": "m-1"}})
    client(EDITOR).put(f"/projects/{pid}/instructions", json={"instructions": "Be brief."})  # no change

    assert [(r["action"], r["target_type"], r["target_id"], r["actor"].user_id) for r in trail.records] == [
        ("project.instructions_updated", "project", pid, EDITOR.user_id),
        ("project.tools_updated", "project", pid, EDITOR.user_id),
        ("project.model_updated", "project", pid, EDITOR.user_id),
    ]
    assert trail.records[1]["before"] == {"refs": []}
    assert trail.records[1]["after"] == {"version": 3, "refs": ["web_search"]}
    assert "Be brief." not in str(trail.records[0])  # the text lives in the version history
