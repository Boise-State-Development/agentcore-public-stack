"""Tests for admin routes.

Endpoints under test:
- GET  /admin/managed-models          → 200 with model list (admin)
- POST /admin/managed-models          → 201 with created model (admin)
- DELETE /admin/managed-models/{id}   → 204 (admin)
- All admin endpoints                 → 403 for non-admin / no roles
- All admin endpoints                 → 401 for unauthenticated

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.routes import router
from apis.shared.auth.rbac import require_admin
from apis.shared.models.models import ManagedModel
from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MANAGED_MODELS_PATH = "apis.app_api.admin.routes"

SAMPLE_MODEL = ManagedModel(
    id="model-001",
    modelId="anthropic.claude-3-haiku",
    modelName="Claude 3 Haiku",
    provider="bedrock",
    providerName="Anthropic",
    inputModalities=["TEXT"],
    outputModalities=["TEXT"],
    maxInputTokens=200000,
    maxOutputTokens=4096,
    allowedAppRoles=["Admin"],
    availableToRoles=[],
    enabled=True,
    inputPricePerMillionTokens=0.25,
    outputPricePerMillionTokens=1.25,
    supportsCaching=True,
    isDefault=False,
    createdAt=datetime(2024, 1, 1),
    updatedAt=datetime(2024, 1, 1),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the admin router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


def _override_require_admin(app: FastAPI, user):
    """Override the require_admin dependency to return the given user."""
    app.dependency_overrides[require_admin] = lambda: user


def _override_require_admin_403(app: FastAPI):
    """Override require_admin to raise 403."""
    from fastapi import HTTPException, status

    def _raise():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    app.dependency_overrides[require_admin] = _raise


# ---------------------------------------------------------------------------
# Requirement 7.1: Admin endpoint returns 200 for user with Admin role
# ---------------------------------------------------------------------------


class TestAdminAccess:
    """Admin endpoints return 200 for users with Admin role."""

    def test_admin_managed_models_returns_200(self, app, make_user):
        """Req 7.1: Admin user can access managed-models endpoint."""
        admin = make_user(email="admin@example.com", user_id="admin-001", roles=["Admin"])
        _override_require_admin(app, admin)

        with patch(
            f"{MANAGED_MODELS_PATH}.list_managed_models",
            new_callable=AsyncMock,
            return_value=[SAMPLE_MODEL],
        ):
            client = TestClient(app)
            resp = client.get("/admin/managed-models")

        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert body["totalCount"] >= 1


# ---------------------------------------------------------------------------
# Requirement 7.2: Admin endpoint returns 403 for user without Admin role
# ---------------------------------------------------------------------------


class TestNonAdminRejection:
    """Admin endpoints return 403 for users without Admin role."""

    def test_managed_models_returns_403_for_non_admin(self, app, make_user):
        """Req 7.2: Non-admin user gets 403."""
        user = make_user(roles=["User"])
        mock_auth_user(app, user)
        # Do NOT override require_admin — let the real dependency run
        # Since require_admin depends on get_current_user (which we mocked),
        # it will check roles and reject.

        client = TestClient(app)
        resp = client.get("/admin/managed-models")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Requirement 7.3: Admin endpoint returns 403 for user with no roles
# ---------------------------------------------------------------------------


class TestNoRolesRejection:
    """Admin endpoints return 403 for users with empty roles."""

    def test_managed_models_returns_403_for_no_roles(self, app, make_user):
        """Req 7.3: User with no roles gets 403."""
        user = make_user(email="norole@example.com", user_id="norole-001", roles=[])
        mock_auth_user(app, user)

        client = TestClient(app)
        resp = client.get("/admin/managed-models")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Requirement 7.4: Admin endpoint returns 401 for unauthenticated request
# ---------------------------------------------------------------------------


class TestUnauthenticatedRejection:
    """Admin endpoints return 401 for unauthenticated requests."""

    def test_managed_models_returns_401_unauthenticated(self, app, unauthenticated_client):
        """Req 7.4: Unauthenticated request gets 401."""
        client = unauthenticated_client(app)
        resp = client.get("/admin/managed-models")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Requirement 7.5: GET managed models returns 200 with model list for admin
# ---------------------------------------------------------------------------


class TestListManagedModels:
    """GET /admin/managed-models returns model list for admin."""

    def test_returns_model_list(self, app, make_user):
        """Req 7.5: Admin gets 200 with list of managed models."""
        admin = make_user(email="admin@example.com", user_id="admin-001", roles=["Admin"])
        _override_require_admin(app, admin)

        models = [SAMPLE_MODEL]
        with patch(
            f"{MANAGED_MODELS_PATH}.list_managed_models",
            new_callable=AsyncMock,
            return_value=models,
        ):
            client = TestClient(app)
            resp = client.get("/admin/managed-models")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["models"]) == 1
        assert body["models"][0]["modelId"] == "anthropic.claude-3-haiku"
        assert body["totalCount"] == 1

    def test_returns_empty_list(self, app, make_user):
        """Req 7.5: Admin gets 200 with empty list when no models exist."""
        admin = make_user(email="admin@example.com", user_id="admin-001", roles=["Admin"])
        _override_require_admin(app, admin)

        with patch(
            f"{MANAGED_MODELS_PATH}.list_managed_models",
            new_callable=AsyncMock,
            return_value=[],
        ):
            client = TestClient(app)
            resp = client.get("/admin/managed-models")

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"] == []
        assert body["totalCount"] == 0


# ---------------------------------------------------------------------------
# Requirement 7.6: POST create managed model returns 201 for admin
# ---------------------------------------------------------------------------


class TestCreateManagedModel:
    """POST /admin/managed-models creates a model for admin."""

    def test_create_returns_201(self, app, make_user):
        """Req 7.6: Admin can create a managed model."""
        admin = make_user(email="admin@example.com", user_id="admin-001", roles=["Admin"])
        _override_require_admin(app, admin)

        with patch(
            f"{MANAGED_MODELS_PATH}.create_managed_model",
            new_callable=AsyncMock,
            return_value=SAMPLE_MODEL,
        ):
            client = TestClient(app)
            resp = client.post(
                "/admin/managed-models",
                json={
                    "modelId": "anthropic.claude-3-haiku",
                    "modelName": "Claude 3 Haiku",
                    "provider": "bedrock",
                    "providerName": "Anthropic",
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"],
                    "maxInputTokens": 200000,
                    "maxOutputTokens": 4096,
                    "inputPricePerMillionTokens": 0.25,
                    "outputPricePerMillionTokens": 1.25,
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["modelId"] == "anthropic.claude-3-haiku"


# ---------------------------------------------------------------------------
# Requirement 7.7: DELETE managed model returns 204 for admin
# ---------------------------------------------------------------------------


class TestDeleteManagedModel:
    """DELETE /admin/managed-models/{model_id} removes a model for admin."""

    def test_delete_returns_204(self, app, make_user):
        """Req 7.7: Admin can delete a managed model."""
        admin = make_user(email="admin@example.com", user_id="admin-001", roles=["Admin"])
        _override_require_admin(app, admin)

        with patch(
            f"{MANAGED_MODELS_PATH}.delete_managed_model",
            new_callable=AsyncMock,
            return_value=True,
        ):
            client = TestClient(app)
            resp = client.delete("/admin/managed-models/model-001")

        assert resp.status_code == 204
