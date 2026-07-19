"""HTTP surface of the My Skills routes (Skills v2 PR-3).

Asserts the contract the SPA depends on: camelCase DTOs, session-cookie auth
(never Bearer-only), and the status mapping for the owner-scoped errors.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.skills import routes as skill_routes
from apis.shared.auth import get_current_user_from_session


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    monkeypatch.setattr(
        skill_routes, "get_user_skill_service", lambda: user_skill_service
    )
    app = FastAPI()
    app.include_router(skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def other_client(user_skill_service, other_user, monkeypatch):
    """Same app, authenticated as a different user."""
    monkeypatch.setattr(
        skill_routes, "get_user_skill_service", lambda: user_skill_service
    )
    app = FastAPI()
    app.include_router(skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: other_user
    with TestClient(app) as c:
        yield c


def test_my_skills_routes_use_session_auth_not_bearer():
    """A Bearer-only dependency here would 401-loop the cookie-bearing SPA.

    Walks the TRANSITIVE dependency tree, not just each route's direct
    dependencies. The routes depend on the session directly today (the
    ``require_skills_capability`` wrapper they used to nest under was removed),
    but the invariant being pinned is "the session dependency is reachable from
    every /mine route", which is what actually keeps the cookie-bearing SPA off
    the 401-redirect loop — asserting the flat shape instead would break on any
    future wrapper while proving less.
    """
    paths = [
        r
        for r in skill_routes.router.routes
        if getattr(r, "path", "").startswith("/skills/mine")
    ]
    assert paths, "expected /mine routes to be registered"

    def _calls(dependant):
        for sub in dependant.dependencies:
            if sub.call is not None:
                yield sub.call
            yield from _calls(sub)

    for route in paths:
        assert get_current_user_from_session in set(_calls(route.dependant)), route.path


def test_create_list_get_roundtrip(client):
    created = client.post(
        "/skills/mine",
        json={
            "displayName": "Grant Writing",
            "description": "How we write grant narratives.",
            "instructions": "Start with the abstract.",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["skillId"] == "grant_writing"
    assert body["displayName"] == "Grant Writing"

    listed = client.get("/skills/mine")
    assert listed.status_code == 200
    assert listed.json()["totalCount"] == 1
    assert listed.json()["skills"][0]["skillId"] == "grant_writing"

    fetched = client.get("/skills/mine/grant_writing")
    assert fetched.status_code == 200
    assert fetched.json()["instructions"] == "Start with the abstract."


def test_create_rejects_a_blank_description(client):
    resp = client.post(
        "/skills/mine", json={"displayName": "Named", "description": "   "}
    )
    assert resp.status_code == 400


def test_update_and_delete(client):
    client.post(
        "/skills/mine", json={"displayName": "Notes", "description": "Old."}
    )

    updated = client.put("/skills/mine/notes", json={"description": "New."})
    assert updated.status_code == 200
    assert updated.json()["description"] == "New."

    deleted = client.delete("/skills/mine/notes")
    assert deleted.status_code == 200
    assert client.get("/skills/mine/notes").status_code == 404


def test_update_cannot_rehome_ownership(client, user_skill_service, author_user):
    """``ownerId``/``visibility`` are absent from the DTO, so they're ignored."""
    client.post("/skills/mine", json={"displayName": "Notes", "description": "d"})

    resp = client.put(
        "/skills/mine/notes",
        json={"description": "d2", "ownerId": "system", "visibility": "admin"},
    )
    assert resp.status_code == 200

    stored = client.get("/skills/mine/notes")
    assert stored.status_code == 200
    # Still owned by the author — proven by it still being reachable as theirs.
    mine = client.get("/skills/mine").json()
    assert [s["skillId"] for s in mine["skills"]] == ["notes"]


def test_another_users_skill_is_404_everywhere(client, other_client):
    client.post("/skills/mine", json={"displayName": "Notes", "description": "d"})

    assert other_client.get("/skills/mine/notes").status_code == 404
    assert other_client.put("/skills/mine/notes", json={"description": "x"}).status_code == 404
    assert other_client.delete("/skills/mine/notes").status_code == 404
    assert other_client.get("/skills/mine/notes/resources").status_code == 404
    assert other_client.get("/skills/mine").json()["totalCount"] == 0


def test_resource_upload_read_and_delete(client):
    client.post("/skills/mine", json={"displayName": "Notes", "description": "d"})

    uploaded = client.post(
        "/skills/mine/notes/resources",
        files={"file": ("forms.md", b"# Forms", "text/markdown")},
    )
    assert uploaded.status_code == 200
    refs = uploaded.json()["resources"]
    assert refs[0]["filename"] == "forms.md"
    assert refs[0]["s3Key"] == "skills/notes/references/forms.md"

    read = client.get("/skills/mine/notes/resources/forms.md")
    assert read.status_code == 200
    assert read.content == b"# Forms"

    removed = client.delete("/skills/mine/notes/resources/forms.md")
    assert removed.status_code == 200
    assert removed.json()["resources"] == []


def test_script_uploads_are_accepted_and_stored_inert(client):
    client.post("/skills/mine", json={"displayName": "Notes", "description": "d"})

    uploaded = client.post(
        "/skills/mine/notes/resources",
        files={"file": ("build.py", b"print(1)", "text/x-python")},
        data={"kind": "script"},
    )

    assert uploaded.status_code == 200
    ref = uploaded.json()["resources"][0]
    assert ref["kind"] == "script"
    assert ref["s3Key"] == "skills/notes/scripts/build.py"


def test_resource_upload_rejects_a_traversal_filename(client):
    client.post("/skills/mine", json={"displayName": "Notes", "description": "d"})

    resp = client.post(
        "/skills/mine/notes/resources",
        files={"file": ("../../etc/passwd", b"x", "text/plain")},
    )

    assert resp.status_code == 400
