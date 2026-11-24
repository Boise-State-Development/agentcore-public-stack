"""Skill version history PR-1: every content write is kept as a version
(docs/specs/skill-version-history.md D1, D2, D6, §4.2), against moto.

What these pin:

* with the flag off, a write cuts no version (only a pin does, as before);
* with it on, each content write in both tiers cuts one version, tagged with what
  cut it, and a write that changes nothing (or only the status) cuts none;
* the content a write replaces is a version before the write runs, so the first
  write after the flag turns on keeps the old content, and a re-uploaded file's
  old bytes stay readable;
* a failed capture never fails the write, and the next write repairs the gap;
* a re-import does not run unless its checkpoint succeeded (fails closed);
* a pin after a save reuses the save's version;
* a capture that changed no file makes no S3 call.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import List

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.skills import history
from apis.shared.skills import pinning
from apis.shared.skills import repository as repository_module
from apis.shared.skills import resource_store as resource_store_module
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillDefinition, SkillImportSource
from apis.shared.skills.versions import SkillVersion, content_hash

from .conftest import AWS_REGION, TABLE

SKILL_MD = """---
name: contract-redline
description: Redline a contract against a playbook.
---

Use the NDA playbook.
"""


def run(coro):
    return asyncio.run(coro)


def make_zip(files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def versions_of(skill_id: str) -> List[SkillVersion]:
    """Every version of a skill, oldest first (PR-2 adds the repository's list)."""
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE)
    items = table.query(
        KeyConditionExpression=Key("PK").eq(f"SKILL#{skill_id}") & Key("SK").begins_with("VERSION#")
    )["Items"]
    return [SkillVersion.from_dynamo_item(i) for i in items]


def triggers(skill_id: str) -> List[str]:
    return [v.trigger for v in versions_of(skill_id)]


@pytest.fixture()
def history_on(monkeypatch):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")


@pytest.fixture()
def singletons(skill_service, monkeypatch):
    """Point ``pin_current``'s getters at the moto-backed repository and store."""
    monkeypatch.setattr(repository_module, "_repository_instance", skill_service.repository)
    monkeypatch.setattr(resource_store_module, "_store", skill_service.resource_store)


def _create(user_skill_service, author_user, **kw) -> SkillDefinition:
    return run(
        user_skill_service.create_my_skill(
            author_user,
            display_name=kw.pop("display_name", "Grant Review"),
            description=kw.pop("description", "Review a grant proposal."),
            instructions=kw.pop("instructions", "Read it closely."),
            **kw,
        )
    )


class TestFlagOff:
    def test_writes_cut_no_version(self, user_skill_service, author_user, monkeypatch):
        monkeypatch.delenv("SKILL_VERSION_HISTORY_ENABLED", raising=False)
        skill = _create(user_skill_service, author_user)
        run(user_skill_service.update_my_skill(skill.skill_id, {"instructions": "v2"}, author_user))
        run(user_skill_service.write_text_file(skill.skill_id, "reference", "a.md", "a", author_user))
        assert versions_of(skill.skill_id) == []


