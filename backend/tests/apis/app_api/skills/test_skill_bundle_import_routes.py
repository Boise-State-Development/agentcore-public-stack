"""The bundle import routes against moto DynamoDB and S3 (skill-bundle-import.md).

What these pin: the flag 404s only after auth; preview writes nothing; create and
re-import land files exactly where a hand upload would; a re-import removes what
the bundle dropped and does not rewrite what it kept; apply refuses (409) when
the source or the skill moved since the preview; and a re-import never cuts a
version, so a pinned project keeps its version until someone clicks Update.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.skills import bundle_import_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills import pinning
from apis.shared.skills import repository as repository_module
from apis.shared.skills import resource_store as resource_store_module

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
    "contract-redline/assets/x.svg": "<svg/>",
}
V2 = {
    "contract-redline/SKILL.md": SKILL_MD.replace("NDA playbook", "NDA and MSA playbooks"),
    "contract-redline/references/nda.md": "NDA rules v2",
    "contract-redline/references/keep.md": "unchanged",
    "contract-redline/references/msa.md": "MSA rules",
}


def _app(user, service, monkeypatch, *, enabled=True):
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "true" if enabled else "")
    monkeypatch.setattr(bundle_import_routes, "get_user_skill_service", lambda: service)
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: service)
    app = FastAPI()
    app.include_router(bundle_import_routes.router)
    app.include_router(my_skill_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    with _app(author_user, user_skill_service, monkeypatch) as c:
        yield c


def _zip_form(files, **data):
    return {"files": {"file": ("bundle.zip", make_zip(files), "application/zip")}, "data": data}


def _preview(client, files, **data):
    return client.post("/skills/mine/import/preview", **_zip_form(files, **data))


def _create(client, files, **data):
    return client.post("/skills/mine/import", **_zip_form(files, **data))


def _reimport(client, skill_id, files, **data):
    return client.post(f"/skills/mine/{skill_id}/import", **_zip_form(files, **data))


def _statuses(body):
    return {(r["filename"] or r["path"]): r["status"] for r in body["files"]}


class TestFlag:
    def test_off_is_404_for_every_route(self, user_skill_service, author_user, monkeypatch):
        with _app(author_user, user_skill_service, monkeypatch, enabled=False) as off:
            assert _preview(off, V1).status_code == 404
            assert _create(off, V1).status_code == 404
            assert _reimport(off, "some_skill", V1).status_code == 404

    def test_auth_runs_before_the_flag(self, user_skill_service, monkeypatch):
        with _app(None, user_skill_service, monkeypatch, enabled=False) as anonymous:
            assert _preview(anonymous, V1).status_code == 401
            assert _create(anonymous, V1).status_code == 401
            assert _reimport(anonymous, "some_skill", V1).status_code == 401


class TestPreview:
    def test_preview_lists_files_and_writes_nothing(self, client, user_skill_service, author_user):
        resp = _preview(client, V1)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skill"]["displayName"] == "Contract redline"
        assert body["source"]["type"] == "zip"
        assert body["source"]["filename"] == "bundle.zip"
        assert _statuses(body) == {
            "nda.md": "added",
            "keep.md": "added",
            "engine.py": "added",
            "assets/x.svg": "skipped",
        }
        assert body["bundleHash"]
        assert client.get("/skills/mine").json()["skills"] == []

    def test_several_skills_return_candidates(self, client):
        resp = _preview(client, {"a/s1/SKILL.md": SKILL_MD, "a/s2/SKILL.md": SKILL_MD})
        assert resp.status_code == 200
        assert resp.json()["candidates"] == ["s1", "s2"]
        assert resp.json()["skill"] is None
        assert _create(client, {"a/s1/SKILL.md": SKILL_MD, "a/s2/SKILL.md": SKILL_MD}).status_code == 400
        picked = _create(client, {"a/s1/SKILL.md": SKILL_MD, "a/s2/SKILL.md": SKILL_MD}, path="s2")
        assert picked.status_code == 200
        assert picked.json()["skill"]["importSource"]["path"] == "s2"

    def test_neither_or_both_sources_is_400(self, client):
        assert client.post("/skills/mine/import/preview", data={}).status_code == 400
        both = _zip_form(V1, url="https://github.com/a/b")
        assert client.post("/skills/mine/import/preview", **both).status_code == 400

    def test_an_oversize_upload_is_refused_before_parsing(self, client, monkeypatch):
        monkeypatch.setattr(bundle_import_routes, "MAX_UPLOAD_ARCHIVE_BYTES", 10)
        resp = _preview(client, V1)
        assert resp.status_code == 400
        assert "larger than" in resp.json()["detail"]

    def test_a_traversal_archive_is_400(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", SKILL_MD)
            info = zipfile.ZipInfo("x")
            info.filename = "../../evil.md"
            zf.writestr(info, "x")
        resp = client.post(
            "/skills/mine/import/preview", files={"file": ("b.zip", buf.getvalue(), "application/zip")}
        )
        assert resp.status_code == 400
        assert "unsafe path" in resp.json()["detail"]

    def test_preview_against_another_users_skill_is_404(
        self, client, user_skill_service, other_user, monkeypatch
    ):
        with _app(other_user, user_skill_service, monkeypatch) as theirs:
            skill_id = _create(theirs, V1).json()["skill"]["skillId"]
        assert _preview(client, V1, skillId=skill_id).status_code == 404
        assert _reimport(client, skill_id, V1).status_code == 404


class TestCreate:
    def test_create_lands_files_in_the_bundle_layout(self, client, skill_resource_store):
        resp = _create(client, V1, expectedBundleHash=_preview(client, V1).json()["bundleHash"])
        assert resp.status_code == 200, resp.text
        skill = resp.json()["skill"]
        assert skill["description"] == "Redline a contract against a playbook."
        assert skill["instructions"] == "Use the NDA playbook."
        keys = sorted(r["s3Key"] for r in skill["resources"])
        sid = skill["skillId"]
        assert keys == [
            f"skills/{sid}/references/keep.md",
            f"skills/{sid}/references/nda.md",
            f"skills/{sid}/scripts/engine.py",
        ]
        assert skill_resource_store.get(f"skills/{sid}/references/nda.md") == b"NDA rules v1"
        assert skill["importSource"]["type"] == "zip"
        assert skill["importSource"]["bundleHash"]

    def test_a_display_name_override_wins(self, client):
        skill = _create(client, V1, displayName="Contracts").json()["skill"]
        assert skill["displayName"] == "Contracts"

    def test_a_stale_hash_is_409(self, client):
        stale = _preview(client, V1).json()["bundleHash"]
        resp = _create(client, V2, expectedBundleHash=stale)
        assert resp.status_code == 409
        assert client.get("/skills/mine").json()["skills"] == []


class TestReimport:
    @pytest.fixture()
    def created(self, client):
        return _create(client, V1, displayName="My contracts").json()["skill"]

    def test_reimport_writes_changes_and_removes_what_the_bundle_dropped(
        self, client, created, skill_resource_store
    ):
        sid = created["skillId"]
        preview = _preview(client, V2, skillId=sid).json()
        assert _statuses(preview) == {
            "nda.md": "changed",
            "keep.md": "unchanged",
            "msa.md": "added",
            "engine.py": "removed",
        }
        assert preview["changes"] == {
            "description": False,
            "instructions": True,
            "allowedTools": False,
            "skillMetadata": False,
        }
        assert preview["skill"]["displayName"] == "My contracts"

        resp = _reimport(
            client,
            sid,
            V2,
            expectedBundleHash=preview["bundleHash"],
            expectedUpdatedAt=preview["targetUpdatedAt"],
        )
        assert resp.status_code == 200, resp.text
        skill = resp.json()["skill"]
        assert skill["displayName"] == "My contracts"  # the user's, not the slug's
        assert skill["instructions"] == "Use the NDA and MSA playbooks."
        assert sorted(r["filename"] for r in skill["resources"]) == ["keep.md", "msa.md", "nda.md"]
        assert skill_resource_store.get(f"skills/{sid}/references/nda.md") == b"NDA rules v2"
        with pytest.raises(Exception):
            skill_resource_store.get(f"skills/{sid}/scripts/engine.py")

    def test_unchanged_files_are_not_written_again(self, client, created, skill_resource_store, monkeypatch):
        puts = []
        original = skill_resource_store.put
        monkeypatch.setattr(
            skill_resource_store, "put", lambda **kw: puts.append(kw["filename"]) or original(**kw)
        )
        resp = _reimport(client, created["skillId"], V2)
        assert resp.status_code == 200
        assert sorted(puts) == ["msa.md", "nda.md"]

    def test_a_skill_edited_since_the_preview_is_409(self, client, created):
        sid = created["skillId"]
        preview = _preview(client, V2, skillId=sid).json()
        assert client.put(f"/skills/mine/{sid}", json={"description": "edited"}).status_code == 200
        resp = _reimport(
            client, sid, V2, expectedBundleHash=preview["bundleHash"], expectedUpdatedAt=preview["targetUpdatedAt"]
        )
        assert resp.status_code == 409
        assert client.get(f"/skills/mine/{sid}").json()["description"] == "edited"

    def test_reimport_never_cuts_a_version(
        self, client, created, user_skill_service, skill_resource_store, monkeypatch
    ):
        """Versions are cut on pin; a pinned project rolls forward only with Update (D5)."""
        monkeypatch.setattr(repository_module, "_repository_instance", user_skill_service.repository)
        monkeypatch.setattr(resource_store_module, "_store", skill_resource_store)
        sid = created["skillId"]
        repo = user_skill_service.repository

        import asyncio

        v1 = asyncio.run(pinning.pin_current(sid, pinned_by="user-author"))
        assert v1.version == 1

        assert _reimport(client, sid, V2).status_code == 200

        latest = asyncio.run(repo.get_latest_skill_version(sid))
        assert latest.version == 1
        assert latest.instructions == "Use the NDA playbook."
        # The frozen copy still serves the old bytes, and the pin sees an update.
        nda = next(r for r in latest.resources if r.filename == "nda.md")
        assert skill_resource_store.get(nda.s3_key) == b"NDA rules v1"
        status = asyncio.run(pinning.pin_status([(sid, 1)]))[sid]
        assert status.update_available is True


class TestGitHub:
    def test_a_github_url_is_fetched_and_recorded(self, client, monkeypatch):
        fetched = []

        async def fake_fetch(location):
            fetched.append(location)
            data = make_zip({f"skills-main/{k}": v for k, v in {"skills/pdf/SKILL.md": SKILL_MD, "skills/pdf/a.md": "a"}.items()})
            # GitHub's archive comment is the commit SHA.
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf, "a") as zf:
                zf.comment = b"e4bb5d960d87126ff3b3a8af1a104b7db67560ba"
            return buf.getvalue()

        monkeypatch.setattr(bundle_import_routes, "fetch_archive", fake_fetch)
        resp = client.post(
            "/skills/mine/import",
            data={"url": "https://github.com/anthropics/skills/tree/main/skills/pdf"},
        )
        assert resp.status_code == 200, resp.text
        source = resp.json()["skill"]["importSource"]
        assert source == {
            "type": "github",
            "path": "skills/pdf",
            "bundleHash": source["bundleHash"],
            "importedAt": source["importedAt"],
            "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
            "owner": "anthropics",
            "repo": "skills",
            "ref": "main",
            "commitSha": "e4bb5d960d87126ff3b3a8af1a104b7db67560ba",
            "filename": None,
            "originalName": "contract-redline",
        }
        assert fetched[0].archive_url == "https://codeload.github.com/anthropics/skills/zip/main"

    def test_a_non_github_url_is_400_and_never_fetched(self, client, monkeypatch):
        async def boom(location):  # pragma: no cover - must not run
            raise AssertionError("fetched")

        monkeypatch.setattr(bundle_import_routes, "fetch_archive", boom)
        resp = client.post("/skills/mine/import/preview", data={"url": "https://169.254.169.254/latest/meta-data"})
        assert resp.status_code == 400
