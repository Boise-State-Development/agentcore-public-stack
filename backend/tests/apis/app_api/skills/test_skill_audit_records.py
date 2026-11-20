"""Skill audit records (agentskills-conformance PR 6: S5, D-5).

One record per skill mutation at the service layer, for both tiers and export;
none for a no-op save; a failing audit write never fails the mutation; and no
record carries a skill's instructions or description.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as admin_skill_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.audit.models import TARGET_SKILL, AuditAction
from apis.shared.audit.service import AuditService
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills import audit as skill_audit
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillDefinition, SkillImportSource
from apis.shared.skills.versions import content_hash

# Distinctive text that must never reach an audit record.
SECRET_DESCRIPTION = "Reviews NDAs-DESCRIPTION-SENTINEL-41"
SECRET_INSTRUCTIONS = "Follow the playbook INSTRUCTIONS-SENTINEL-73 exactly."

SKILL_MD = f"""---
name: contract-redline
description: {SECRET_DESCRIPTION}
---
{SECRET_INSTRUCTIONS}
"""


def make_zip(files: Dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


V1 = {
    "contract-redline/SKILL.md": SKILL_MD,
    "contract-redline/references/nda.md": "NDA rules v1",
}
V2 = {
    "contract-redline/SKILL.md": SKILL_MD.replace("exactly", "to the letter"),
    "contract-redline/references/nda.md": "NDA rules v2",
}


class _MemoryAuditRepository:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def put(self, record) -> None:
        self.items.append(record.to_item())


class _FailingAuditRepository:
    def put(self, record) -> None:
        raise RuntimeError("DynamoDB is down")


@pytest.fixture()
def audit_items(monkeypatch) -> List[Dict[str, Any]]:
    """Every skill audit record written, as the DynamoDB items they would be."""
    repo = _MemoryAuditRepository()
    service = AuditService(repository=repo)
    monkeypatch.setattr(skill_audit, "get_audit_service", lambda: service)
    return repo.items


def _actions(items: List[Dict[str, Any]]) -> List[str]:
    return [i["action"] for i in items]


def _assert_no_text(items: List[Dict[str, Any]]) -> None:
    blob = json.dumps(items, default=str)
    for text in (SECRET_DESCRIPTION, SECRET_INSTRUCTIONS, "NDA rules", "Step one of the secret"):
        assert text not in blob


def _source() -> SkillImportSource:
    return SkillImportSource(
        type="github",
        bundle_hash="h",
        imported_at="2026-11-18T00:00:00Z",
        url="https://github.com/acme/skills/tree/main/contract-redline",
        owner="acme",
        repo="skills",
        ref="main",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
    )


def _bundle(files: Dict[str, str]):
    load = load_bundle(make_zip(files))
    assert load.bundle is not None, load
    return load.bundle


# =============================================================================
# Audit records, My Skills tier
# =============================================================================


@pytest.mark.asyncio
async def test_each_user_mutation_writes_exactly_one_record(user_skill_service, author_user, audit_items):
    svc = user_skill_service
    skill = await svc.create_my_skill(
        author_user,
        display_name="Grant Review",
        description=SECRET_DESCRIPTION,
        instructions=SECRET_INSTRUCTIONS,
    )
    sid = skill.skill_id
    assert _actions(audit_items) == [AuditAction.SKILL_CREATED]

    await svc.update_my_skill(sid, {"instructions": SECRET_INSTRUCTIONS + " v2"}, author_user)
    assert _actions(audit_items)[-1] == AuditAction.SKILL_UPDATED

    await svc.write_text_file(sid, "reference", "notes.md", "Step one of the secret", author_user)
    await svc.delete_resource(sid, "notes.md", author_user)
    await svc.delete_my_skill(sid, author_user)

    assert _actions(audit_items) == [
        AuditAction.SKILL_CREATED,
        AuditAction.SKILL_UPDATED,
        AuditAction.SKILL_UPDATED,
        AuditAction.SKILL_UPDATED,
        AuditAction.SKILL_DELETED,
    ]
    assert {i["targetType"] for i in audit_items} == {TARGET_SKILL}
    assert {i["targetId"] for i in audit_items} == {sid}
    assert {i["actorUserId"] for i in audit_items} == {author_user.user_id}
    _assert_no_text(audit_items)


@pytest.mark.asyncio
async def test_a_record_carries_tier_owner_hash_and_files(user_skill_service, author_user, audit_items):
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Grant Review", description=SECRET_DESCRIPTION
    )
    refs = await user_skill_service.write_text_file(
        skill.skill_id, "reference", "notes.md", "text", author_user
    )
    created, file_written = audit_items
    assert created["after"]["tier"] == "user"
    assert created["after"]["ownerId"] == author_user.user_id
    assert created["after"]["contentHash"] == content_hash(skill)
    assert file_written["before"]["files"] == {}
    assert file_written["after"]["files"] == {"notes.md": refs[0].content_hash}
    assert "files" in file_written["changes"]


@pytest.mark.asyncio
async def test_a_no_op_save_writes_no_record(user_skill_service, author_user, audit_items):
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Grant Review", description="d", instructions="i"
    )
    await user_skill_service.update_my_skill(
        skill.skill_id, {"description": "d", "instructions": "i"}, author_user
    )
    await user_skill_service.write_text_file(skill.skill_id, "reference", "a.md", "same", author_user)
    await user_skill_service.write_text_file(skill.skill_id, "reference", "a.md", "same", author_user)
    assert _actions(audit_items) == [AuditAction.SKILL_CREATED, AuditAction.SKILL_UPDATED]


@pytest.mark.asyncio
async def test_import_and_refresh_carry_the_source(user_skill_service, author_user, audit_items):
    svc = user_skill_service
    skill = await svc.import_bundle(author_user, _bundle(V1), source=_source())
    await svc.import_bundle(author_user, _bundle(V2), source=_source(), skill_id=skill.skill_id)
    # The same bundle again changes nothing a record would show.
    await svc.import_bundle(author_user, _bundle(V2), source=_source(), skill_id=skill.skill_id)

    assert _actions(audit_items) == [AuditAction.SKILL_IMPORTED, AuditAction.SKILL_REFRESHED]
    imported, refreshed = audit_items
    assert imported["after"]["importSource"] == {
        "type": "github",
        "owner": "acme",
        "repo": "skills",
        "ref": "main",
        "commitSha": "0123456789abcdef0123456789abcdef01234567",
    }
    assert set(imported["after"]["files"]) == {"nda.md"}
    assert refreshed["before"]["contentHash"] != refreshed["after"]["contentHash"]
    _assert_no_text(audit_items)


@pytest.mark.asyncio
async def test_a_failing_audit_write_does_not_fail_the_mutation(
    user_skill_service, author_user, monkeypatch
):
    monkeypatch.setattr(
        skill_audit, "get_audit_service", lambda: AuditService(repository=_FailingAuditRepository())
    )
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Grant Review", description="d"
    )
    updated = await user_skill_service.update_my_skill(skill.skill_id, {"description": "e"}, author_user)
    assert updated.description == "e"


@pytest.mark.asyncio
async def test_an_audit_service_that_raises_is_swallowed(user_skill_service, author_user, monkeypatch):
    def boom():
        raise RuntimeError("no audit service")

    monkeypatch.setattr(skill_audit, "get_audit_service", boom)
    skill = await user_skill_service.create_my_skill(author_user, display_name="Grant Review", description="d")
    assert skill.skill_id


# =============================================================================
# Audit records, admin catalog tier and export
# =============================================================================


@pytest.mark.asyncio
async def test_each_catalog_mutation_writes_exactly_one_record(skill_service, admin_user, audit_items):
    skill = SkillDefinition(
        skill_id="policy_format",
        display_name="Policy Format",
        description=SECRET_DESCRIPTION,
        instructions=SECRET_INSTRUCTIONS,
    )
    await skill_service.create_skill(skill, admin_user)
    await skill_service.update_skill("policy_format", {"instructions": "new"}, admin_user)
    await skill_service.update_skill("policy_format", {"instructions": "new"}, admin_user)  # no-op
    await skill_service.add_resource("policy_format", "x.md", b"x", "text/markdown", admin_user)
    await skill_service.delete_resource("policy_format", "x.md", admin_user)
    await skill_service.delete_skill("policy_format", admin_user, soft=True)

    assert _actions(audit_items) == [
        AuditAction.SKILL_CREATED,
        AuditAction.SKILL_UPDATED,
        AuditAction.SKILL_UPDATED,
        AuditAction.SKILL_UPDATED,
        AuditAction.SKILL_DELETED,
    ]
    assert audit_items[0]["after"]["tier"] == "catalog"
    assert audit_items[-1]["after"]["softDelete"] is True
    _assert_no_text(audit_items)


def test_export_writes_one_record_each(user_skill_service, skill_service, author_user, admin_user, audit_items, monkeypatch):
    import asyncio

    skill = asyncio.run(
        user_skill_service.create_my_skill(author_user, display_name="Grant Review", description="d")
    )
    asyncio.run(
        skill_service.create_skill(
            SkillDefinition(skill_id="policy_format", display_name="P", description="d", instructions=""), admin_user
        )
    )
    audit_items.clear()

    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: user_skill_service)
    monkeypatch.setattr(admin_skill_routes, "get_skill_catalog_service", lambda: skill_service)
    user_app = FastAPI()
    user_app.include_router(my_skill_routes.router)
    user_app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    admin_app = FastAPI()
    admin_app.include_router(admin_skill_routes.router, prefix="/admin")
    admin_app.dependency_overrides[admin_skill_routes.require_skills_admin] = lambda: admin_user

    with TestClient(user_app) as c:
        assert c.get(f"/skills/mine/{skill.skill_id}/export").status_code == 200
    with TestClient(admin_app) as c:
        assert c.get("/admin/skills/policy_format/export").status_code == 200

    assert _actions(audit_items) == [AuditAction.SKILL_EXPORTED, AuditAction.SKILL_EXPORTED]
    assert [i["targetId"] for i in audit_items] == [skill.skill_id, "policy_format"]


