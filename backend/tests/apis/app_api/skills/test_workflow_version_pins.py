"""The sheet's pinned Agent steps (``docs/specs/workflow-designer.md`` §23, PR-D.8a).

Through ``PUT /workflows/{skillId}/{name}?validate=true``, which answers with the document and its
sheet and writes nothing. What these pin:

* while ``AGENT_VERSION_PINNING_ENABLED`` is off, the document is exactly what it was;
* while on, a step that records a version carries ``pin``, judged by the one rule, for each row of
  §23.2's table, and a refused pin is the ``agent_version_unavailable`` blocker;
* the owner of a pinned step's Agent runs the pin, not their draft;
* the rows the rule needs are read in one call, and a failed read blocks rather than runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.workflows import routes as workflow_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.workflows import sheet as workflow_sheet

STAMP = "2026-11-01T00:00:00+00:00"


def _agent(name: str, *, published: Optional[int], owner: str = "someone-else"):
    return SimpleNamespace(
        name=name,
        emoji=None,
        image_url=None,
        icon_key=None,
        owner_id=owner,
        listing=SimpleNamespace(published_version=published),
        model_settings=None,
    )


class World:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.agents: Dict[str, SimpleNamespace] = {}
        self.rows: Dict[tuple, SimpleNamespace] = {}
        self.reads: List[list] = []
        self.read_fails = False

    async def accessible(self, user) -> List[str]:
        return [s.skill_id for s in await self.repository.list_skills_by_owner(user.user_id)]

    async def agent_lookup(self, assistant_id, user_id, user_email=None):
        agent = self.agents.get(assistant_id)
        return (agent, "viewer") if agent is not None else (None, None)

    async def get_versions(self, refs):
        self.reads.append(list(refs))
        if self.read_fails:
            raise RuntimeError("DynamoDB is unavailable")
        return {ref: self.rows[ref] for ref in refs if ref in self.rows}


@pytest.fixture()
def world(user_skill_service, monkeypatch):
    w = World(user_skill_service.repository)

    async def step_model(agent):
        return None, None

    for module in (workflow_routes, workflow_sheet):
        monkeypatch.setattr(module, "get_user_skill_service", lambda: user_skill_service)
        monkeypatch.setattr(module, "get_skill_catalog_repository", lambda: user_skill_service.repository)
        monkeypatch.setattr(module, "resolve_accessible_skill_ids", w.accessible)
        monkeypatch.setattr(module, "get_assistant_with_access_check", w.agent_lookup)
    monkeypatch.setattr(workflow_sheet, "step_model", step_model)
    monkeypatch.setattr(workflow_sheet, "get_versions", w.get_versions)
    return w


@pytest.fixture()
def client(world, author_user, monkeypatch):
    for name in ("HANDOFFS_ENABLED", "WORKFLOW_RUNS_ENABLED", "WORKFLOWS_ENABLED"):
        monkeypatch.setenv(name, "true")
    monkeypatch.delenv("AGENT_VERSION_PINNING_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(workflow_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        resp = c.post("/workflows", json={"title": "Proposal review"})
        assert resp.status_code == 201, resp.text
        c.path = f"/workflows/{resp.json()['workflow']['skillId']}/{resp.json()['workflow']['name']}"
        yield c


def _pin_on(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", "true")


def _sheet(client, *steps: dict) -> dict:
    """The validate answer for a workflow of ``steps`` (each ``{name, agentId, agentVersion?}``)."""
    body = {"title": "Proposal review", "request": "Review it", "steps": list(steps)}
    resp = client.put(client.path, json=body, params={"validate": "true"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _two(version: Optional[int] = 3) -> tuple:
    first = {"name": "reviewer", "agentId": "AST-rev"}
    if version is not None:
        first["agentVersion"] = version
    return first, {"name": "budget", "agentId": "AST-bud"}


@pytest.fixture()
def agents(world):
    world.agents["AST-rev"] = _agent("Reviewer", published=5)
    world.agents["AST-bud"] = _agent("Budget", published=None)  # never published: records no version
    world.rows[("AST-rev", 3)] = SimpleNamespace(published_at=STAMP)
    return world


# --- Off: exactly today ----------------------------------------------------------------------------


def test_off_the_document_is_unchanged(client, agents, monkeypatch):
    off = _sheet(client, *_two())

    assert all("pin" not in step for step in off["workflow"]["steps"])
    assert off["sheet"] == {"startable": True, "blockers": []}
    assert agents.reads == [], "nothing is read while pinning is off"

    # A pin that would be refused changes nothing while off either.
    agents.agents["AST-rev"] = _agent("Reviewer", published=None)
    assert _sheet(client, *_two())["sheet"] == {"startable": True, "blockers": []}


def test_off_the_owner_still_runs_their_draft(client, agents, author_user):
    agents.agents["AST-rev"] = _agent("Reviewer", published=5, owner=author_user.user_id)
    step = _sheet(client, *_two())["workflow"]["steps"][0]
    assert step["agent"]["runsDraft"] is True and "pin" not in step


# --- On: §23.2's table -----------------------------------------------------------------------------


def test_an_approved_update_leaves_the_step_on_its_version_with_the_current_one_offered(
    client, agents, monkeypatch
):
    _pin_on(monkeypatch)
    doc = _sheet(client, *_two())

    reviewer, budget = doc["workflow"]["steps"]
    assert reviewer["pin"] == {"version": 3, "canRun": True, "offerVersion": 5}
    assert reviewer["agent"]["changed"] is True
    assert "pin" not in budget, "a step that records no version runs live, as today"
    assert doc["sheet"] == {"startable": True, "blockers": []}
    assert agents.reads == [[("AST-rev", 3)]]


def test_the_current_version_needs_no_offer(client, agents, monkeypatch):
    _pin_on(monkeypatch)
    agents.rows[("AST-rev", 5)] = SimpleNamespace(published_at=None)  # stamp or not, P runs
    reviewer = _sheet(client, *_two(5))["workflow"]["steps"][0]
    assert reviewer["pin"] == {"version": 5, "canRun": True}


def test_a_rollback_blocks_a_step_pinned_above_it(client, agents, monkeypatch):
    _pin_on(monkeypatch)
    agents.agents["AST-rev"] = _agent("Reviewer", published=2)

    doc = _sheet(client, *_two())

    assert doc["workflow"]["steps"][0]["pin"] == {
        "version": 3,
        "canRun": False,
        "reason": "above_published",
        "offerVersion": 2,
    }
    assert doc["sheet"]["blockers"] == [
        {
            "code": "agent_version_unavailable",
            "step": "reviewer",
            "message": "Version 3 of this Agent is no longer published; version 2 is the current one. Use version 2, or remove the step",
        }
    ]
    assert agents.reads == [], "above the published version, the pointer alone decides"


def test_a_takedown_blocks_every_pinned_step(client, agents, monkeypatch):
    _pin_on(monkeypatch)
    agents.agents["AST-rev"] = _agent("Reviewer", published=None)

    doc = _sheet(client, *_two())

    assert doc["workflow"]["steps"][0]["pin"] == {"version": 3, "canRun": False, "reason": "unpublished"}
    assert doc["sheet"]["startable"] is False
    assert doc["sheet"]["blockers"][0]["message"] == (
        "This Agent is no longer published, so version 3 can't run. Remove the step or choose another Agent"
    )


def test_a_rejected_submission_can_never_be_pinned(client, agents, monkeypatch):
    _pin_on(monkeypatch)
    agents.rows[("AST-rev", 3)] = SimpleNamespace(published_at=None)

    doc = _sheet(client, *_two())

    assert doc["workflow"]["steps"][0]["pin"]["reason"] == "never_published"
    assert doc["sheet"]["blockers"][0]["message"] == (
        "Version 3 of this Agent was never published; version 5 is the current one. Use version 5, or remove the step"
    )


def test_a_missing_row_or_a_failed_read_blocks(client, agents, monkeypatch):
    _pin_on(monkeypatch)
    del agents.rows[("AST-rev", 3)]
    assert _sheet(client, *_two())["workflow"]["steps"][0]["pin"]["reason"] == "missing"

    agents.rows[("AST-rev", 3)] = SimpleNamespace(published_at=STAMP)
    agents.read_fails = True
    doc = _sheet(client, *_two())
    assert doc["workflow"]["steps"][0]["pin"]["reason"] == "missing"
    assert doc["sheet"]["blockers"][0]["code"] == "agent_version_unavailable"


def test_the_owner_runs_the_pin_not_their_draft(client, agents, monkeypatch, author_user):
    _pin_on(monkeypatch)
    agents.agents["AST-rev"] = _agent("Reviewer", published=5, owner=author_user.user_id)
    agents.agents["AST-bud"] = _agent("Budget", published=None, owner=author_user.user_id)

    reviewer, budget = _sheet(client, *_two())["workflow"]["steps"]

    assert reviewer["agent"]["runsDraft"] is False
    assert reviewer["pin"]["canRun"] is True
    assert budget["agent"]["runsDraft"] is True, "an Agent never published records no version and runs live"


def test_an_agent_the_caller_cant_use_gets_no_pin_view(client, agents, monkeypatch):
    _pin_on(monkeypatch)
    del agents.agents["AST-rev"]

    doc = _sheet(client, *_two())

    assert "pin" not in doc["workflow"]["steps"][0]
    assert [b["code"] for b in doc["sheet"]["blockers"]] == ["step_unavailable"]


def test_use_version_n_for_an_editor_is_the_ordinary_put(client, agents, monkeypatch):
    """The file moves to the offered version; nothing new on the write path."""
    _pin_on(monkeypatch)
    agents.agents["AST-rev"] = _agent("Reviewer", published=2)
    body = {"title": "Proposal review", "request": "Review it", "steps": list(_two())}
    assert client.put(client.path, json=body).json()["sheet"]["startable"] is False

    body["steps"][0]["agentVersion"] = 2
    agents.rows[("AST-rev", 2)] = SimpleNamespace(published_at=STAMP)
    saved = client.put(client.path, json=body).json()

    assert saved["written"] is True
    assert saved["workflow"]["steps"][0]["pin"] == {"version": 2, "canRun": True}
    assert saved["sheet"]["startable"] is True
