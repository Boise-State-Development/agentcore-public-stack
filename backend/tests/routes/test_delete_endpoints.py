"""Tests for refactored delete endpoints (soft-delete + background cleanup).

Endpoints under test:
- DELETE /assistants/{assistant_id}/documents/{document_id} → 204 after soft-delete
- DELETE /assistants/{assistant_id} → 204 after soft-deleting docs + hard-deleting assistant

Requirements: 2.1, 2.2, 8.1, 8.2
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.documents.routes import router as documents_router
from apis.app_api.assistants.routes import router as assistants_router
from apis.app_api.documents.models import Document
from apis.shared.auth.dependencies import get_current_user_id, get_current_user
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSISTANT_ID = "ast-001"
USER_ID = "user-001"
DOC_SERVICE = "apis.app_api.documents.services.document_service"
CLEANUP_SERVICE = "apis.app_api.documents.services.cleanup_service"
ASSISTANT_SERVICE = "apis.shared.assistants.service"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_document(**overrides) -> Document:
    defaults = dict(
        documentId="doc-001",
        assistantId=ASSISTANT_ID,
        filename="report.pdf",
        contentType="application/pdf",
        sizeBytes=1024,
        s3Key=f"assistants/{ASSISTANT_ID}/documents/doc-001/report.pdf",
        status="deleting",
        chunkCount=5,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        ttl=1737504600,
    )
    defaults.update(overrides)
    return Document.model_validate(defaults)


def _make_user() -> User:
    return User(
        email="test@example.com",
        user_id=USER_ID,
        name="Test User",
        roles=["User"],
    )


# ---------------------------------------------------------------------------
# TestDocumentDeleteEndpoint
# ---------------------------------------------------------------------------


class TestDocumentDeleteEndpoint:
    """DELETE /assistants/{id}/documents/{doc_id} — soft-delete + background cleanup."""

    @pytest.fixture
    def app(self):
        _app = FastAPI()
        _app.include_router(documents_router)
        _app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        return _app

    def test_delete_returns_204_after_soft_delete(self, app):
        """Req 2.1: Endpoint returns 204 after successful soft-delete."""
        doc = _make_document()

        with patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=doc,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_document_resources",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 204

    def test_delete_returns_404_when_not_found(self, app):
        """Req 1.5: Returns 404 when soft_delete_document returns None."""
        with patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 404

    def test_delete_fires_cleanup_in_background(self, app):
        """Req 2.2: Cleanup is scheduled as a background task via asyncio.ensure_future."""
        doc = _make_document()

        with patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=doc,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_document_resources",
            new_callable=AsyncMock,
        ) as mock_cleanup, patch(
            "asyncio.ensure_future",
        ) as mock_ensure:
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 204
        mock_ensure.assert_called_once()


# ---------------------------------------------------------------------------
# TestAssistantDeleteEndpoint
# ---------------------------------------------------------------------------


class TestAssistantDeleteEndpoint:
    """DELETE /assistants/{id} — soft-delete docs, hard-delete assistant, background cleanup."""

    ROUTES_MODULE = "apis.app_api.assistants.routes"

    @pytest.fixture
    def app(self):
        _app = FastAPI()
        _app.include_router(assistants_router)
        _app.dependency_overrides[get_current_user] = _make_user
        return _app

    def test_delete_soft_deletes_all_docs(self, app):
        """Req 8.1: All documents are batch soft-deleted before assistant is removed."""
        docs = [
            _make_document(documentId="doc-001"),
            _make_document(documentId="doc-002"),
        ]

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(docs, None),
        ), patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents",
            new_callable=AsyncMock,
            return_value=2,
        ) as mock_batch, patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_assistant_documents",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        mock_batch.assert_called_once_with(
            assistant_id=ASSISTANT_ID,
            document_ids=["doc-001", "doc-002"],
        )

    def test_delete_hard_deletes_assistant(self, app):
        """Req 8.1: Assistant record is hard-deleted after soft-deleting docs."""
        docs = [_make_document(documentId="doc-001")]

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(docs, None),
        ), patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents",
            new_callable=AsyncMock,
            return_value=1,
        ), patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_delete_ast, patch(
            f"{CLEANUP_SERVICE}.cleanup_assistant_documents",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        mock_delete_ast.assert_called_once_with(
            assistant_id=ASSISTANT_ID,
            owner_id=USER_ID,
        )

    def test_delete_fires_cleanup_in_background(self, app):
        """Req 8.2: Background cleanup is scheduled via asyncio.ensure_future."""
        docs = [_make_document(documentId="doc-001")]

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(docs, None),
        ), patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents",
            new_callable=AsyncMock,
            return_value=1,
        ), patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_assistant_documents",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ) as mock_ensure:
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        mock_ensure.assert_called_once()
