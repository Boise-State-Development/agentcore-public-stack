"""Skill export (agentskills.io bundle as a zip): the builder and both routes.

What these pin:

- ``import(export(x)) == x``: the import parser reads an export back to the same
  description, instructions, advisory tools, metadata (allowing for the documented
  fold of non-standard keys under ``metadata``) and file set, for fixture skills
  and the seed skill.
- The exported ``SKILL.md`` passes the strict validator, its ``name`` is its
  directory, and Strands loads the unzipped bundle with ``strict=True``.
- An unchanged skill exports to identical bytes.
- Only the owner (user route) or a catalog admin (admin route, catalog skills
  only) can export; every other caller gets the same 404 as a missing skill, and
  an unauthenticated request is refused before any lookup.
- A missing file is 409 naming it, never a partial zip; ``?version=n`` exports the
  frozen version; the zip is built off the event loop.
"""

from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as admin_skill_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills import pinning
from apis.shared.skills import repository as repository_module
from apis.shared.skills import resource_store as resource_store_module
from apis.shared.skills.access import resolve_invocable_skill_ids
from apis.shared.skills.bundle import skill_md_frontmatter
from apis.shared.skills.bundle_export import (
    SkillExportFileError,
    build_skill_zip,
    export_slug,
)
from apis.shared.skills.bundle_files import MAX_RESOURCE_BYTES, MAX_RESOURCES_PER_SKILL
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillDefinition, SkillResourceRef
from apis.shared.skills.resource_store import compute_content_hash
from apis.shared.skills.skill_md import parse_skill_md, validate_frontmatter
from strands.vended_plugins.skills.skill import Skill
from tests.conftest import override_admin_auth

FIELD_SET = b"""---
title: Award
version: 1
---

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| total | Total | currency | Total awarded amount | | no |
"""

SEED_DIR = Path(__file__).resolve().parents[4] / "scripts" / "seed_skills"


# =============================================================================
# Fixtures: records and their bytes
# =============================================================================


def _ref(skill_id: str, filename: str, kind: str, content: bytes, content_type: str):
    return SkillResourceRef(
        filename=filename,
        content_hash=compute_content_hash(content),
        size=len(content),
        content_type=content_type,
        s3_key=f"skills/{skill_id}/{kind}/{filename}",
        kind=kind,
    )


def _record(skill_id: str, files=(), **kw) -> tuple[SkillDefinition, dict[str, bytes]]:
    """A record and the store behind it; ``files`` are ``(name, kind, bytes, type)``."""
    refs, blobs = [], {}
    for filename, kind, content, content_type in files:
        ref = _ref(skill_id, filename, kind, content, content_type)
        refs.append(ref)
        blobs[ref.s3_key] = content
    record = SkillDefinition(
        skill_id=skill_id,
        display_name=kw.pop("display_name", "A skill"),
        description=kw.pop("description", "Do a thing when asked to."),
        instructions=kw.pop("instructions", "# Steps\n\nDo it."),
        resources=refs,
        owner_id=kw.pop("owner_id", "system"),
        **kw,
    )
    return record, blobs


def _minimal():
    return _record("plain_skill")


def _full():
    return _record(
        "pdf_workflows",
        files=[
            ("forms.md", "reference", b"# Forms\n\nFill them.", "text/markdown"),
            ("field-set.award.md", "reference", FIELD_SET, "text/markdown"),
            ("fill.py", "script", b"print('fill')\n", "text/x-python"),
            ("logo.png", "asset", b"\x89PNG\r\n\x1a\n" + bytes(range(256)), "image/png"),
        ],
        description="Fill, merge and split PDFs.",
        instructions="# PDF Workflows\n\nRead references/forms.md first.",
        allowed_tools=["Read", "Bash"],
        skill_metadata={
            "license": "Apache-2.0",
            "compatibility": "Needs a PDF library",
            "metadata": {"author": "web team", "version": "2"},
            "category-hint": "documents",
            "tags": ["pdf", "forms"],
        },
    )


