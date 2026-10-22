"""Tests for assistants routes.

Endpoints under test:
- GET /assistants  → 200 with assistant list (authenticated)
- GET /assistants  → 401 for unauthenticated request

Requirements: 13.1, 13.2
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.assistants.routes import router
from apis.shared.assistants.models import (
    Assistant,
    AssistantResponse,
    AssistantsListResponse,
)
from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROUTES_MODULE = "apis.app_api.assistants.routes"


def _make_assistant(**overrides) -> Assistant:
    """Create a sample Assistant model for testing."""
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-001",
        ownerName="Test User",
        name="My Assistant",
        description="A helpful assistant",
        instructions="You are a helpful assistant.",
        vectorIndexId="idx-001",
        visibility="PRIVATE",
        tags=["test"],
        starters=["Hello"],
        emoji="🤖",
        usageCount=0,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the assistants router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 13.1: Assistants endpoint returns 200 with assistant data
# ---------------------------------------------------------------------------


class TestListAssistantsAuthenticated:
    """GET /assistants returns 200 with assistant data for authenticated user."""

    def test_returns_200_with_assistants(self, app, make_user):
        """Req 13.1: Authenticated user gets 200 with assistant list."""
        user = make_user()
        mock_auth_user(app, user)

        sample = _make_assistant()

        with patch(
            f"{ROUTES_MODULE}.list_user_assistants",
            new_callable=AsyncMock,
            return_value=([sample], None),
        ), patch(
            f"{ROUTES_MODULE}.list_shared_with_user",
            new_callable=AsyncMock,
            return_value=[],
        ):
            client = TestClient(app)
            resp = client.get("/assistants")

        assert resp.status_code == 200
        body = resp.json()
        assert "assistants" in body
        assert len(body["assistants"]) == 1
        assert body["assistants"][0]["name"] == "My Assistant"

    def test_returns_200_with_empty_list(self, app, make_user):
        """Req 13.1: Authenticated user gets 200 with empty list when no assistants."""
        user = make_user()
        mock_auth_user(app, user)

        with patch(
            f"{ROUTES_MODULE}.list_user_assistants",
            new_callable=AsyncMock,
            return_value=([], None),
        ), patch(
            f"{ROUTES_MODULE}.list_shared_with_user",
            new_callable=AsyncMock,
            return_value=[],
        ):
            client = TestClient(app)
            resp = client.get("/assistants")

        assert resp.status_code == 200
        body = resp.json()
        assert body["assistants"] == []


class TestGetAssistantProjectHarness:
    """GET /assistants/{id} says when the agent is a project's harness.

    A project task binds the harness, and the chat's crumb renders it as the project
    rather than an Agent with Edit / Share — both of which the harness refuses.
    """

    def _get(self, app, make_user, assistant):
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check",
            new_callable=AsyncMock,
            return_value=(assistant, "viewer"),
        ):
            return TestClient(app).get(f"/assistants/{assistant.assistant_id}")

    def test_harness_carries_kind_and_project_id(self, app, make_user):
        resp = self._get(app, make_user, _make_assistant(kind="project", projectId="prj_1"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "project"
        assert body["projectId"] == "prj_1"

    def test_ordinary_agent_omits_both(self, app, make_user):
        resp = self._get(app, make_user, _make_assistant())

        assert resp.status_code == 200
        body = resp.json()
        assert "kind" not in body
        assert "projectId" not in body


# ---------------------------------------------------------------------------
# Requirement 13.2: Assistants endpoint returns 401 for unauthenticated
# ---------------------------------------------------------------------------


class TestListAssistantsUnauthenticated:
    """GET /assistants returns 401 for unauthenticated request."""

    def test_returns_401_unauthenticated(self, app, unauthenticated_client):
        """Req 13.2: Unauthenticated request gets 401."""
        client = unauthenticated_client(app)
        resp = client.get("/assistants")

        assert resp.status_code == 401
