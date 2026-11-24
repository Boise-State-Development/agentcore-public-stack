"""The admin's skill-scripts page and switch (skill-code-sandbox §8.4, §8.10, §9).

``apis.shared.skills.script_switch_admin`` and ``/admin/skill-scripts`` against a
moto AppRoles table, so the switch row the admin writes is the row the
Runtime's ``script_trust.read_trust_rows`` reads on the next script call.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skill_scripts import routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.skills import script_reviews as sr
from apis.shared.skills import script_switch_admin as admin_switch
from apis.shared.skills import script_trust as st
from apis.shared.skills.models import SkillResourceRef

ADMIN = User(email="admin@example.edu", user_id="admin-1", name="Admin", roles=[])
AUTHOR = User(email="author@example.edu", user_id="author-1", name="Author", roles=[])
MEMBER_ID = "member-1"
AGENT = "agent-1"
PROJECT_HARNESS = "harness-1"
SOURCE = b"import sys\nprint('fill', sys.argv)\n"
NAMES = {"author-1": "Dana Author", "admin-1": "Avery Admin", "reviewer-1": "Riley Reviewer"}
ASSISTANTS = {AGENT: ("Grant Helper", None, False), PROJECT_HARNESS: ("Award Intake", "project", False)}


def _ref(path: str, data: bytes, kind: str = "script") -> SkillResourceRef:
    return SkillResourceRef(
        filename=path,
        content_hash=hashlib.sha256(data).hexdigest(),
        content_type="text/x-python",
        size=len(data),
        s3_key=f"skills/pdf/{kind}s/{path}",
        kind=kind,
        path=path,
    )


def _skill(source: bytes = SOURCE) -> Any:
    return SimpleNamespace(
        skill_id="pdf",
        display_name="PDF forms",
        status="active",
        owner_id=AUTHOR.user_id,
        resources=[_ref("forms.md", b"# forms", kind="reference"), _ref("fill.py", source)],
        import_source=SimpleNamespace(provenance=lambda: {"type": "github", "owner": "acme", "repo": "skills"}),
    )


@pytest.fixture()
def table(roles_table, monkeypatch):
    # The Runtime reader's client is per process; drop one built under another test's moto.
    monkeypatch.setattr(st, "_client", None)
    import boto3

    return boto3.resource("dynamodb", region_name="us-east-1").Table("test-app-roles")


@pytest.fixture()
def catalog(monkeypatch):
    """The skill catalog and the resource bucket, by skill id and S3 key."""
    skills: Dict[str, Any] = {"pdf": _skill()}
    blobs: Dict[str, bytes] = {"skills/pdf/scripts/fill.py": SOURCE}

    class _Repo:
        async def get_skill(self, skill_id: str) -> Any:
            return skills.get(skill_id)

    class _Store:
        def get(self, key: str) -> bytes:
            return blobs[key]

    import apis.shared.skills.repository as repository
    import apis.shared.skills.resource_store as resource_store

    monkeypatch.setattr(repository, "get_skill_catalog_repository", lambda: _Repo())
    monkeypatch.setattr(resource_store, "get_skill_resource_store", lambda: _Store())

    async def name(user_id):
        return NAMES.get(user_id or "")

    async def assistant(assistant_id):
        return ASSISTANTS.get(assistant_id, (None, None, True))

    monkeypatch.setattr(admin_switch, "_display_name", name)
    monkeypatch.setattr(admin_switch, "_assistant", assistant)
    return SimpleNamespace(skills=skills, blobs=blobs)


@pytest.fixture()
def audits(monkeypatch):
    seen: List[Dict[str, Any]] = []

    class _Audit:
        def record(self, **kw):
            seen.append(kw)
            return object()

    import apis.shared.skills.script_switch_admin as module

    monkeypatch.setattr(module, "get_audit_service", lambda: _Audit())
    monkeypatch.setattr(sr, "_audit", lambda *a, **k: True)
    return seen


@pytest.fixture()
def scopes(monkeypatch):
    held = {"app_roles": [], "admin_scopes": ["admin.skill_scripts"]}

    class _Roles:
        async def resolve_user_permissions(self, user):
            return SimpleNamespace(**held)

    import apis.shared.rbac.service as rbac_service

    monkeypatch.setattr(rbac_service, "get_app_role_service", lambda: _Roles())
    return held


@pytest.fixture()
def client(table, catalog, audits, scopes) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router, prefix="/admin")
    app.dependency_overrides[get_current_user_from_session] = lambda: ADMIN
    return TestClient(app)


def _put_review(table, scope_id: str, digest: str, *, reviewer: str, at: str, version: int, scope_type: str = "AST") -> None:
    table.put_item(
        Item={
            "PK": "SKILL#pdf",
            "SK": f"SCRIPTS_REVIEW#{scope_type}#{scope_id}#{digest}",
            "skillId": "pdf",
            "scopeType": scope_type,
            "scopeId": scope_id,
            "scriptsHash": digest,
            "reviewedBy": reviewer,
            "reviewedAt": at,
            "method": "review",
            "state": "approved",
            "version": version,
            "scripts": {"scripts/fill.py": "f" * 64},
        }
    )


def _member_verdict(skill: Any, scope: str = AGENT) -> st.ScriptVerdict:
    """What the Runtime decides on the member's next call: the real read, then the verdict."""
    rows = st.read_trust_rows(skill, MEMBER_ID, scope, table_name="test-app-roles")
    return st.script_verdict(skill, MEMBER_ID, scope, rows)