def _seed(directory: Path):
    """The seed skill as an import would store it: zip the directory, parse it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, f"{directory.name}/{path.relative_to(directory)}")
    bundle = load_bundle(buffer.getvalue()).bundle
    assert bundle is not None and not bundle.skipped
    return _record(
        directory.name,
        files=[(f.filename, f.kind, f.content, f.content_type) for f in bundle.files],
        display_name=bundle.display_name,
        description=bundle.description,
        instructions=bundle.instructions,
        allowed_tools=bundle.allowed_tools,
        skill_metadata=bundle.skill_metadata,
    )


SEED_SKILLS = sorted(p for p in SEED_DIR.iterdir() if (p / "SKILL.md").is_file())
FIXTURES = [_minimal, _full] + [lambda d=d: _seed(d) for d in SEED_SKILLS]
FIXTURE_IDS = ["minimal", "full"] + [f"seed:{d.name}" for d in SEED_SKILLS]


def _export(record, blobs) -> bytes:
    return build_skill_zip(record, blobs.__getitem__)


def _expected_metadata(record: SkillDefinition) -> dict:
    """What an import stores as ``skill_metadata``: the exported frontmatter's other keys."""
    frontmatter = skill_md_frontmatter(
        skill_id=record.skill_id,
        description=record.description,
        allowed_tools=list(record.allowed_tools),
        skill_metadata=dict(record.skill_metadata),
    )
    return {k: v for k, v in frontmatter.items() if k not in ("name", "description", "allowed-tools")}


def test_there_is_a_seed_skill_to_round_trip():
    assert SEED_SKILLS, "expected at least one bundle under scripts/seed_skills/"


# =============================================================================
# The builder
# =============================================================================


@pytest.mark.parametrize("make", FIXTURES, ids=FIXTURE_IDS)
class TestRoundTrip:
    def test_import_reads_back_the_same_skill(self, make):
        record, blobs = make()
        bundle = load_bundle(_export(record, blobs)).bundle

        assert bundle is not None
        assert bundle.skipped == []
        assert bundle.root == ""  # the single top folder is stripped
        assert bundle.name == export_slug(record.skill_id)
        assert bundle.description == record.description
        assert bundle.instructions == record.instructions.strip()
        assert bundle.allowed_tools == list(record.allowed_tools)
        assert bundle.skill_metadata == _expected_metadata(record)
        assert {(f.filename, f.kind, f.content_hash) for f in bundle.files} == {
            (r.filename, r.kind, r.content_hash) for r in record.resources
        }

    def test_skill_md_is_strictly_valid_and_named_after_its_directory(self, make):
        record, blobs = make()
        with zipfile.ZipFile(io.BytesIO(_export(record, blobs))) as archive:
            names = archive.namelist()
            top = {n.split("/", 1)[0] for n in names}
            assert top == {export_slug(record.skill_id)}
            text = archive.read(f"{export_slug(record.skill_id)}/SKILL.md").decode("utf-8")
        parsed = parse_skill_md(text)
        assert [d for d in validate_frontmatter(parsed.frontmatter, strict=True) if d.is_error] == []
        assert parsed.frontmatter["name"] == next(iter(top))

    def test_strands_loads_it_strictly(self, make, tmp_path):
        record, blobs = make()
        with zipfile.ZipFile(io.BytesIO(_export(record, blobs))) as archive:
            archive.extractall(tmp_path)
        skill = Skill.from_file(tmp_path / export_slug(record.skill_id), strict=True)
        assert skill.name == export_slug(record.skill_id)
        assert skill.description == record.description

    def test_two_exports_are_byte_identical(self, make):
        record, blobs = make()
        assert _export(record, blobs) == _export(record, blobs)