@pytest.mark.usefixtures("history_on")
class TestUserTier:
    def test_each_content_write_cuts_one_version(self, user_skill_service, author_user):
        skill = _create(user_skill_service, author_user)
        sid = skill.skill_id
        run(user_skill_service.update_my_skill(sid, {"instructions": "v2"}, author_user))
        run(user_skill_service.write_text_file(sid, "reference", "a.md", "alpha", author_user))
        run(user_skill_service.delete_resource(sid, "a.md", author_user))
        assert triggers(sid) == ["create", "save", "file", "file"]
        assert [v.version for v in versions_of(sid)] == [1, 2, 3, 4]
        latest = versions_of(sid)[-1]
        live = run(user_skill_service.get_my_skill(sid, author_user))
        assert latest.content_hash == content_hash(live)
        assert latest.created_by == author_user.user_id

    def test_no_op_and_status_only_writes_cut_nothing(self, user_skill_service, author_user):
        skill = _create(user_skill_service, author_user, instructions="same")
        sid = skill.skill_id
        run(user_skill_service.update_my_skill(sid, {"instructions": "same"}, author_user))
        run(user_skill_service.update_my_skill(sid, {"category": "research"}, author_user))
        assert triggers(sid) == ["create"]

    def test_the_authoring_trigger_is_recorded(self, user_skill_service, author_user):
        skill = _create(user_skill_service, author_user, trigger="authoring")
        run(
            user_skill_service.write_text_file(
                skill.skill_id, "reference", "a.md", "a", author_user, trigger="authoring"
            )
        )
        assert triggers(skill.skill_id) == ["authoring", "authoring"]

    def test_a_re_uploaded_file_keeps_its_old_bytes(
        self, user_skill_service, skill_resource_store, author_user
    ):
        sid = _create(user_skill_service, author_user).skill_id
        run(user_skill_service.write_text_file(sid, "reference", "nda.md", "NDA v1", author_user))
        run(user_skill_service.write_text_file(sid, "reference", "nda.md", "NDA v2", author_user))
        first, second = versions_of(sid)[-2:]
        assert skill_resource_store.get(first.resources[0].s3_key) == b"NDA v1"
        assert skill_resource_store.get(second.resources[0].s3_key) == b"NDA v2"


class TestCheckpoint:
    def test_the_first_write_after_turning_on_keeps_the_old_content(
        self, user_skill_service, author_user, monkeypatch
    ):
        monkeypatch.delenv("SKILL_VERSION_HISTORY_ENABLED", raising=False)
        skill = _create(user_skill_service, author_user, instructions="written before")
        monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
        run(user_skill_service.update_my_skill(skill.skill_id, {"instructions": "after"}, author_user))
        old, new = versions_of(skill.skill_id)
        assert (old.trigger, old.instructions) == ("checkpoint", "written before")
        assert (new.trigger, new.instructions) == ("save", "after")

    def test_a_failed_capture_never_fails_the_write_and_is_repaired(
        self, user_skill_service, author_user, monkeypatch, history_on
    ):
        sid = _create(user_skill_service, author_user).skill_id
        real = user_skill_service.repository.put_skill_version

        async def broken(version):
            raise RuntimeError("dynamodb down")

        monkeypatch.setattr(user_skill_service.repository, "put_skill_version", broken)
        updated = run(user_skill_service.update_my_skill(sid, {"instructions": "lost?"}, author_user))
        assert updated.instructions == "lost?"
        assert triggers(sid) == ["create"]

        monkeypatch.setattr(user_skill_service.repository, "put_skill_version", real)
        run(user_skill_service.update_my_skill(sid, {"instructions": "next"}, author_user))
        assert [v.instructions for v in versions_of(sid)] == ["Read it closely.", "lost?", "next"]
        assert triggers(sid) == ["create", "checkpoint", "save"]

    def test_a_reimport_does_not_run_without_its_checkpoint(
        self, user_skill_service, author_user, monkeypatch, history_on
    ):
        sid = _create(user_skill_service, author_user, instructions="keep me").skill_id
        monkeypatch.delenv("SKILL_VERSION_HISTORY_ENABLED")
        run(user_skill_service.update_my_skill(sid, {"instructions": "not yet a version"}, author_user))
        monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")

        async def broken(*args, **kwargs):
            raise RuntimeError("s3 down")

        monkeypatch.setattr(history, "capture_current", broken)
        bundle = load_bundle(make_zip({"contract-redline/SKILL.md": SKILL_MD})).bundle
        source = SkillImportSource(type="zip", path="", bundle_hash="h", imported_at="2026-11-23")
        with pytest.raises(history.SkillCheckpointError):
            run(user_skill_service.import_bundle(author_user, bundle, source=source, skill_id=sid))
        live = run(user_skill_service.get_my_skill(sid, author_user))
        assert live.instructions == "not yet a version"


