"""Skill versions and pins (shared-projects 3.1), against moto DynamoDB and S3.

A pin freezes a skill's content: its instructions in a ``VERSION#`` row and its
reference files in content-addressed copies, so neither an edit nor a re-upload
changes what a pinned binding runs. The live row still decides whether it runs.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import boto3
import pytest

from apis.shared.skills import pinning
from apis.shared.skills import repository as repository_module
from apis.shared.skills import resource_store as resource_store_module
from apis.shared.skills.models import SkillDefinition, SkillResourceRef, SkillStatus
from apis.shared.skills.pins import base_skill_id, binding_pin, pinned_ref, split_pinned_ref
from apis.shared.skills.repository import SkillCatalogRepository
from apis.shared.skills.resource_store import (
    SkillResourceChangedError,
    SkillResourceStore,
    compute_content_hash,
    version_blob_key,
)
from apis.shared.skills.versions import content_hash

BUCKET = "test-skill-resources"


@pytest.fixture()
def repo(roles_table, monkeypatch) -> SkillCatalogRepository:
    repo = SkillCatalogRepository(table_name="test-app-roles")
    monkeypatch.setattr(repository_module, "_repository_instance", repo)
    return repo


@pytest.fixture()
def store(aws, monkeypatch) -> SkillResourceStore:
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=BUCKET)
    store = SkillResourceStore(bucket_name=BUCKET, s3_client=s3)
    monkeypatch.setattr(resource_store_module, "_store", store)
    return store


async def _skill(repo, skill_id="pdf_workflows", **kw) -> SkillDefinition:
    defaults = dict(
        skill_id=skill_id, display_name="PDF Workflows", description="Fill PDFs.", instructions="Use pypdf."
    )
    defaults.update(kw)
    return await repo.create_skill(SkillDefinition(**defaults))


async def _upload(repo, store, skill_id: str, filename: str, body: bytes) -> None:
    key = store.put(skill_id=skill_id, filename=filename, content=body, content_type="text/markdown")
    ref = SkillResourceRef(
        filename=filename, content_hash=compute_content_hash(body), size=len(body),
        content_type="text/markdown", s3_key=key,
    )
    skill = await repo.get_skill(skill_id)
    await repo.update_skill(skill_id, {"resources": [r for r in skill.resources if r.filename != filename] + [ref]})


# --- The run-time id ---------------------------------------------------------------


@pytest.mark.parametrize(
    "ref,expected",
    [("pdf_workflows", ("pdf_workflows", None)), ("pdf_workflows@3", ("pdf_workflows", 3)),
     ("pdf_workflows@0", ("pdf_workflows@0", None)), ("pdf_workflows@x", ("pdf_workflows@x", None))],
)
def test_split_pinned_ref(ref, expected):
    assert split_pinned_ref(ref) == expected


def test_pinned_ref_round_trips_and_an_unpinned_ref_is_the_bare_id():
    assert pinned_ref("pdf_workflows", 3) == "pdf_workflows@3"
    assert pinned_ref("pdf_workflows", None) == "pdf_workflows"
    assert base_skill_id("pdf_workflows@3") == "pdf_workflows"


@pytest.mark.parametrize(
    "config,expected",
    [({"version": 2}, 2), ({"version": Decimal(2)}, 2), ({}, None), (None, None), ({"version": 0}, None),
     ({"version": "2"}, None), ({"version": True}, None), ({"version": 2.5}, None)],
)
def test_binding_pin_reads_only_a_positive_whole_number(config, expected):
    assert binding_pin(config) == expected


# --- Cutting versions ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_cuts_a_version_once_and_reuses_it_until_the_skill_changes(repo):
    await _skill(repo)
    first = await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    again = await pinning.pin_current("pdf_workflows", pinned_by="u-2")
    assert (first.version, again.version, again.created_by) == (1, 1, "u-1")

    await repo.update_skill("pdf_workflows", {"status": SkillStatus.DISABLED})
    assert (await pinning.pin_current("pdf_workflows", pinned_by="u-2")).version == 1  # status isn't content

    await repo.update_skill("pdf_workflows", {"instructions": "Use pdfplumber."})
    second = await pinning.pin_current("pdf_workflows", pinned_by="u-2")
    assert (second.version, second.instructions) == (2, "Use pdfplumber.")


@pytest.mark.asyncio
async def test_pinning_a_missing_skill_is_refused(repo):
    with pytest.raises(pinning.SkillPinError):
        await pinning.pin_current("nope_nope", pinned_by="u-1")


@pytest.mark.asyncio
async def test_versions_never_list_as_skills(repo):
    await _skill(repo, owner_id="u-1")
    await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    assert [s.skill_id for s in await repo.list_skills()] == ["pdf_workflows"]
    assert [s.skill_id for s in await repo.list_skills_by_owner("u-1")] == ["pdf_workflows"]


@pytest.mark.asyncio
async def test_a_pinned_file_survives_a_re_upload(repo, store):
    await _skill(repo)
    await _upload(repo, store, "pdf_workflows", "forms.md", b"old forms")
    version = await pinning.pin_current("pdf_workflows", pinned_by="u-1")

    frozen = version.resources[0].s3_key
    assert frozen == version_blob_key("pdf_workflows", compute_content_hash(b"old forms"))
    await _upload(repo, store, "pdf_workflows", "forms.md", b"new forms")
    assert store.get(frozen) == b"old forms"

    # The new upload is new content: the next pin is a new version with its own copy.
    newer = await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    assert (newer.version, store.get(newer.resources[0].s3_key)) == (2, b"new forms")


@pytest.mark.asyncio
async def test_a_file_replaced_mid_pin_is_not_frozen(repo, store):
    await _skill(repo)
    await _upload(repo, store, "pdf_workflows", "forms.md", b"forms")
    # The bytes change under the manifest, as a concurrent re-upload would.
    store.put(skill_id="pdf_workflows", filename="forms.md", content=b"other", content_type="text/markdown")
    with pytest.raises(SkillResourceChangedError):
        await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    assert await repo.get_latest_skill_version("pdf_workflows") is None


def test_content_hash_ignores_where_a_file_is_stored():
    ref = dict(filename="forms.md", content_hash="abc", size=1, content_type="text/markdown")
    live = SkillDefinition(skill_id="pdf_workflows", display_name="P", description="", instructions="",
                           resources=[SkillResourceRef(s3_key="skills/pdf_workflows/references/forms.md", **ref)])
    frozen = live.model_copy(update={"resources": [SkillResourceRef(s3_key="skill-versions/pdf_workflows/abc", **ref)]})
    assert content_hash(live) == content_hash(frozen)


# --- What a turn runs ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pinned_ref_runs_the_version_and_an_unpinned_one_runs_live(repo):
    await _skill(repo)
    await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    await repo.update_skill("pdf_workflows", {"instructions": "Use pdfplumber.", "description": "Newer."})

    [pinned] = await pinning.load_skill_records(["pdf_workflows@1"])
    [live] = await pinning.load_skill_records(["pdf_workflows"])
    assert (pinned.instructions, pinned.description) == ("Use pypdf.", "Fill PDFs.")
    assert (live.instructions, live.description) == ("Use pdfplumber.", "Newer.")


@pytest.mark.asyncio
async def test_the_live_row_still_decides_whether_a_pin_runs(repo):
    from agents.main_agent.skills.strands_mapping import fetch_active_skill_records

    await _skill(repo)
    await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    assert [r.instructions for r in fetch_active_skill_records(["pdf_workflows@1"])] == ["Use pypdf."]

    await repo.update_skill("pdf_workflows", {"status": SkillStatus.DISABLED})
    assert fetch_active_skill_records(["pdf_workflows@1"]) == []


@pytest.mark.asyncio
async def test_a_pin_to_a_missing_version_is_left_out_not_run_live(repo):
    await _skill(repo)
    assert await pinning.load_skill_records(["pdf_workflows@4", "pdf_workflows"]) != []
    assert [r.skill_id for r in await pinning.load_skill_records(["pdf_workflows@4"])] == []


@pytest.mark.asyncio
async def test_pin_status_reports_a_newer_version(repo):
    await _skill(repo)
    await _skill(repo, skill_id="other_skill")
    await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    status = await pinning.pin_status([("pdf_workflows", 1), ("other_skill", None)])
    assert (status["pdf_workflows"].update_available, status["other_skill"].version) == (False, None)

    await repo.update_skill("pdf_workflows", {"instructions": "Use pdfplumber."})
    assert (await pinning.pin_status([("pdf_workflows", 1)]))["pdf_workflows"].update_available is True


# --- Deleting a skill ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_deleting_a_skill_drops_its_versions_and_frozen_files(repo, store):
    from apis.app_api.skills.service import SkillCatalogService

    await _skill(repo)
    await _upload(repo, store, "pdf_workflows", "forms.md", b"forms")
    version = await pinning.pin_current("pdf_workflows", pinned_by="u-1")
    frozen = version.resources[0].s3_key

    service = SkillCatalogService(
        repository=repo, app_role_service=MagicMock(), app_role_admin_service=MagicMock(), resource_store=store
    )
    admin = MagicMock(user_id="admin-1", email="admin@example.edu")
    assert await service.delete_skill("pdf_workflows", admin, soft=False) is True

    assert await repo.get_latest_skill_version("pdf_workflows") is None
    with pytest.raises(Exception):
        store.get(frozen)
