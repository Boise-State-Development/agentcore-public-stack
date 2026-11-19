"""Reach confirmation on REST skill writes (agentskills-conformance PR 6: S3).

``PUT /skills/mine/{id}`` and the re-import route 409 with the approval card's reach
summary only while someone else's session would run the edit unpinned;
``confirmReach`` saves, and every other case saves as before.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.skills import bundle_import_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.audit.service import AuditService
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills import audit as skill_audit
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillImportSource
from apis.shared.skills.reach import REACH_CONFIRMATION_CODE, SkillBinding, SkillReach

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
# Reach on REST writes (S3)
# =============================================================================


def _reach(*bindings: SkillBinding) -> SkillReach:
    return SkillReach("any", list(bindings))


SHARED_UNPINNED = SkillBinding(name="Contracts Desk", is_project=False, shared=True, pinned_version=None)
PROJECT_UNPINNED = SkillBinding(name="Legal", is_project=True, shared=True, pinned_version=None)
PRIVATE_UNPINNED = SkillBinding(name="My Drafts", is_project=False, shared=False, pinned_version=None)
SHARED_PINNED = SkillBinding(name="Contracts Desk", is_project=False, shared=True, pinned_version=2)


@pytest.fixture()
def reach_client(user_skill_service, author_user, monkeypatch):
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "true")
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: user_skill_service)
    monkeypatch.setattr(bundle_import_routes, "get_user_skill_service", lambda: user_skill_service)
    reach = {"value": _reach()}
    calls: List[tuple] = []

    def fake_lookup(skill_id, owner_id):
        calls.append((skill_id, owner_id))
        return reach["value"]

    monkeypatch.setattr(my_skill_routes, "lookup_skill_reach", fake_lookup)
    app = FastAPI()
    app.include_router(bundle_import_routes.router)
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        c.reach = reach  # type: ignore[attr-defined]
        c.reach_calls = calls  # type: ignore[attr-defined]
        yield c


def _new_skill(client) -> str:
    created = client.post("/skills/mine", json={"displayName": "Grant Review", "description": "d"})
    assert created.status_code == 200
    return created.json()["skillId"]


@pytest.mark.parametrize("binding", [SHARED_UNPINNED, PROJECT_UNPINNED])
def test_put_409s_with_the_reach_summary_for_others_unpinned(reach_client, binding):
    sid = _new_skill(reach_client)
    reach_client.reach["value"] = _reach(binding)
    resp = reach_client.put(f"/skills/mine/{sid}", json={"description": "changed"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == REACH_CONFIRMATION_CODE
    assert detail["reach"] == _reach(binding).summary()
    assert reach_client.get(f"/skills/mine/{sid}").json()["description"] == "d"


def test_put_with_confirm_reach_saves_without_a_lookup(reach_client):
    sid = _new_skill(reach_client)
    reach_client.reach["value"] = _reach(SHARED_UNPINNED)
    resp = reach_client.put(f"/skills/mine/{sid}", json={"description": "changed", "confirmReach": True})
    assert resp.status_code == 200
    assert resp.json()["description"] == "changed"
    assert reach_client.reach_calls == []


@pytest.mark.parametrize(
    "bindings",
    [(), (PRIVATE_UNPINNED,), (SHARED_PINNED,), (PRIVATE_UNPINNED, SHARED_PINNED)],
    ids=["bound-nowhere", "owner-only", "pinned-only", "private-and-pinned"],
)
def test_put_saves_as_today_when_no_one_else_is_reached(reach_client, bindings):
    sid = _new_skill(reach_client)
    reach_client.reach["value"] = _reach(*bindings)
    resp = reach_client.put(f"/skills/mine/{sid}", json={"description": "changed"})
    assert resp.status_code == 200


def test_put_saves_when_the_lookup_could_not_run(reach_client):
    sid = _new_skill(reach_client)
    reach_client.reach["value"] = SkillReach("x", complete=False, error="ClientError")
    assert reach_client.put(f"/skills/mine/{sid}", json={"description": "changed"}).status_code == 200


def test_reimport_409s_then_confirm_reach_saves(reach_client):
    files = {"file": ("bundle.zip", make_zip(V1), "application/zip")}
    created = reach_client.post("/skills/mine/import", files=files)
    assert created.status_code == 200, created.text
    sid = created.json()["skill"]["skillId"]

    reach_client.reach["value"] = _reach(SHARED_UNPINNED)
    again = {"file": ("bundle.zip", make_zip(V2), "application/zip")}
    refused = reach_client.post(f"/skills/mine/{sid}/import", files=again)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == REACH_CONFIRMATION_CODE

    confirmed = reach_client.post(
        f"/skills/mine/{sid}/import", files=again, data={"confirmReach": "true"}
    )
    assert confirmed.status_code == 200, confirmed.text