class TestLayout:
    def test_entries_are_sorted_under_kind_directories(self):
        record, blobs = _full()
        with zipfile.ZipFile(io.BytesIO(_export(record, blobs))) as archive:
            names = archive.namelist()
            infos = archive.infolist()
        assert names == [
            "pdf-workflows/SKILL.md",
            "pdf-workflows/assets/logo.png",
            "pdf-workflows/references/field-set.award.md",
            "pdf-workflows/references/forms.md",
            "pdf-workflows/scripts/fill.py",
        ]
        assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
        modes = {i.filename.rsplit("/", 1)[-1]: (i.external_attr >> 16) & 0o777 for i in infos}
        assert modes["fill.py"] == 0o755
        assert {m for n, m in modes.items() if n != "fill.py"} == {0o644}

    def test_skill_md_comes_from_the_row_not_from_storage(self):
        record, blobs = _full()
        # A stale projection in storage is never read: nothing names it.
        blobs["skills/pdf_workflows/SKILL.md"] = b"---\nname: stale\n---\nold"
        with zipfile.ZipFile(io.BytesIO(_export(record, blobs))) as archive:
            text = archive.read("pdf-workflows/SKILL.md").decode()
        assert "stale" not in text and "Read references/forms.md first." in text

    def test_reordering_the_manifest_does_not_change_the_bytes(self):
        record, blobs = _full()
        shuffled = record.model_copy(update={"resources": list(reversed(record.resources))})
        assert _export(record, blobs) == _export(shuffled, blobs)


class TestUnreadableFile:
    def test_a_missing_file_fails_the_export_naming_it(self):
        record, blobs = _full()
        del blobs["skills/pdf_workflows/script/fill.py"]
        with pytest.raises(SkillExportFileError) as raised:
            _export(record, blobs)
        assert raised.value.filename == "fill.py"
        assert "fill.py" in str(raised.value)


# =============================================================================
# The routes
# =============================================================================


