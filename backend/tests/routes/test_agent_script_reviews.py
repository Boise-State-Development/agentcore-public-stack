"""An Agent save records the script reviews it implies (skill-code-sandbox §8.5).

The review logic itself is ``tests/shared/test_script_reviews.py``; this pins the
wiring: both Agent write routes hand the stored bindings and the saver's
``scriptsReviewed`` to ``record_binding_reviews``, and a save that doesn't touch
bindings records nothing.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agent_designer.routes import router as agents_router
from apis.shared.assistants.models import AgentBinding, Assistant
from tests.routes.conftest import mock_auth_user

ROUTES = "apis.app_api.agent_designer.routes"


def _assistant(bindings):
    return Assistant.model_validate(dict(
        assistantId="ast-001", ownerId="user-001", ownerName="Pat", name="Forms", description="d",
        instructions="i", vectorIndexId="idx", visibility="PRIVATE", usageCount=0,
        createdAt="2026-11-23T00:00:00Z", updatedAt="2026-11-23T00:00:00Z", status="COMPLETE",
        bindings=[b.model_dump(by_alias=True) for b in bindings],
    ))


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(agents_router)
    mock_auth_user(app, make_user(user_id="user-001", email="pat@example.edu"))
    return TestClient(app)


def test_an_update_hands_the_bindings_and_reviews_to_the_hook(client):
    before = _assistant([])
    after = _assistant([AgentBinding(kind="skill", ref="pdf", config={"version": 3})])
    hook = AsyncMock(return_value=[])
    with patch(f"{ROUTES}.resolve_assistant_permission", AsyncMock(return_value=(before, "owner"))), \
         patch(f"{ROUTES}.validate_agent_write", AsyncMock()), \
         patch(f"{ROUTES}.pin_imported_skills", AsyncMock(side_effect=lambda u, b, *a: b)), \
         patch(f"{ROUTES}.update_assistant", AsyncMock(return_value=after)), \
         patch(f"{ROUTES}.record_binding_reviews", hook):
        response = client.put(
            "/agents/ast-001",
            json={"bindings": [{"kind": "skill", "ref": "pdf"}], "scriptsReviewed": {"pdf": "a" * 64}},
        )
    assert response.status_code == 200, response.text
    (call,) = hook.await_args_list
    assert call.args[0] == "ast-001"
    assert call.args[1] == before.bindings and call.args[2] == after.bindings
    assert call.kwargs["scripts_reviewed"] == {"pdf": "a" * 64}


def test_a_save_without_bindings_records_nothing(client):
    agent = _assistant([])
    hook = AsyncMock(return_value=[])
    with patch(f"{ROUTES}.resolve_assistant_permission", AsyncMock(return_value=(agent, "owner"))), \
         patch(f"{ROUTES}.validate_agent_write", AsyncMock()), \
         patch(f"{ROUTES}.pin_imported_skills", AsyncMock(side_effect=lambda u, b, *a: b)), \
         patch(f"{ROUTES}.update_assistant", AsyncMock(return_value=agent)), \
         patch(f"{ROUTES}.record_binding_reviews", hook):
        assert client.put("/agents/ast-001", json={"name": "Renamed"}).status_code == 200
    hook.assert_not_awaited()
