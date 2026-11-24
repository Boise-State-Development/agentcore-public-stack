"""The admin catalog's bundle import routes against moto (skill-bundle-import.md).

The catalog twin of ``test_skill_bundle_import_routes``. What these pin: the
flag 404s every route; preview writes nothing; an import creates a **draft**
catalog skill with no grants, named from the frontmatter and suffixed past both
an existing id and an existing activation name; a re-import replaces the files
and keeps the admin's own fields; apply refuses (409) a source that moved; and
the routes never touch a user-authored skill (404, the catalog predicate).
"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as skill_routes
from apis.app_api.skills import bundle_import_core
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillImportSource
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


V1 = {
    "contract-redline/SKILL.md": SKILL_MD,
    "contract-redline/references/nda.md": "NDA rules v1",
    "contract-redline/references/keep.md": "unchanged",
    "contract-redline/scripts/engine.py": "print('v1')",
}
V2 = {
    "contract-redline/SKILL.md": SKILL_MD.replace("NDA playbook", "NDA and MSA playbooks"),
    "contract-redline/references/nda.md": "NDA rules v2",
    "contract-redline/references/keep.md": "unchanged",
    "contract-redline/references/msa.md": "MSA rules",
}


def _app(admin_user, service, monkeypatch, *, enabled=True):
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "true" if enabled else "")
    monkeypatch.setattr(skill_routes, "get_skill_catalog_service", lambda: service)
    app = FastAPI()
    app.include_router(skill_routes.router)
    override_admin_auth(app, lambda: admin_user)
    return TestClient(app)


@pytest.fixture()
def client(skill_service, admin_user, monkeypatch):
    with _app(admin_user, skill_service, monkeypatch) as c:
        yield c


def _zip_form(files, **data):
    return {"files": {"file": ("bundle.zip", make_zip(files), "application/zip")}, "data": data}


def _preview(client, files, **data):
    return client.post("/skills/import/preview", **_zip_form(files, **data))


def _create(client, files, **data):
    return client.post("/skills/import", **_zip_form(files, **data))


def _reimport(client, skill_id, files, **data):
    return client.post(f"/skills/{skill_id}/import", **_zip_form(files, **data))


def _statuses(body):
    return {(r["filename"] or r["path"]): r["status"] for r in body["files"]}


class TestFlag:
    def test_off_is_404_for_every_route(self, skill_service, admin_user, monkeypatch):
        with _app(admin_user, skill_service, monkeypatch, enabled=False) as off:
            assert _preview(off, V1).status_code == 404
            assert _create(off, V1).status_code == 404
            assert _reimport(off, "some_skill", V1).status_code == 404


class TestPreview:
    def test_preview_plans_the_id_and_writes_nothing(self, client):
        resp = _preview(client, V1)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skill"]["storedName"] == "contract-redline"
        assert _statuses(body) == {"nda.md": "added", "keep.md": "added", "engine.py": "added"}
        assert body["bundleHash"]
        assert client.get("/skills/").json()["skills"] == []

    def test_several_skills_return_candidates(self, client):
        many = {"a/s1/SKILL.md": SKILL_MD, "a/s2/SKILL.md": SKILL_MD}
        assert _preview(client, many).json()["candidates"] == ["s1", "s2"]
        assert _create(client, many).status_code == 400
        assert _create(client, many, path="s2").json()["skill"]["importSource"]["path"] == "s2"


class TestCreate:
    def test_import_creates_a_draft_catalog_skill_with_no_grants(self, client):
        resp = _create(client, V1, displayName="Contract Redline")
        assert resp.status_code == 200, resp.text
        skill = resp.json()["skill"]
        assert skill["skillId"] == "contract_redline"
        assert skill["displayName"] == "Contract Redline"
        assert skill["status"] == "draft"
        assert skill["ownerId"] == "system"
        assert skill["allowedAppRoles"] == []
        assert skill["importSource"]["type"] == "zip"
        assert skill["importSource"]["originalName"] == "contract-redline"
        assert sorted(r["filename"] for r in skill["resources"]) == ["engine.py", "keep.md", "nda.md"]
        listed = client.get("/skills/").json()["skills"]
        assert [s["skillId"] for s in listed] == ["contract_redline"]

    def test_a_taken_id_or_activation_name_is_suffixed(self, client):
        client.post(
            "/skills/",
            json={"skillId": "contract_redline", "displayName": "x", "description": "x"},
        )
        assert _preview(client, V1).json()["skill"]["storedName"] == "contract-redline-2"
        assert _create(client, V1).json()["skill"]["skillId"] == "contract_redline_2"

    def test_a_stale_preview_is_409(self, client):
        resp = _create(client, V1, expectedBundleHash="not-the-hash")
        assert resp.status_code == 409
        assert client.get("/skills/").json()["skills"] == []


class TestReimport:
    def test_reimport_replaces_files_and_keeps_admin_fields(self, client):
        created = _create(client, V1, displayName="Contract Redline").json()["skill"]
        sid = created["skillId"]
        client.put(f"/skills/{sid}", json={"status": "active", "category": "document"})

        preview = _preview(client, V2, skillId=sid).json()
        assert _statuses(preview) == {
            "nda.md": "changed",
            "keep.md": "unchanged",
            "msa.md": "added",
            "engine.py": "removed",
        }
        assert preview["changes"]["instructions"] is True
        assert "MSA" in preview["instructionsDiff"]["diff"]

        resp = _reimport(
            client,
            sid,
            V2,
            expectedBundleHash=preview["bundleHash"],
            expectedUpdatedAt=preview["targetUpdatedAt"],
        )
        assert resp.status_code == 200, resp.text
        skill = resp.json()["skill"]
        assert skill["displayName"] == "Contract Redline"
        assert skill["status"] == "active"
        assert skill["category"] == "document"
        assert "MSA" in skill["instructions"]
        assert sorted(r["filename"] for r in skill["resources"]) == ["keep.md", "msa.md", "nda.md"]

    def test_a_skill_edited_since_the_preview_is_409(self, client):
        sid = _create(client, V1).json()["skill"]["skillId"]
        preview = _preview(client, V2, skillId=sid).json()
        client.put(f"/skills/{sid}", json={"description": "edited elsewhere"})
        resp = _reimport(client, sid, V2, expectedUpdatedAt=preview["targetUpdatedAt"])
        assert resp.status_code == 409

    def test_a_user_authored_skill_is_404(self, client, user_skill_service, author_user):
        load = load_bundle(make_zip(V1))
        source = SkillImportSource(type="zip", path="", bundle_hash="h", imported_at="2026-01-01")
        mine = asyncio.run(
            user_skill_service.import_bundle(author_user, load.bundle, source=source)
        )
        assert _preview(client, V2, skillId=mine.skill_id).status_code == 404
        assert _reimport(client, mine.skill_id, V2).status_code == 404
        assert "MSA" not in asyncio.run(
            user_skill_service.get_my_skill(mine.skill_id, author_user)
        ).instructions


class TestGitHub:
    def test_a_github_url_is_fetched_and_recorded(self, client, monkeypatch):
        async def fake_fetch(location):
            buf = io.BytesIO(make_zip({"skills-main/skills/pdf/SKILL.md": SKILL_MD}))
            with zipfile.ZipFile(buf, "a") as zf:
                zf.comment = b"e4bb5d960d87126ff3b3a8af1a104b7db67560ba"
            return buf.getvalue()

        monkeypatch.setattr(bundle_import_core, "fetch_archive", fake_fetch)
        resp = client.post(
            "/skills/import",
            data={"url": "https://github.com/anthropics/skills/tree/main/skills/pdf"},
        )
        assert resp.status_code == 200, resp.text
        source = resp.json()["skill"]["importSource"]
        assert source["type"] == "github"
        assert source["owner"] == "anthropics"
        assert source["path"] == "skills/pdf"
        assert source["commitSha"] == "e4bb5d960d87126ff3b3a8af1a104b7db67560ba"

    def test_a_non_github_url_is_400_and_never_fetched(self, client, monkeypatch):
        async def boom(location):  # pragma: no cover - must not run
            raise AssertionError("fetched")

        monkeypatch.setattr(bundle_import_core, "fetch_archive", boom)
        resp = client.post(
            "/skills/import/preview", data={"url": "https://169.254.169.254/latest/meta-data"}
        )
        assert resp.status_code == 400


class TestVersionHistory:
    def test_a_reimport_whose_checkpoint_fails_is_503_and_writes_nothing(
        self, client, monkeypatch
    ):
        from apis.shared.skills import history

        sid = _create(client, V1).json()["skill"]["skillId"]
        client.put(f"/skills/{sid}", json={"instructions": "edited before history"})
        monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")

        async def broken(*args, **kwargs):
            raise RuntimeError("s3 down")

        monkeypatch.setattr(history, "capture_current", broken)
        resp = _reimport(client, sid, V2)
        assert resp.status_code == 503
        assert "nothing was changed" in resp.json()["detail"]
        assert client.get(f"/skills/{sid}").json()["instructions"] == "edited before history"