@pytest.mark.usefixtures("history_on")
class TestImport:
    def test_import_and_reimport_keep_the_full_source(self, user_skill_service, author_user):
        bundle = load_bundle(make_zip({"contract-redline/SKILL.md": SKILL_MD})).bundle
        source = SkillImportSource(
            type="github",
            path="skills/redline",
            bundle_hash=bundle.bundle_hash,
            imported_at="2026-11-23",
            url="https://github.com/acme/skills/tree/main/skills/redline",
            owner="acme",
            repo="skills",
            ref="main",
            commit_sha="a" * 40,
        )
        created = run(user_skill_service.import_bundle(author_user, bundle, source=source))
        changed = load_bundle(
            make_zip({"contract-redline/SKILL.md": SKILL_MD.replace("NDA", "MSA")})
        ).bundle
        run(
            user_skill_service.import_bundle(
                author_user, changed, source=source, skill_id=created.skill_id
            )
        )
        first, second = versions_of(created.skill_id)
        assert (first.trigger, second.trigger) == ("import", "reimport")
        assert first.import_source_record.url == source.url
        assert first.import_source == {
            "type": "github",
            "owner": "acme",
            "repo": "skills",
            "ref": "main",
            "commitSha": "a" * 40,
        }
        assert "MSA" in second.instructions


@pytest.mark.usefixtures("history_on")
class TestAdminTier:
    def test_each_catalog_write_cuts_one_version(self, skill_service, admin_user):
        skill = SkillDefinition(
            skill_id="pdf_tools", display_name="PDF", description="PDFs.", instructions=""
        )
        run(skill_service.create_skill(skill, admin_user))
        run(skill_service.update_skill("pdf_tools", {"instructions": "Merge."}, admin_user))
        run(skill_service.update_skill("pdf_tools", {"status": "draft"}, admin_user))
        run(
            skill_service.add_resource(
                "pdf_tools", "merge.md", b"how to merge", "text/markdown", admin_user
            )
        )
        run(skill_service.delete_resource("pdf_tools", "merge.md", admin_user))
        assert triggers("pdf_tools") == ["create", "save", "file", "file"]
        assert all(v.created_by == admin_user.user_id for v in versions_of("pdf_tools"))

    def test_catalog_import_and_reimport(self, skill_service, admin_user):
        bundle = load_bundle(make_zip({"contract-redline/SKILL.md": SKILL_MD})).bundle
        source = SkillImportSource(type="zip", path="", bundle_hash="h", imported_at="2026-11-23")
        created = run(skill_service.import_bundle(admin_user, bundle, source=source))
        changed = load_bundle(
            make_zip({"contract-redline/SKILL.md": SKILL_MD.replace("NDA", "MSA")})
        ).bundle
        run(skill_service.import_bundle(admin_user, changed, source=source, skill_id=created.skill_id))
        assert triggers(created.skill_id) == ["import", "reimport"]


@pytest.mark.usefixtures("history_on", "singletons")
class TestPinsAndCost:
    def test_a_pin_after_a_save_reuses_its_version(self, user_skill_service, author_user):
        sid = _create(user_skill_service, author_user).skill_id
        run(user_skill_service.update_my_skill(sid, {"instructions": "v2"}, author_user))
        pinned = run(pinning.pin_current(sid, pinned_by="someone-else"))
        assert pinned.version == 2
        assert pinned.trigger == "save"
        assert len(versions_of(sid)) == 2

    def test_a_capture_that_changed_no_file_makes_no_s3_call(
        self, user_skill_service, skill_resource_store, author_user, monkeypatch
    ):
        sid = _create(user_skill_service, author_user).skill_id
        run(user_skill_service.write_text_file(sid, "reference", "a.md", "a", author_user))
        run(user_skill_service.write_text_file(sid, "reference", "b.md", "b", author_user))
        calls = []
        real = skill_resource_store.freeze

        def counting(**kwargs):
            calls.append(kwargs["s3_key"])
            return real(**kwargs)

        monkeypatch.setattr(skill_resource_store, "freeze", counting)
        run(user_skill_service.update_my_skill(sid, {"instructions": "only this"}, author_user))
        assert calls == []
        run(user_skill_service.write_text_file(sid, "reference", "a.md", "a changed", author_user))
        assert len(calls) == 1
