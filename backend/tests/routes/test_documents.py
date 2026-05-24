"""Tests for documents routes.

Endpoints under test:
- GET  /assistants/{assistant_id}/documents  → 200 with document list (authenticated)
- GET  /assistants/{assistant_id}/documents  → 401 for unauthenticated request

Requirements: 14.1, 14.2
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.documents.routes import router
from apis.app_api.documents.models import Document
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from tests.routes.conftest import mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROUTES_MODULE = "apis.app_api.documents.routes"
ASSISTANT_ID = "ast-001"
USER_ID = "user-001"


def _make_document(**overrides) -> Document:
    """Create a sample Document model for testing."""
    defaults = dict(
        documentId="doc-001",
        assistantId=ASSISTANT_ID,
        filename="report.pdf",
        contentType="application/pdf",
        sizeBytes=1024,
        s3Key=f"assistants/{ASSISTANT_ID}/documents/doc-001/report.pdf",
        status="complete",
        chunkCount=5,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return Document.model_validate(defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the documents router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


def _override_user_id(app: FastAPI, user_id: str = USER_ID) -> None:
    """Override the session-cookie dependency with a fixed User."""
    app.dependency_overrides[get_current_user_from_session] = lambda: User(
        user_id=user_id, email=f"{user_id}@example.com", name="Test User", roles=["User"]
    )


def _owner_resolve(user_id: str = USER_ID):
    """Build a resolve_assistant_permission return value for an owner."""
    return (SimpleNamespace(owner_id=user_id), "owner")


# ---------------------------------------------------------------------------
# Requirement 14.1: Documents endpoint returns 200 with document data
# ---------------------------------------------------------------------------


class TestListDocumentsAuthenticated:
    """GET /assistants/{id}/documents returns 200 with document data for authenticated user."""

    def test_returns_200_with_documents(self, app):
        """Req 14.1: Authenticated user gets 200 with document list."""
        _override_user_id(app)

        sample = _make_document()

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=([sample], None),
        ):
            client = TestClient(app)
            resp = client.get(f"/assistants/{ASSISTANT_ID}/documents")

        assert resp.status_code == 200
        body = resp.json()
        assert "documents" in body
        assert len(body["documents"]) == 1
        assert body["documents"][0]["filename"] == "report.pdf"

    def test_returns_200_with_empty_list(self, app):
        """Req 14.1: Authenticated user gets 200 with empty list when no documents."""
        _override_user_id(app)

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=([], None),
        ):
            client = TestClient(app)
            resp = client.get(f"/assistants/{ASSISTANT_ID}/documents")

        assert resp.status_code == 200
        body = resp.json()
        assert body["documents"] == []


# ---------------------------------------------------------------------------
# Requirement 14.2: Documents endpoint returns 401 for unauthenticated
# ---------------------------------------------------------------------------


class TestListDocumentsUnauthenticated:
    """GET /assistants/{id}/documents returns 401 for unauthenticated request."""

    def test_returns_401_unauthenticated(self, app):
        """Req 14.2: Unauthenticated request gets 401."""
        mock_no_auth(app)
        client = TestClient(app)
        resp = client.get(f"/assistants/{ASSISTANT_ID}/documents")

        assert resp.status_code == 401
