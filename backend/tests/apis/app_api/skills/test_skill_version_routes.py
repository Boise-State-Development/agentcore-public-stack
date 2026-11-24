"""Version history routes for both tiers, against moto
(docs/specs/skill-version-history.md §5, §6.1, PR-2).

What these pin:

* the flag 404s every route, after auth;
* a list is newest first, paged, marks the version matching the live skill as
  current, and names its author only as ``createdByYou`` on My Skills;
* a detail carries the diff from the live skill to that version;
* a restore writes a new version with the old content, files and display name, and
  can itself be undone; a stale ``expectedUpdatedAt`` is a 409 and a missing
  version a 404; My Skills asks for the reach confirmation first;
* restoring a hand-written version over an imported skill clears its import source;
* the admin routes serve catalog skills only.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as admin_routes
from apis.app_api.skills import bundle_import_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.app_api.skills import version_routes
from apis.shared.auth import get_current_user_from_session
from tests.conftest import override_admin_auth

SKILL_MD = """---
name: contract-redline
description: Redline a contract against a playbook.
---

Use the NDA playbook.
"""


def make_zip(files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _user_app(user, service, monkeypatch, *, enabled=True):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true" if enabled else "")
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "true")
    for module in (version_routes, my_skill_routes, bundle_import_routes):
        monkeypatch.setattr(module, "get_user_skill_service", lambda: service)
    # Reach is the reach module's concern; here only whether the route asks.
    monkeypatch.setattr(version_routes, "require_reach_confirmed", _no_reach)
    app = FastAPI()
    app.include_router(bundle_import_routes.router)
    app.include_router(version_routes.router)
    app.include_router(my_skill_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


async def _no_reach(skill_id, user):
    return None


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    with _user_app(author_user, user_skill_service, monkeypatch) as c:
        yield c


@pytest.fixture()
def admin(skill_service, admin_user, monkeypatch):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
    monkeypatch.setattr(admin_routes, "get_skill_catalog_service", lambda: skill_service)
    app = FastAPI()
    app.include_router(admin_routes.router)
    override_admin_auth(app, lambda: admin_user)
    with TestClient(app) as c:
        yield c


def _new_skill(client, **kw) -> dict:
    body = {"displayName": "Grant Review", "description": "Review a grant.", "instructions": "v1"}
    body.update(kw)
    resp = client.post("/skills/mine", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _edit(client, sid, **fields) -> dict:
    resp = client.put(f"/skills/mine/{sid}", json=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload(client, sid, name, body):
    resp = client.post(
        f"/skills/mine/{sid}/resources",
        files={"file": (name, body.encode(), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text


def _versions(client, sid, **params):
    resp = client.get(f"/skills/mine/{sid}/versions", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _restore(client, sid, number, **body):
    return client.post(f"/skills/mine/{sid}/versions/{number}/restore", json=body)


class TestFlag:
    def test_off_is_404_after_auth(self, user_skill_service, author_user, monkeypatch):
        with _user_app(author_user, user_skill_service, monkeypatch, enabled=False) as off:
            assert off.get("/skills/mine/x/versions").status_code == 404
            assert off.get("/skills/mine/x/versions/1").status_code == 404
            assert off.post("/skills/mine/x/versions/1/restore", json={}).status_code == 404
        with _user_app(None, user_skill_service, monkeypatch, enabled=False) as anonymous:
            assert anonymous.get("/skills/mine/x/versions").status_code == 401


class TestList:
    def test_newest_first_with_current_and_author(self, client):
        sid = _new_skill(client)["skillId"]
        _edit(client, sid, instructions="v2")
        _edit(client, sid, instructions="v3")
        rows = _versions(client, sid)["versions"]
        assert [(r["version"], r["trigger"], r["isCurrent"]) for r in rows] == [
            (3, "save", True),
            (2, "save", False),
            (1, "create", False),
        ]
        assert all(r["createdByYou"] is True for r in rows)
        assert all("createdByEmail" not in r for r in rows)

    def test_pages_with_a_cursor(self, client):
        sid = _new_skill(client)["skillId"]
        for n in range(2, 6):
            _edit(client, sid, instructions=f"v{n}")
        first = _versions(client, sid, limit=2)
        assert [r["version"] for r in first["versions"]] == [5, 4]
        second = _versions(client, sid, limit=2, before=first["nextCursor"])
        assert [r["version"] for r in second["versions"]] == [3, 2]
        last = _versions(client, sid, limit=2, before=second["nextCursor"])
        assert [r["version"] for r in last["versions"]] == [1]

    def test_another_users_skill_is_404(self, client, user_skill_service, other_user, monkeypatch):
        sid = _new_skill(client)["skillId"]
        with _user_app(other_user, user_skill_service, monkeypatch) as other:
            assert other.get(f"/skills/mine/{sid}/versions").status_code == 404
            assert other.get(f"/skills/mine/{sid}/versions/1").status_code == 404
            assert other.post(f"/skills/mine/{sid}/versions/1/restore", json={}).status_code == 404


class TestDetail:
    def test_the_diff_runs_from_live_to_the_version(self, client):
        sid = _new_skill(client, instructions="Read it closely.")["skillId"]
        _upload(client, sid, "rubric.md", "rubric v1")
        _edit(client, sid, instructions="Skim it.")
        _upload(client, sid, "extra.md", "extra")
        detail = client.get(f"/skills/mine/{sid}/versions/2").json()
        assert detail["instructions"] == "Read it closely."
        assert detail["isCurrent"] is False
        assert "+Read it closely." in detail["instructionsDiff"]["diff"]
        assert "-Skim it." in detail["instructionsDiff"]["diff"]
        assert detail["descriptionDiff"]["diff"] == ""
        assert {(r["filename"], r["status"]) for r in detail["fileChanges"]} == {
            ("rubric.md", "unchanged"),
            ("extra.md", "removed"),
        }

    def test_a_missing_version_is_404(self, client):
        sid = _new_skill(client)["skillId"]
        assert client.get(f"/skills/mine/{sid}/versions/9").status_code == 404


class TestRestore:
    def test_restore_is_a_new_version_with_the_old_content(self, client, skill_resource_store):
        created = _new_skill(client, displayName="Grant Review", instructions="first")
        sid = created["skillId"]
        _upload(client, sid, "rubric.md", "rubric v1")  # v2
        _upload(client, sid, "rubric.md", "rubric v2")  # v3
        _upload(client, sid, "extra.md", "extra")  # v4
        live = _edit(client, sid, displayName="Grant Triage", instructions="second")  # v5

        resp = _restore(client, sid, 2, expectedUpdatedAt=live["updatedAt"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skill"]["instructions"] == "first"
        assert body["skill"]["displayName"] == "Grant Review"
        assert [r["filename"] for r in body["skill"]["resources"]] == ["rubric.md"]
        key = body["skill"]["resources"][0]["s3Key"]
        assert skill_resource_store.get(key) == b"rubric v1"
        assert (body["version"]["version"], body["version"]["trigger"]) == (6, "restore")
        assert body["version"]["restoredFrom"] == 2
        assert body["version"]["isCurrent"] is True

        undo = _restore(client, sid, 5, expectedUpdatedAt=body["skill"]["updatedAt"])
        assert undo.status_code == 200, undo.text
        assert undo.json()["skill"]["instructions"] == "second"
        assert undo.json()["skill"]["displayName"] == "Grant Triage"
        assert sorted(r["filename"] for r in undo.json()["skill"]["resources"]) == [
            "extra.md",
            "rubric.md",
        ]
        assert undo.json()["version"]["version"] == 7

    def test_a_stale_view_is_409_and_writes_nothing(self, client):
        sid = _new_skill(client, instructions="first")["skillId"]
        seen = _edit(client, sid, instructions="second")
        _edit(client, sid, instructions="third")
        resp = _restore(client, sid, 1, expectedUpdatedAt=seen["updatedAt"])
        assert resp.status_code == 409
        assert client.get(f"/skills/mine/{sid}").json()["instructions"] == "third"

    def test_a_missing_version_is_404(self, client):
        sid = _new_skill(client)["skillId"]
        assert _restore(client, sid, 9).status_code == 404

    def test_my_skills_asks_for_the_reach_confirmation_first(self, client, monkeypatch):
        sid = _new_skill(client, instructions="first")["skillId"]
        _edit(client, sid, instructions="second")
        asked = []

        async def reaches(skill_id, user):
            asked.append(skill_id)
            raise HTTPException(status_code=409, detail={"code": "skill_reach_unconfirmed"})

        monkeypatch.setattr(version_routes, "require_reach_confirmed", reaches)
        assert _restore(client, sid, 1).status_code == 409
        assert client.get(f"/skills/mine/{sid}").json()["instructions"] == "second"
        assert _restore(client, sid, 1, confirmReach=True).status_code == 200
        assert asked == [sid]

    def test_restoring_hand_written_content_clears_the_import_source(self, client):
        sid = _new_skill(client, instructions="hand written")["skillId"]
        reimport = client.post(
            f"/skills/mine/{sid}/import",
            files={"file": ("b.zip", make_zip({"contract-redline/SKILL.md": SKILL_MD}), "application/zip")},
        )
        assert reimport.status_code == 200, reimport.text
        assert reimport.json()["skill"]["importSource"]["type"] == "zip"
        restored = _restore(client, sid, 1).json()
        assert restored["skill"]["instructions"] == "hand written"
        assert restored["skill"].get("importSource") is None
        assert restored["importSourceRestored"] is True


class TestAdmin:
    def test_catalog_history_and_restore(self, admin):
        admin.post(
            "/skills/",
            json={"skillId": "pdf_tools", "displayName": "PDF", "description": "PDFs.", "instructions": "a"},
        )
        admin.put("/skills/pdf_tools", json={"instructions": "b"})
        rows = admin.get("/skills/pdf_tools/versions").json()["versions"]
        assert [(r["version"], r["trigger"]) for r in rows] == [(2, "save"), (1, "create")]
        assert all("createdByYou" not in r for r in rows)
        assert admin.get("/skills/pdf_tools/versions/1").json()["instructions"] == "a"
        restored = admin.post("/skills/pdf_tools/versions/1/restore", json={})
        assert restored.status_code == 200, restored.text
        assert restored.json()["skill"]["instructions"] == "a"
        assert restored.json()["version"]["restoredFrom"] == 1

    def test_a_user_skill_is_404(self, admin, user_skill_service, author_user):
        import asyncio

        mine = asyncio.run(
            user_skill_service.create_my_skill(
                author_user, display_name="Mine", description="Mine.", instructions="x"
            )
        )
        assert admin.get(f"/skills/{mine.skill_id}/versions").status_code == 404
        assert admin.get(f"/skills/{mine.skill_id}/versions/1").status_code == 404
        assert admin.post(f"/skills/{mine.skill_id}/versions/1/restore", json={}).status_code == 404

    def test_off_is_404(self, admin, monkeypatch):
        monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "")
        assert admin.get("/skills/pdf_tools/versions").status_code == 404
