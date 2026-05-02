"""Tests for models routes.

Endpoints under test:
- GET /models  → 200 with accessible models (authenticated)
- GET /models  → 401 for unauthenticated request

Requirements: 12.1, 12.2
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.models.routes import router
from apis.app_api.admin.services.model_access import (
    ModelAccessService,
    get_model_access_service,
)
from apis.shared.models.models import ManagedModel
from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROUTES_MODULE = "apis.app_api.models.routes"

SAMPLE_MODEL = ManagedModel(
    id="model-1",
    modelId="anthropic.claude-3-haiku",
    modelName="Claude 3 Haiku",
    provider="bedrock",
    providerName="Amazon Bedrock",
    inputModalities=["text"],
    outputModalities=["text"],
    maxInputTokens=200000,
    maxOutputTokens=4096,
    allowedAppRoles=["User"],
    availableToRoles=["User"],
    enabled=True,
    inputPricePerMillionTokens=0.25,
    outputPricePerMillionTokens=1.25,
    createdAt=datetime(2024, 1, 1),
    updatedAt=datetime(2024, 1, 1),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the models router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 12.1: GET /models returns 200 with model data for authenticated user
# ---------------------------------------------------------------------------


class TestGetModelsAuthenticated:
    """GET /models returns 200 with model data for authenticated user."""

    def test_returns_200_with_models(self, app, make_user):
        """Req 12.1: Authenticated user gets 200 with model list."""
        user = make_user()
        mock_auth_user(app, user)

        mock_service = MagicMock(spec=ModelAccessService)
        mock_service.filter_accessible_models = AsyncMock(
            return_value=[SAMPLE_MODEL]
        )

        with patch(
            f"{ROUTES_MODULE}.list_all_managed_models",
            new_callable=AsyncMock,
            return_value=[SAMPLE_MODEL],
        ), patch(
            f"{ROUTES_MODULE}.get_model_access_service",
            return_value=mock_service,
        ):
            client = TestClient(app)
            resp = client.get("/models")

        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert "totalCount" in body
        assert len(body["models"]) == 1
        assert body["totalCount"] == 1
        assert body["models"][0]["modelId"] == "anthropic.claude-3-haiku"

    def test_returns_200_with_empty_models(self, app, make_user):
        """Req 12.1: Authenticated user gets 200 with empty list when no models accessible."""
        user = make_user()
        mock_auth_user(app, user)

        mock_service = MagicMock(spec=ModelAccessService)
        mock_service.filter_accessible_models = AsyncMock(return_value=[])

        with patch(
            f"{ROUTES_MODULE}.list_all_managed_models",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            f"{ROUTES_MODULE}.get_model_access_service",
            return_value=mock_service,
        ):
            client = TestClient(app)
            resp = client.get("/models")

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"] == []
        assert body["totalCount"] == 0


# ---------------------------------------------------------------------------
# Requirement 12.2: GET /models returns 401 for unauthenticated request
# ---------------------------------------------------------------------------


class TestGetModelsUnauthenticated:
    """GET /models returns 401 for unauthenticated request."""

    def test_returns_401_unauthenticated(self, app, unauthenticated_client):
        """Req 12.2: Unauthenticated request gets 401."""
        client = unauthenticated_client(app)
        resp = client.get("/models")

        assert resp.status_code == 401