def _user_app(user, service, monkeypatch):
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: service)
    app = FastAPI()
    app.include_router(my_skill_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


def _members(resp) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _create_owned(user_skill_service, user, **kw):
    skill = asyncio.run(
        user_skill_service.create_my_skill(
            user,
            display_name=kw.get("display_name", "Contract redline"),
            description="Redline a contract against a playbook.",
            instructions="Use the NDA playbook.",
            allowed_tools=["Read"],
            skill_metadata={"license": "MIT"},
        )
    )
    asyncio.run(
        user_skill_service.add_resource(
            skill.skill_id, "nda.md", b"NDA rules v1", "text/markdown", user
        )
    )
    asyncio.run(
        user_skill_service.add_resource(
            skill.skill_id, "engine.py", b"print('v1')\n", "text/x-python", user, kind="script"
        )
    )
    return skill.skill_id


class TestUserRoute:
    def test_owner_downloads_a_conformant_zip(self, user_skill_service, author_user, monkeypatch):
        sid = _create_owned(user_skill_service, author_user)
        with _user_app(author_user, user_skill_service, monkeypatch) as client:
            resp = client.get(f"/skills/mine/{sid}/export")
        assert resp.status_code == 200, resp.text
        slug = export_slug(sid)
        assert resp.headers["content-type"] == "application/zip"
        assert f'attachment; filename="{slug}.zip"' in resp.headers["content-disposition"]
        assert resp.headers["x-content-type-options"] == "nosniff"
        members = _members(resp)
        assert sorted(members) == [
            f"{slug}/SKILL.md",
            f"{slug}/references/nda.md",
            f"{slug}/scripts/engine.py",
        ]
        bundle = load_bundle(resp.content).bundle
        assert bundle.description == "Redline a contract against a playbook."
        assert bundle.allowed_tools == ["Read"]
        assert bundle.skill_metadata == {"license": "MIT"}
        assert bundle.skipped == []

    def test_two_downloads_are_byte_identical(self, user_skill_service, author_user, monkeypatch):
        sid = _create_owned(user_skill_service, author_user)
        with _user_app(author_user, user_skill_service, monkeypatch) as client:
            first = client.get(f"/skills/mine/{sid}/export").content
            second = client.get(f"/skills/mine/{sid}/export").content
        assert first == second

    def test_a_user_who_can_use_it_through_an_agent_gets_404(
        self, user_skill_service, author_user, other_user, monkeypatch
    ):
        sid = _create_owned(user_skill_service, author_user)
        # The other user can *run* it through an Agent the author shared (invoke-through)…
        usable = asyncio.run(
            resolve_invocable_skill_ids(other_user, [sid], agent_owner_id=author_user.user_id)
        )
        assert sid in usable
        # …but cannot export it, and the refusal reads exactly like a missing skill.
        with _user_app(other_user, user_skill_service, monkeypatch) as client:
            theirs = client.get(f"/skills/mine/{sid}/export")
            missing = client.get("/skills/mine/no_such_skill/export")
        assert theirs.status_code == missing.status_code == 404
        assert theirs.json()["detail"] == f"Skill '{sid}' not found"
        assert missing.json()["detail"] == "Skill 'no_such_skill' not found"

    def test_a_catalog_skill_is_404_on_the_user_route(
        self, skill_service, user_skill_service, admin_user, author_user, monkeypatch
    ):
        catalog, _ = _record("catalog_skill")
        asyncio.run(skill_service.create_skill(catalog, admin_user))
        with _user_app(author_user, user_skill_service, monkeypatch) as client:
            assert client.get("/skills/mine/catalog_skill/export").status_code == 404

    def test_unauthenticated_is_refused_before_any_lookup(self, monkeypatch):
        looked_up = []

        async def require_owned(skill_id, user):
            looked_up.append(skill_id)
            raise AssertionError("looked up before authenticating")

        service = SimpleNamespace(require_owned=require_owned)
        with _user_app(None, service, monkeypatch) as client:
            resp = client.get("/skills/mine/anything/export")
        assert resp.status_code == 401
        assert looked_up == []

    def test_a_missing_file_is_409_and_no_zip(
        self, user_skill_service, skill_resource_store, author_user, monkeypatch
    ):
        sid = _create_owned(user_skill_service, author_user)
        skill = asyncio.run(user_skill_service.require_owned(sid, author_user))
        script = next(r for r in skill.resources if r.filename == "engine.py")
        skill_resource_store._client().delete_object(
            Bucket=skill_resource_store.bucket_name, Key=script.s3_key
        )
        with _user_app(author_user, user_skill_service, monkeypatch) as client:
            resp = client.get(f"/skills/mine/{sid}/export")
        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/json")
        assert "engine.py" in resp.json()["detail"]

    def test_version_exports_the_frozen_content(
        self, user_skill_service, skill_resource_store, author_user, monkeypatch
    ):
        sid = _create_owned(user_skill_service, author_user)
        monkeypatch.setattr(repository_module, "_repository_instance", user_skill_service.repository)
        monkeypatch.setattr(resource_store_module, "_store", skill_resource_store)
        v1 = asyncio.run(pinning.pin_current(sid, pinned_by=author_user.user_id))

        # The live skill moves on: new instructions, a file rewritten in place, one removed.
        asyncio.run(
            user_skill_service.update_my_skill(sid, {"instructions": "Use the MSA playbook."}, author_user)
        )
        asyncio.run(
            user_skill_service.add_resource(sid, "nda.md", b"NDA rules v2", "text/markdown", author_user)
        )
        asyncio.run(user_skill_service.delete_resource(sid, "engine.py", author_user))

        slug = export_slug(sid)
        with _user_app(author_user, user_skill_service, monkeypatch) as client:
            frozen = client.get(f"/skills/mine/{sid}/export?version={v1.version}")
            live = client.get(f"/skills/mine/{sid}/export")
            absent = client.get(f"/skills/mine/{sid}/export?version={v1.version + 41}")
        assert frozen.status_code == 200, frozen.text
        old = _members(frozen)
        assert old[f"{slug}/references/nda.md"] == b"NDA rules v1"
        assert old[f"{slug}/scripts/engine.py"] == b"print('v1')\n"
        assert b"Use the NDA playbook." in old[f"{slug}/SKILL.md"]
        new = _members(live)
        assert new[f"{slug}/references/nda.md"] == b"NDA rules v2"
        assert f"{slug}/scripts/engine.py" not in new
        assert b"Use the MSA playbook." in new[f"{slug}/SKILL.md"]
        assert absent.status_code == 404

    def test_the_largest_skill_is_built_off_the_event_loop(self, author_user, monkeypatch):
        sid = "largest_skill"
        files = [
            (f"file-{i:02d}.md", "reference", bytes([65 + i % 26]) * MAX_RESOURCE_BYTES, "text/markdown")
            for i in range(MAX_RESOURCES_PER_SKILL)
        ]
        record, blobs = _record(sid, files=files, owner_id=author_user.user_id)
        reader_threads = set()

        def get(key):
            reader_threads.add(threading.current_thread() is threading.main_thread())
            return blobs[key]

        async def require_owned(skill_id, user):
            return record

        offloaded = []
        real_run_blocking = my_skill_routes.run_blocking

        async def spy_run_blocking(fn, *args, **kwargs):
            offloaded.append(fn)
            return await real_run_blocking(fn, *args, **kwargs)

        monkeypatch.setattr(my_skill_routes, "run_blocking", spy_run_blocking)
        service = SimpleNamespace(
            require_owned=require_owned,
            repository=None,
            bundle_files=SimpleNamespace(resource_store=SimpleNamespace(get=get)),
        )
        with _user_app(author_user, service, monkeypatch) as client:
            resp = client.get(f"/skills/mine/{sid}/export")
        assert resp.status_code == 200
        assert offloaded == [build_skill_zip]
        # Every S3 read ran in the worker thread, never on the loop's thread.
        assert reader_threads == {False}
        assert len(_members(resp)) == MAX_RESOURCES_PER_SKILL + 1

    def test_not_behind_the_bundle_import_flag(self, user_skill_service, author_user, monkeypatch):
        monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "")
        sid = _create_owned(user_skill_service, author_user)
        with _user_app(author_user, user_skill_service, monkeypatch) as client:
            assert client.get(f"/skills/mine/{sid}/export").status_code == 200


