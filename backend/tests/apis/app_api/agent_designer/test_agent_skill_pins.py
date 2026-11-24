"""Moving an Agent's skill pin, and reporting its pins (skill-version-history.md D9).

``pin_agent_skill`` is the Agent twin of a project's Update: owner or editor, never a
project harness, only a pinned binding, after the caller is re-checked for the
skill. These pin its rules with the shared services stubbed, and the two routes'
flag and wiring through a TestClient.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agent_designer import routes as agent_routes
from apis.app_api.agent_designer.services import skill_pins
from apis.app_api.agent_designer.services.binding_validation import BindingValidationError
from apis.shared.assistants.models import AgentBinding
from apis.shared.auth.models import User
from apis.shared.skills.pinning import PinStatus

USER = User(user_id="u1", email="u1@example.com", name="U", roles=["user"], raw_token="t")


def _agent(bindings, **kw):
    return SimpleNamespace(
        assistant_id="a1", owner_id="u1", bindings=bindings, kind=kw.get("kind"), **{
            k: v for k, v in kw.items() if k != "kind"
        }
    )


@pytest.fixture()
def stubs(monkeypatch):
    calls = SimpleNamespace(updated=None, validated=[], permission="owner", agent=None, harness=False)

    async def permission(assistant_id, user_id, user_email):
        return calls.agent, calls.permission

    async def validate(user, *, bindings=None, **kw):
        calls.validated.append([b.ref for b in bindings or []])

    async def pin(skill_id, *, pinned_by):
        return SimpleNamespace(version=7)

    async def update(assistant_id, owner_id, bindings=None, **kw):
        calls.updated = bindings
        return _agent(bindings)

    monkeypatch.setattr(skill_pins, "resolve_assistant_permission", permission)
    monkeypatch.setattr(skill_pins, "validate_agent_write", validate)
    monkeypatch.setattr(skill_pins, "pin_current", pin)
    monkeypatch.setattr(skill_pins, "update_assistant", update)
    monkeypatch.setattr(skill_pins, "is_project_harness", lambda a: calls.harness)
    return calls


def _pin(skill_id="grant_review"):
    return asyncio.run(skill_pins.pin_agent_skill("a1", USER, skill_id))


def _status(exc_info):
    return exc_info.value.status_code


class TestPinAgentSkill:
    def test_moves_the_pin_and_leaves_other_bindings_alone(self, stubs):
        other = AgentBinding(kind="tool", ref="web_search")
        stubs.agent = _agent(
            [AgentBinding(kind="skill", ref="grant_review", config={"version": 3, "addedBy": "x"}), other]
        )
        _, version = _pin()
        assert version == 7
        assert stubs.validated == [["grant_review"]]
        moved, kept = stubs.updated
        assert moved.config == {"version": 7, "addedBy": "x"}
        assert kept is other

    def test_refuses_what_it_should(self, stubs):
        stubs.agent = None
        with pytest.raises(skill_pins.AgentSkillPinError) as e:
            _pin()
        assert _status(e) == 404

        stubs.agent = _agent([AgentBinding(kind="skill", ref="grant_review", config={"version": 3})])
        stubs.permission = "viewer"
        with pytest.raises(skill_pins.AgentSkillPinError) as e:
            _pin()
        assert _status(e) == 403

        stubs.permission = "editor"
        stubs.harness = True
        with pytest.raises(skill_pins.AgentSkillPinError) as e:
            _pin()
        assert _status(e) == 409

        stubs.harness = False
        with pytest.raises(skill_pins.AgentSkillPinError) as e:
            _pin("not_bound")
        assert _status(e) == 404

        stubs.agent = _agent([AgentBinding(kind="skill", ref="grant_review", config={})])
        with pytest.raises(skill_pins.AgentSkillPinError) as e:
            _pin()
        assert _status(e) == 409
        assert stubs.updated is None

    def test_a_caller_who_may_no_longer_bind_the_skill_is_refused(self, stubs, monkeypatch):
        stubs.agent = _agent([AgentBinding(kind="skill", ref="grant_review", config={"version": 3})])

        async def denied(user, **kw):
            raise BindingValidationError("You no longer have this skill", status_code=403)

        monkeypatch.setattr(skill_pins, "validate_agent_write", denied)
        with pytest.raises(skill_pins.AgentSkillPinError) as e:
            _pin()
        assert _status(e) == 403
        assert stubs.updated is None


def test_agent_skill_pins_reports_only_pinned_skills(monkeypatch):
    async def status(pairs):
        return {sid: PinStatus(n, None, sid == "behind") for sid, n in pairs}

    monkeypatch.setattr(skill_pins, "pin_status", status)
    agent = _agent([
        AgentBinding(kind="skill", ref="behind", config={"version": 2}),
        AgentBinding(kind="skill", ref="fresh", config={"version": 4}),
        AgentBinding(kind="skill", ref="live", config={}),
        AgentBinding(kind="tool", ref="web_search"),
    ])
    pins = asyncio.run(skill_pins.agent_skill_pins(agent))
    assert [(p.skill_id, p.version, p.update_available) for p in pins] == [
        ("behind", 2, True),
        ("fresh", 4, False),
    ]


class TestRoutes:
    @pytest.fixture()
    def client(self, monkeypatch):
        monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
        app = FastAPI()
        app.include_router(agent_routes.router)
        app.dependency_overrides[agent_routes.require_agents_enabled] = lambda: USER
        with TestClient(app) as c:
            yield c

    def test_pin_route_returns_the_new_version(self, client, monkeypatch):
        async def pin(agent_id, user, skill_id):
            return SimpleNamespace(), 7

        monkeypatch.setattr(agent_routes, "pin_agent_skill", pin)
        resp = client.post("/agents/a1/skills/grant_review/pin")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"skillId": "grant_review", "version": 7}

    def test_pin_route_maps_the_service_status(self, client, monkeypatch):
        async def refused(agent_id, user, skill_id):
            raise skill_pins.AgentSkillPinError("not yours", 403)

        monkeypatch.setattr(agent_routes, "pin_agent_skill", refused)
        assert client.post("/agents/a1/skills/grant_review/pin").status_code == 403

    def test_pins_route_is_for_owners_and_editors(self, client, monkeypatch):
        async def permission(assistant_id, user_id, user_email):
            return _agent([]), "viewer"

        monkeypatch.setattr(agent_routes, "resolve_assistant_permission", permission)
        assert client.get("/agents/a1/skills/pins").status_code == 403

    def test_flag_off_is_404(self, client, monkeypatch):
        monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "")
        assert client.post("/agents/a1/skills/grant_review/pin").status_code == 404
        assert client.get("/agents/a1/skills/pins").status_code == 404
