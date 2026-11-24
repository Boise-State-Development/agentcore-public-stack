"""``/skills/mine/{id}/uses`` and ``…/uses/update`` (skill-version-history.md D9, §6.3).

What these pin: the flag 404s both routes after auth; a use names an Agent by its
id and a project by its project id, and only a store Agent reads as published;
an update moves only pins ``uses`` lists as behind and updatable; one failing
target never stops the others; a request names at most 25 targets.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.skills import routes as my_skill_routes
from apis.app_api.skills import uses_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills.reach import SkillBinding
from apis.shared.skills.uses import SkillUses

LIVE_AGENT = SkillBinding("Advisor", False, True, None, agent_id="a-live")
BEHIND_AGENT = SkillBinding("Grader", False, True, 2, agent_id="a-behind", published=True)
BEHIND_PROJECT = SkillBinding("Grant Office", True, True, 3, agent_id="h-p1", project_id="p1")


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
    for module in (uses_routes, my_skill_routes):
        monkeypatch.setattr(module, "get_user_skill_service", lambda: user_skill_service)

    async def classify(live, owner_id):
        return SkillUses(
            current=[LIVE_AGENT], behind=[BEHIND_AGENT, BEHIND_PROJECT], others_behind=1, complete=True
        )

    monkeypatch.setattr(uses_routes, "classify_uses", classify)
    app = FastAPI()
    app.include_router(uses_routes.router)
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        yield c


def _skill(client) -> str:
    resp = client.post(
        "/skills/mine", json={"displayName": "Grant Review", "description": "Review.", "instructions": "x"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["skillId"]


def test_uses_names_agents_and_projects(client):
    sid = _skill(client)
    body = client.get(f"/skills/mine/{sid}/uses").json()
    assert [(u["kind"], u["id"], u["pinnedVersion"]) for u in body["current"]] == [("agent", "a-live", None)]
    assert [(u["kind"], u["id"], u["published"]) for u in body["behind"]] == [
        ("agent", "a-behind", True),
        ("project", "p1", False),
    ]
    assert body["othersBehind"] == 1
    assert body["complete"] is True


def test_update_moves_only_behind_pins_and_reports_each(client, monkeypatch):
    sid = _skill(client)
    moved = []

    async def pin_agent(agent_id, skill_id, user):
        moved.append(("agent", agent_id))
        return 8

    async def pin_project(project_id, skill_id, user):
        raise HTTPException(status_code=403, detail="You are not an editor of this project")

    monkeypatch.setattr(uses_routes, "_pin_agent", pin_agent)
    monkeypatch.setattr(uses_routes, "_pin_project", pin_project)
    resp = client.post(
        f"/skills/mine/{sid}/uses/update",
        json={
            "targets": [
                {"kind": "project", "id": "p1"},
                {"kind": "agent", "id": "a-behind"},
                {"kind": "agent", "id": "a-live"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0] == {
        "kind": "project", "id": "p1", "ok": False, "version": None,
        "error": "You are not an editor of this project",
    }
    assert results[1] == {"kind": "agent", "id": "a-behind", "ok": True, "version": 8, "error": None}
    assert results[2]["ok"] is False
    assert moved == [("agent", "a-behind")]


def test_an_update_whose_scripts_need_a_review_says_where_to_review_them(client, monkeypatch):
    """A bulk update can't show the review sheet (skill-code-sandbox §8.5)."""
    from apis.shared.skills.script_review_check import ScriptsUnreviewedError

    sid = _skill(client)

    async def unreviewed(target_id, skill_id, user):
        raise ScriptsUnreviewedError([{"skillId": skill_id, "displayName": "PDF"}])

    monkeypatch.setattr(uses_routes, "_pin_agent", unreviewed)
    resp = client.post(f"/skills/mine/{sid}/uses/update", json={"targets": [{"kind": "agent", "id": "a-behind"}]})
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["ok"] is False
    assert result["error"] == "Its scripts changed. Update it from the agent's settings to review them."


def test_at_most_25_targets(client):
    sid = _skill(client)
    targets = [{"kind": "agent", "id": f"a{i}"} for i in range(26)]
    assert client.post(f"/skills/mine/{sid}/uses/update", json={"targets": targets}).status_code == 422


def test_another_users_skill_is_404(client, user_skill_service, other_user, monkeypatch):
    sid = _skill(client)
    app = FastAPI()
    app.include_router(uses_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: other_user
    with TestClient(app) as other:
        assert other.get(f"/skills/mine/{sid}/uses").status_code == 404
        assert other.post(f"/skills/mine/{sid}/uses/update", json={"targets": []}).status_code == 404


def test_flag_off_is_404(client, monkeypatch):
    sid = _skill(client)
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "")
    assert client.get(f"/skills/mine/{sid}/uses").status_code == 404
    assert client.post(f"/skills/mine/{sid}/uses/update", json={"targets": []}).status_code == 404
