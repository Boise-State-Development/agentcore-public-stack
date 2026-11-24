"""``GET /skills/{id}/script-trust`` and ``PUT /skills/mine/{id}/scripts`` (skill-code-sandbox §8.4, §8.6)."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.skills import script_trust_routes as routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.skills import script_reviews
from apis.shared.skills import script_trust as st
from apis.shared.skills.models import SkillResourceRef

MEMBER = User(email="member@example.com", user_id="member-1", name="Member", roles=[])
AUTHOR_ID = "author-1"
NAMES = {AUTHOR_ID: "Dana Author", "reviewer-1": "Riley Reviewer"}


def _skill(owner: str = AUTHOR_ID, imported: bool = False) -> Any:
    data = b"print(1)"
    ref = SkillResourceRef(
        filename="fill.py", content_hash=hashlib.sha256(data).hexdigest(), content_type="text/x-python",
        size=len(data), s3_key="k", kind="script",
    )
    source = SimpleNamespace(provenance=lambda: {"type": "github", "owner": "acme", "repo": "skills"}) if imported else None
    return SimpleNamespace(skill_id="pdf", display_name="PDF", owner_id=owner, resources=[ref], import_source=source)


@pytest.fixture()
def state(monkeypatch):
    s: Dict[str, Any] = {"record": _skill(), "review": None, "switch": None, "bound": True}

    async def bound(skill_id, assistant_id, user):
        if not s["bound"]:
            raise HTTPException(status_code=404, detail="not found")
        return s["record"]

    async def accessible(skill_id, user):
        return s["record"]

    async def name(user_id: Optional[str]) -> Optional[str]:
        return NAMES.get(user_id or "")

    monkeypatch.setattr(routes, "_bound_record", bound)
    monkeypatch.setattr(routes, "_require_accessible_skill", accessible)
    monkeypatch.setattr(routes, "_display_name", name)
    monkeypatch.setattr(script_reviews, "read_review", lambda *a, **k: s["review"])
    monkeypatch.setattr(script_reviews, "read_switch", lambda *a, **k: s["switch"])
    return s


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: MEMBER
    return TestClient(app, raise_server_exceptions=False)


def test_a_reviewed_skill_names_its_author_reviewer_and_version(state):
    state["review"] = {"reviewedBy": "reviewer-1", "reviewedAt": "2026-11-23T00:00:00Z", "method": "review"}
    body = _client().get("/skills/pdf/script-trust", params={"assistantId": "agent-1"}).json()
    digest = st.scripts_hash(state["record"])
    assert body == {
        "skillId": "pdf", "displayName": "PDF", "author": "Dana Author", "scriptsHash": digest,
        "version": digest[:8], "trustedBy": "reviewed", "method": "review", "reviewer": "Riley Reviewer",
        "reviewedAt": "2026-11-23T00:00:00Z", "provenance": None, "scriptsOff": None,
    }
    # Display names only: no user id anywhere in the payload.
    assert AUTHOR_ID not in str(body) and "reviewer-1" not in str(body)


def test_an_authored_review_reads_as_shared_by(state):
    state["review"] = {"reviewedBy": AUTHOR_ID, "reviewedAt": "t", "method": "authored"}
    body = _client().get("/skills/pdf/script-trust", params={"assistantId": "agent-1"}).json()
    assert (body["trustedBy"], body["method"], body["reviewer"]) == ("reviewed", "authored", "Dana Author")


def test_no_review_and_switched_off_scripts_say_so(state):
    state["switch"] = st.ScriptSwitch(off=True, by="admin")
    body = _client().get("/skills/pdf/script-trust", params={"assistantId": "agent-1"}).json()
    assert (body["trustedBy"], body["reviewer"], body["scriptsOff"]) == (None, None, {"by": "admin"})


def test_the_callers_own_import_is_imported_with_its_provenance(state):
    state["record"] = _skill(owner=MEMBER.user_id, imported=True)
    body = _client().get("/skills/pdf/script-trust").json()
    assert (body["trustedBy"], body["provenance"]) == ("imported", {"type": "github", "owner": "acme", "repo": "skills"})


def test_a_skill_the_agent_does_not_bind_is_a_404(state):
    state["bound"] = False
    assert _client().get("/skills/pdf/script-trust", params={"assistantId": "agent-1"}).status_code == 404


def test_the_authors_switch_and_an_admin_lock(monkeypatch):
    calls = []

    class Service:
        async def require_owned(self, skill_id, user):
            calls.append(("owned", skill_id))

    async def switch(skill_id, user, *, off, reason=""):
        if skill_id == "locked":
            raise script_reviews.ScriptSwitchLockedError("An administrator turned this skill's scripts off.")
        return st.ScriptSwitch(off=off, by="author")

    monkeypatch.setattr(routes, "get_user_skill_service", lambda: Service())
    monkeypatch.setattr(script_reviews, "set_author_switch", switch)
    client = _client()
    assert client.put("/skills/mine/pdf/scripts", json={"off": True}).json() == {"off": True, "by": "author"}
    assert client.put("/skills/mine/pdf/scripts", json={"off": False}).json() == {"off": False, "by": None}
    locked = client.put("/skills/mine/locked/scripts", json={"off": False})
    assert locked.status_code == 409 and "administrator" in locked.json()["detail"]
    assert calls[0] == ("owned", "pdf")


def test_both_routes_use_session_auth():
    for route in routes.router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert get_current_user_from_session in deps, route.path
