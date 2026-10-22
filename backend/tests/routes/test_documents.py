"""Tests for documents routes.

Endpoints under test:
- GET  /assistants/{assistant_id}/documents  → 200 with document list (authenticated)
- GET  /assistants/{assistant_id}/documents  → 401 for unauthenticated request

Requirements: 14.1, 14.2
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
# Requirement 12.11 visibility: kbUsage folded into the documents-list response
# ---------------------------------------------------------------------------

RECORDS_MODULE = "apis.shared.kb_backend.records"
BYTE_CAP_MODULE = "apis.shared.kb_backend.byte_cap"


class TestListDocumentsKbUsage:
    """GET /assistants/{id}/documents returns kbUsage for the storage bar."""

    def _list(self, app, kb_record=None, get_record_side_effect=None, cap=100 * 1024 * 1024):
        _override_user_id(app)
        # get_kb_record is a SYNC boto3 call run via asyncio.to_thread, so it is
        # mocked with a plain MagicMock (an AsyncMock would leave an un-awaited
        # coroutine for to_thread to return).
        get_record = MagicMock(return_value=kb_record)
        if get_record_side_effect is not None:
            get_record = MagicMock(side_effect=get_record_side_effect)

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=([_make_document()], None),
        ), patch(
            f"{RECORDS_MODULE}.get_kb_record", get_record
        ), patch(
            f"{BYTE_CAP_MODULE}.effective_cap", return_value=cap
        ):
            client = TestClient(app)
            resp = client.get(f"/assistants/{ASSISTANT_ID}/documents")
        return resp

    def test_managed_kb_reports_usage_and_cap(self, app):
        """A managed KB returns engine=managed, its bytes, and the binding cap."""
        from decimal import Decimal

        record = {
            "retrievalEngine": "managed",
            "storedBytes": Decimal(30 * 1024 * 1024),
            "reservedBytes": Decimal(5 * 1024 * 1024),
            "elevatedByteCap": False,
        }
        resp = self._list(app, kb_record=record, cap=100 * 1024 * 1024)

        assert resp.status_code == 200
        usage = resp.json()["kbUsage"]
        assert usage["engine"] == "managed"
        assert usage["storedBytes"] == 30 * 1024 * 1024
        assert usage["reservedBytes"] == 5 * 1024 * 1024
        assert usage["cap"] == 100 * 1024 * 1024
        assert usage["elevated"] is False

    def test_elevated_flag_is_read_from_record(self, app):
        """elevatedByteCap on the record surfaces as elevated=true."""
        record = {"retrievalEngine": "managed", "elevatedByteCap": True}
        resp = self._list(app, kb_record=record, cap=1024 * 1024 * 1024)

        usage = resp.json()["kbUsage"]
        assert usage["elevated"] is True
        assert usage["cap"] == 1024 * 1024 * 1024

    def test_legacy_kb_is_uncapped(self, app):
        """A legacy KB (no record) returns cap=null and zeroed counters."""
        resp = self._list(app, kb_record=None)

        assert resp.status_code == 200
        usage = resp.json()["kbUsage"]
        assert usage["engine"] == "s3vectors"
        assert usage["cap"] is None
        assert usage["storedBytes"] == 0
        assert usage["reservedBytes"] == 0

    def test_usage_read_failure_does_not_break_the_list(self, app):
        """A KB-record read error yields kbUsage=null but still returns the docs."""
        resp = self._list(app, get_record_side_effect=RuntimeError("dynamo down"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["kbUsage"] is None
        assert len(body["documents"]) == 1


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
