"""Tests for the /agents alias surface (Agent Designer Phase 1, PR-3).

Covers the AGENTS_API_ENABLED 404-gate, the Agent projection (agentId + bindings +
modelConfig), and CRUD/shares parity with /assistants. Services are patched in the
agents routes module namespace.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agents.routes import router
from apis.shared.assistants.models import AgentBinding, AgentModelConfig, Assistant
from tests.routes.conftest import mock_auth_user

ROUTES_MODULE = "apis.app_api.agents.routes"


def _make_assistant(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-001",
        ownerName="Test User",
        name="My Agent",
        description="A helpful agent",
        instructions="You are helpful.",
        vectorIndexId="idx-001",
        visibility="PRIVATE",
        tags=["test"],
        starters=["Hi"],
        emoji="🤖",
        usageCount=0,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def _flag_on(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")


# --------------------------------------------------------------------------- gate
class TestFeatureGate:
    def test_404_when_flag_off(self, app, make_user, monkeypatch):
        monkeypatch.setenv("AGENTS_API_ENABLED", "false")
        mock_auth_user(app, make_user())  # auth passes; the gate itself 404s
        with patch(f"{ROUTES_MODULE}.list_user_assistants", new_callable=AsyncMock, return_value=([], None)):
            resp = TestClient(app).get("/agents")
        assert resp.status_code == 404

    def test_available_when_flag_on(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.list_user_assistants", new_callable=AsyncMock, return_value=([], None)), patch(
            f"{ROUTES_MODULE}.list_shared_with_user", new_callable=AsyncMock, return_value=[]
        ):
            resp = TestClient(app).get("/agents")
        assert resp.status_code == 200
        assert resp.json()["agents"] == []


# --------------------------------------------------------------------------- projection
class TestAgentProjection:
    def test_list_projects_agent_id_and_synthesizes_kb_binding(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        with patch(
            f"{ROUTES_MODULE}.list_user_assistants", new_callable=AsyncMock, return_value=([_make_assistant()], None)
        ), patch(f"{ROUTES_MODULE}.list_shared_with_user", new_callable=AsyncMock, return_value=[]):
            resp = TestClient(app).get("/agents")
        assert resp.status_code == 200
        agent = resp.json()["agents"][0]
        assert agent["agentId"] == "ast-001"
        assert "assistantId" not in agent
        # Legacy row → compat synthesizes a knowledge_base binding reffing the id.
        assert agent["bindings"] == [
            {"kind": "knowledge_base", "ref": "ast-001", "config": {"vectorIndexId": "idx-001"}}
        ]

    def test_get_exposes_bindings_and_modelconfig(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        agent = _make_assistant(
            bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "readwrite"})],
            model_settings=AgentModelConfig(model_id="m1", params={"temperature": 0.7}),
        )
        with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check", new_callable=AsyncMock, return_value=(agent, "owner")
        ):
            resp = TestClient(app).get("/agents/ast-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["modelConfig"] == {"modelId": "m1", "params": {"temperature": 0.7}}
        assert body["bindings"][0]["kind"] == "memory_space"
        assert body["userPermission"] == "owner"

    def test_get_404_when_missing(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=False):
            resp = TestClient(app).get("/agents/ghost")
        assert resp.status_code == 404

    def test_get_403_when_access_denied(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check", new_callable=AsyncMock, return_value=(None, None)
        ):
            resp = TestClient(app).get("/agents/ast-001")
        assert resp.status_code == 403


# --------------------------------------------------------------------------- writes
class TestAgentWrites:
    def test_create_validates_then_persists(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        created = _make_assistant()
        with patch(f"{ROUTES_MODULE}.validate_agent_write", new_callable=AsyncMock) as v, patch(
            f"{ROUTES_MODULE}.create_assistant", new_callable=AsyncMock, return_value=created
        ):
            resp = TestClient(app).post(
                "/agents", json={"name": "My Agent", "description": "d", "instructions": "i"}
            )
        assert resp.status_code == 200
        assert resp.json()["agentId"] == "ast-001"
        v.assert_awaited_once()

    def test_create_surfaces_validation_403(self, app, make_user, _flag_on):
        from apis.app_api.agents.services.binding_validation import BindingValidationError

        mock_auth_user(app, make_user())
        with patch(
            f"{ROUTES_MODULE}.validate_agent_write",
            new_callable=AsyncMock,
            side_effect=BindingValidationError("no access to model 'm1'", status_code=403),
        ):
            resp = TestClient(app).post(
                "/agents",
                json={"name": "X", "description": "d", "instructions": "i", "modelConfig": {"modelId": "m1"}},
            )
        assert resp.status_code == 403
        assert "no access" in resp.json()["detail"]

    def test_update_gates_on_permission(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        existing = _make_assistant()
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(existing, "viewer"),
        ):
            resp = TestClient(app).put("/agents/ast-001", json={"name": "New"})
        assert resp.status_code == 403

    def test_delete_204(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.delete_assistant", new_callable=AsyncMock, return_value=True):
            resp = TestClient(app).delete("/agents/ast-001")
        assert resp.status_code == 204


# --------------------------------------------------------------------------- shares
class TestAgentShares:
    def test_get_shares_projects_agent_id(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        existing = _make_assistant()
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(existing, "owner"),
        ), patch(
            f"{ROUTES_MODULE}.list_assistant_shares",
            new_callable=AsyncMock,
            return_value=[{"email": "bob@x.edu", "permission": "viewer"}],
        ):
            resp = TestClient(app).get("/agents/ast-001/shares")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agentId"] == "ast-001"
        assert body["sharedWith"] == [{"email": "bob@x.edu", "permission": "viewer"}]

    def test_share_404_when_not_owned(self, app, make_user, _flag_on):
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.share_assistant", new_callable=AsyncMock, return_value=False):
            resp = TestClient(app).post("/agents/ast-001/shares", json={"emails": ["b@x.edu"], "permission": "viewer"})
        assert resp.status_code == 404