def _switch(client: TestClient, **body: Any):
    return client.put("/admin/skill-scripts/pdf/switch", json={"reason": "Reported as unsafe", **body})


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_reads_and_switches_are_refused_without_the_scope(client, scopes, table):
    scopes["admin_scopes"] = ["admin.skills"]

    assert client.get("/admin/skill-scripts/pdf").status_code == 403
    assert _switch(client, off=True).status_code == 403
    assert "Item" not in table.get_item(Key={"PK": "SKILL#pdf", "SK": "SCRIPTS_SWITCH"})


def test_system_admin_holds_the_scope_implicitly(client, scopes):
    scopes.update(app_roles=["system_admin"], admin_scopes=[])

    assert client.get("/admin/skill-scripts/pdf").status_code == 200


def test_an_unknown_skill_is_a_404_for_both_routes(client, audits):
    assert client.get("/admin/skill-scripts/nope").status_code == 404
    assert client.put("/admin/skill-scripts/nope/switch", json={"off": True, "reason": "x"}).status_code == 404
    assert audits == []


# ---------------------------------------------------------------------------
# The detail
# ---------------------------------------------------------------------------


def test_the_detail_shows_author_provenance_source_and_where_each_version_is_reviewed(client, table, catalog):
    skill = catalog.skills["pdf"]
    current = st.scripts_hash(skill)
    older = "a" * 64
    _put_review(table, AGENT, current, reviewer="reviewer-1", at="2026-11-20T10:00:00+00:00", version=3)
    _put_review(table, PROJECT_HARNESS, current, reviewer="author-1", at="2026-11-21T10:00:00+00:00", version=3)
    _put_review(table, "gone-agent", older, reviewer="reviewer-1", at="2026-10-01T10:00:00+00:00", version=1)

    body = client.get("/admin/skill-scripts/pdf").json()

    assert body["displayName"] == "PDF forms"
    assert (body["ownerId"], body["authorName"], body["adminCatalog"]) == ("author-1", "Dana Author", False)
    assert body["provenance"] == {"type": "github", "owner": "acme", "repo": "skills"}
    assert body["scriptsHash"] == current
    (script,) = body["scripts"]
    assert script["path"] == "scripts/fill.py"
    assert script["source"] == SOURCE.decode()
    assert script["contentHash"] == hashlib.sha256(SOURCE).hexdigest()
    assert not script["truncated"]

    assert [v["scriptsHash"] for v in body["versions"]] == [current, older]
    live, old = body["versions"]
    assert live["isCurrent"] and not old["isCurrent"]
    assert [(r["scopeKind"], r["scopeName"], r["reviewerName"], r["version"]) for r in live["reviews"]] == [
        ("project", "Award Intake", "Dana Author", 3),
        ("agent", "Grant Helper", "Riley Reviewer", 3),
    ]
    (gone,) = old["reviews"]
    assert gone["scopeMissing"] and gone["scopeName"] is None
    assert body["switch"] == {
        "off": False, "by": None, "byUserId": None, "byName": None, "at": None, "reason": None, "hashes": [],
    }


