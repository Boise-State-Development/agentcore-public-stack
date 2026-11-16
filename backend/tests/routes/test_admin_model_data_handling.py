"""Enabling a provider_review model needs the admin's acknowledgement, and is audited.

apis/shared/models/data_handling.py: a covered model (Fable 5.x, Mythos 5.x)
keeps conversations for provider review, so the admin routes refuse to put one
in users' reach without `acknowledgeDataHandling: true`, refuse it as the
default or as a retirement successor, and write an audit record when it lands.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.routes import router
from apis.shared.models.models import ManagedModel
from tests.conftest import override_admin_auth

ROUTES = "apis.app_api.admin.routes"
FABLE = "global.anthropic.claude-fable-5-2"


def _row(model_id=FABLE, *, enabled=False, **kw) -> ManagedModel:
    return ManagedModel(
        id="row-1", modelId=model_id, modelName="Claude Fable 5.2", provider="bedrock",
        providerName="Anthropic", inputModalities=["TEXT"], outputModalities=["TEXT"],
        maxInputTokens=1_000_000, maxOutputTokens=128_000, allowedAppRoles=[], availableToRoles=[],
        enabled=enabled, inputPricePerMillionTokens=8.0, outputPricePerMillionTokens=40.0,
        supportsCaching=True, isDefault=False, createdAt=datetime(2026, 11, 16),
        updatedAt=datetime(2026, 11, 16), **kw,
    )


CREATE_BODY = {
    "modelId": FABLE, "modelName": "Claude Fable 5.2", "provider": "bedrock",
    "providerName": "Anthropic", "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
    "maxInputTokens": 1_000_000, "maxOutputTokens": 128_000,
    "inputPricePerMillionTokens": 8.0, "outputPricePerMillionTokens": 40.0, "enabled": True,
}


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(router)
    override_admin_auth(app, lambda: make_user(roles=["Admin"], email="admin@example.com"))
    return TestClient(app)


@pytest.fixture(autouse=True)
def stubs():
    roles = AsyncMock()
    roles.hydrate_model_roles.side_effect = lambda models: models
    roles.get_roles_for_model.return_value = []
    audit = MagicMock()
    with patch(f"{ROUTES}.get_model_role_service", return_value=roles), \
         patch(f"{ROUTES}.list_all_managed_models", new_callable=AsyncMock, return_value=[]) as catalog, \
         patch(f"{ROUTES}.get_audit_service", return_value=audit):
        yield {"catalog": catalog, "audit": audit}


class TestCreate:
    def test_an_enabled_covered_model_needs_the_acknowledgement(self, client, stubs):
        with patch(f"{ROUTES}.create_managed_model", new_callable=AsyncMock) as create:
            resp = client.post("/admin/managed-models", json=CREATE_BODY)
        assert resp.status_code == 409
        assert "acknowledgeDataHandling" in resp.json()["detail"]
        create.assert_not_called()
        stubs["audit"].record.assert_not_called()

    def test_with_it_the_create_lands_and_is_audited(self, client, stubs):
        with patch(f"{ROUTES}.create_managed_model", new_callable=AsyncMock,
                   return_value=_row(enabled=True)):
            resp = client.post("/admin/managed-models", json={**CREATE_BODY, "acknowledgeDataHandling": True})
        assert resp.status_code == 201, resp.text
        assert resp.json()["effectiveDataHandling"] == "provider_review"
        kwargs = stubs["audit"].record.call_args.kwargs
        assert kwargs["action"] == "managed_model.provider_review_enabled"
        assert kwargs["target_type"] == "managed_model" and kwargs["target_id"] == "row-1"
        assert kwargs["after"]["noticeDigest"] == resp.json()["dataHandlingNoticeDigest"]

    def test_a_disabled_covered_model_needs_nothing(self, client, stubs):
        with patch(f"{ROUTES}.create_managed_model", new_callable=AsyncMock, return_value=_row()):
            resp = client.post("/admin/managed-models", json={**CREATE_BODY, "enabled": False})
        assert resp.status_code == 201, resp.text
        stubs["audit"].record.assert_not_called()

    def test_a_covered_model_cannot_be_the_default(self, client):
        body = {**CREATE_BODY, "isDefault": True, "acknowledgeDataHandling": True}
        resp = client.post("/admin/managed-models", json=body)
        assert resp.status_code == 400
        assert "cannot be the default" in resp.json()["detail"]


class TestUpdate:
    def test_enabling_needs_the_acknowledgement(self, client, stubs):
        with patch(f"{ROUTES}.get_managed_model", new_callable=AsyncMock, return_value=_row()), \
             patch(f"{ROUTES}.update_managed_model", new_callable=AsyncMock) as update:
            resp = client.put("/admin/managed-models/row-1", json={"enabled": True})
        assert resp.status_code == 409
        update.assert_not_called()

    def test_with_it_the_enable_is_audited_with_the_before(self, client, stubs):
        with patch(f"{ROUTES}.get_managed_model", new_callable=AsyncMock, return_value=_row()), \
             patch(f"{ROUTES}.update_managed_model", new_callable=AsyncMock, return_value=_row(enabled=True)):
            resp = client.put("/admin/managed-models/row-1", json={"enabled": True, "acknowledgeDataHandling": True})
        assert resp.status_code == 200, resp.text
        kwargs = stubs["audit"].record.call_args.kwargs
        assert kwargs["before"] == {"enabled": False, "dataHandling": "provider_review"}
        assert kwargs["after"]["enabled"] is True

    def test_marking_an_enabled_model_needs_it_too(self, client):
        opus = _row("us.anthropic.claude-opus-5-5", enabled=True)
        with patch(f"{ROUTES}.get_managed_model", new_callable=AsyncMock, return_value=opus), \
             patch(f"{ROUTES}.update_managed_model", new_callable=AsyncMock) as update:
            resp = client.put("/admin/managed-models/row-1", json={"dataHandling": "provider_review"})
        assert resp.status_code == 409
        update.assert_not_called()

    def test_an_edit_to_an_already_enabled_covered_model_needs_nothing(self, client, stubs):
        live = _row(enabled=True)
        with patch(f"{ROUTES}.get_managed_model", new_callable=AsyncMock, return_value=live), \
             patch(f"{ROUTES}.update_managed_model", new_callable=AsyncMock, return_value=live):
            resp = client.put("/admin/managed-models/row-1", json={"modelName": "Fable"})
        assert resp.status_code == 200, resp.text
        stubs["audit"].record.assert_not_called()

    def test_no_model_may_retire_into_a_covered_one(self, client, stubs):
        stubs["catalog"].return_value = [_row(enabled=True)]
        old = _row("us.anthropic.claude-opus-4-7", enabled=True)
        with patch(f"{ROUTES}.get_managed_model", new_callable=AsyncMock, return_value=old):
            resp = client.put("/admin/managed-models/row-1", json={"status": "deprecated", "replacedBy": FABLE})
        assert resp.status_code == 400
        assert "provider review" in resp.json()["detail"]
