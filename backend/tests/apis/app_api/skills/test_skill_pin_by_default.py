"""Pin by default (agentskills-conformance PR 6: D-7).

Binding an imported skill to an Agent pins its current version; binding a
hand-written one does not, and a binding already present or already pinned is
left as it is.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict, List

import pytest

from apis.shared.assistants.models import AgentBinding
from apis.shared.audit.service import AuditService
from apis.shared.skills import audit as skill_audit
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillImportSource
from apis.shared.skills.pinning import pin_imported_skill_bindings

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
# Pin by default (D-7)
# =============================================================================


@pytest.fixture(autouse=True)
def _pin_store(request, monkeypatch):
    """``pin_current`` freezes files through the process-wide store; use the moto one."""
    if "user_skill_service" not in request.fixturenames:
        return
    from apis.shared.skills import pinning

    store = request.getfixturevalue("skill_resource_store")
    monkeypatch.setattr(pinning, "get_skill_resource_store", lambda: store)


@pytest.mark.asyncio
async def test_binding_an_imported_skill_pins_it(user_skill_service, author_user):
    imported = await user_skill_service.import_bundle(author_user, _bundle(V1), source=_source())
    hand = await user_skill_service.create_my_skill(author_user, display_name="Hand Written", description="d")

    out = await pin_imported_skill_bindings(
        [
            AgentBinding(kind="skill", ref=imported.skill_id),
            AgentBinding(kind="skill", ref=hand.skill_id),
            AgentBinding(kind="tool", ref="web_search"),
        ],
        [],
        pinned_by=author_user.user_id,
    )
    by_ref = {b.ref: b for b in out}
    assert by_ref[imported.skill_id].config == {"version": 1}
    assert by_ref[hand.skill_id].config == {}
    assert by_ref["web_search"].config == {}

    version = await user_skill_service.repository.get_latest_skill_version(imported.skill_id)
    assert version is not None and version.version == 1


@pytest.mark.asyncio
async def test_an_already_bound_or_pinned_imported_skill_is_left_alone(user_skill_service, author_user):
    imported = await user_skill_service.import_bundle(author_user, _bundle(V1), source=_source())
    sid = imported.skill_id
    kept = AgentBinding(kind="skill", ref=sid)
    out = await pin_imported_skill_bindings([kept], [AgentBinding(kind="skill", ref=sid)], pinned_by="u")
    assert out[0].config == {}

    chosen = AgentBinding(kind="skill", ref=sid, config={"version": 7})
    out = await pin_imported_skill_bindings([chosen], [], pinned_by="u")
    assert out[0].config == {"version": 7}

    assert await pin_imported_skill_bindings(None, [], pinned_by="u") is None


@pytest.mark.asyncio
async def test_designer_save_path_pins_imported_skills(user_skill_service, author_user):
    from apis.app_api.agent_designer.services.binding_validation import pin_imported_skills

    imported = await user_skill_service.import_bundle(author_user, _bundle(V1), source=_source())
    out = await pin_imported_skills(author_user, [AgentBinding(kind="skill", ref=imported.skill_id)])
    assert out[0].config == {"version": 1}