class TestAdminRoute:
    @pytest.fixture()
    def admin_client(self, skill_service, admin_user, monkeypatch):
        monkeypatch.setattr(admin_skill_routes, "get_skill_catalog_service", lambda: skill_service)
        app = FastAPI()
        app.include_router(admin_skill_routes.router)
        override_admin_auth(app, lambda: admin_user)
        return TestClient(app)

    def test_admin_exports_a_catalog_skill(self, admin_client):
        created = admin_client.post(
            "/skills/",
            json={
                "skillId": "pdf_workflows",
                "displayName": "PDF Workflows",
                "description": "Fill, merge and split PDFs.",
                "instructions": "# PDF Workflows",
            },
        )
        assert created.status_code == 200, created.text
        upload = admin_client.post(
            "/skills/pdf_workflows/resources",
            files={"file": ("forms.md", b"# Forms", "text/markdown")},
        )
        assert upload.status_code == 200, upload.text
        resp = admin_client.get("/skills/pdf_workflows/export")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/zip"
        assert 'filename="pdf-workflows.zip"' in resp.headers["content-disposition"]
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert sorted(_members(resp)) == [
            "pdf-workflows/SKILL.md",
            "pdf-workflows/references/forms.md",
        ]
        assert admin_client.get("/skills/pdf_workflows/export?version=1").status_code == 404

    def test_admin_gets_404_for_a_user_tier_skill(
        self, admin_client, user_skill_service, author_user
    ):
        sid = _create_owned(user_skill_service, author_user)
        theirs = admin_client.get(f"/skills/{sid}/export")
        missing = admin_client.get("/skills/no_such_skill/export")
        assert theirs.status_code == missing.status_code == 404