def test_a_long_script_is_cut_and_an_unreadable_one_is_marked(client, catalog):
    big = b"x" * (admin_switch.SOURCE_MAX_BYTES + 10)
    catalog.skills["pdf"] = _skill(big)
    catalog.blobs.clear()
    (script,) = client.get("/admin/skill-scripts/pdf").json()["scripts"]
    assert script["unreadable"] and script["source"] is None

    catalog.blobs["skills/pdf/scripts/fill.py"] = big
    (script,) = client.get("/admin/skill-scripts/pdf").json()["scripts"]
    assert script["truncated"] and len(script["source"]) == admin_switch.SOURCE_MAX_BYTES


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_switching_off_refuses_the_members_next_call_and_on_lets_it_run(client, table, catalog, audits):
    skill = catalog.skills["pdf"]
    digest = st.scripts_hash(skill)
    _put_review(table, AGENT, digest, reviewer="reviewer-1", at="2026-11-20T10:00:00+00:00", version=3)
    assert _member_verdict(skill).allowed

    response = _switch(client, off=True)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currentSwitchedOff"]
    assert (body["switch"]["off"], body["switch"]["by"], body["switch"]["byName"]) == (True, "admin", "Avery Admin")
    assert body["switch"]["reason"] == "Reported as unsafe"
    verdict = _member_verdict(skill)
    assert (verdict.allowed, verdict.refused_reason) == (False, st.REFUSED_OFF_BY_ADMIN)
    # It applies to the author's own runs too, and the author's switch can't undo it.
    rows = st.read_trust_rows(skill, AUTHOR.user_id, None, table_name="test-app-roles")
    assert st.script_verdict(skill, AUTHOR.user_id, None, rows).refused_reason == st.REFUSED_OFF_BY_ADMIN
    with pytest.raises(sr.ScriptSwitchLockedError):
        asyncio.run(sr.set_author_switch("pdf", AUTHOR, off=False))

    assert _switch(client, off=False, reason="Fixed upstream").status_code == 200

    assert _member_verdict(skill).allowed
    assert [(a["action"], a["after"]["off"], a["before"]["off"]) for a in audits] == [
        ("skill.scripts_switched", True, False),
        ("skill.scripts_switched", False, True),
    ]


def test_switching_off_one_version_leaves_the_others_running(client, catalog):
    current = catalog.skills["pdf"]
    other = _skill(b"print('v2')\n")

    response = _switch(client, off=True, hashes=[st.scripts_hash(other).upper()])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["switch"]["hashes"] == [st.scripts_hash(other)]
    assert not body["currentSwitchedOff"]
    assert _member_verdict(other).refused_reason == st.REFUSED_OFF_BY_ADMIN
    # The live version has no review here, so it refuses for the member on that ground,
    # not the switch.
    assert _member_verdict(current).refused_reason == st.REFUSED_NOT_REVIEWED


def test_an_admin_undoes_an_authors_switch(client, table, catalog):
    skill = catalog.skills["pdf"]
    asyncio.run(sr.set_author_switch("pdf", AUTHOR, off=True, reason="pausing"))
    assert _member_verdict(skill).refused_reason == st.REFUSED_OFF_BY_AUTHOR

    assert _switch(client, off=False, reason="Author asked us to").status_code == 200

    item = table.get_item(Key={"PK": "SKILL#pdf", "SK": "SCRIPTS_SWITCH"})["Item"]
    assert (item["off"], item["by"], item["byUserId"]) == (False, "admin", "admin-1")
    # The author is free to switch again once an admin turned it on.
    asyncio.run(sr.set_author_switch("pdf", AUTHOR, off=True))
    assert _member_verdict(skill).refused_reason == st.REFUSED_OFF_BY_AUTHOR


def test_the_audit_record_carries_ids_and_hashes_never_script_text(client, catalog, audits):
    other = st.scripts_hash(_skill(b"print('v2')\n"))
    _switch(client, off=True, hashes=[other])

    (record,) = audits
    assert record["action"] == "skill.scripts_switched"
    assert (record["target_id"], record["target_type"]) == ("pdf", "skill")
    assert record["actor"].user_id == "admin-1"
    assert record["after"] == {"off": True, "by": "admin", "hashes": [other], "reason": "Reported as unsafe"}
    assert record["before"] == {"off": False}
    assert SOURCE.decode() not in str(record) and "fill" not in str(record["after"])


def test_a_request_that_changes_nothing_writes_and_audits_nothing(client, audits):
    assert _switch(client, off=False).status_code == 200
    assert _switch(client, off=True).status_code == 200
    assert _switch(client, off=True).status_code == 200

    assert len(audits) == 1


def test_a_bad_hash_or_a_blank_reason_is_refused(client, audits, table):
    assert _switch(client, off=True, hashes=["3f2a9c1e"]).status_code == 422
    assert client.put("/admin/skill-scripts/pdf/switch", json={"off": True, "reason": "  "}).status_code == 422
    assert client.put("/admin/skill-scripts/pdf/switch", json={"off": True}).status_code == 422

    assert audits == []
    assert "Item" not in table.get_item(Key={"PK": "SKILL#pdf", "SK": "SCRIPTS_SWITCH"})


def test_the_review_query_pages_and_skips_the_switch_row(table):
    for n in range(3):
        _put_review(table, f"agent-{n}", "b" * 64, reviewer="reviewer-1", at=f"2026-11-0{n + 1}", version=n)
    table.put_item(Item={"PK": "SKILL#pdf", "SK": "SCRIPTS_SWITCH", "off": True, "by": "admin"})
    table.put_item(Item={"PK": "SKILL#pdf", "SK": "METADATA"})

    rows = admin_switch.query_review_rows("pdf")

    assert sorted(r["scopeId"] for r in rows) == ["agent-0", "agent-1", "agent-2"]
