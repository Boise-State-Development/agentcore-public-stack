"""Tests for session management routes.

Endpoints under test:
- GET    /sessions                        → 200 with paginated session list
- GET    /sessions                        → 401 for unauthenticated request
- GET    /sessions?limit=N                → at most N sessions
- GET    /sessions/{session_id}/metadata  → 200 with session metadata
- PUT    /sessions/{session_id}/metadata  → 200 with updated metadata
- DELETE /sessions/{session_id}           → 204
- POST   /sessions/bulk-delete            → 200 with deletion results
- GET    /sessions/{session_id}/messages  → 200 with message history

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.sessions.routes import router
from apis.shared.sessions.models import (
    SessionMetadata,
    MessagesListResponse,
    MessageResponse,
    MessageContent,
)

from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_metadata(session_id: str = "sess-001", user_id: str = "user-001") -> SessionMetadata:
    """Create a minimal SessionMetadata for mocking."""
    return SessionMetadata(
        session_id=session_id,
        user_id=user_id,
        title="Test Session",
        status="active",
        created_at="2025-01-01T00:00:00Z",
        last_message_at="2025-01-01T01:00:00Z",
        message_count=5,
        starred=False,
        tags=[],
    )


def _make_message_response(msg_id: str = "msg-001") -> MessageResponse:
    """Create a minimal MessageResponse for mocking."""
    return MessageResponse(
        id=msg_id,
        role="assistant",
        content=[MessageContent(type="text", text="Hello")],
        created_at="2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the sessions router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 3.1: GET /sessions returns 200 with paginated session list
# ---------------------------------------------------------------------------

class TestListSessions:
    """GET /sessions returns paginated session list for authenticated user."""

    def test_returns_200_with_session_list(self, app, make_user, authenticated_client):
        """Req 3.1: Should return 200 with a list of sessions."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_sessions = [_make_session_metadata("sess-001"), _make_session_metadata("sess-002")]

        with patch(
            "apis.app_api.sessions.routes.list_user_sessions",
            new_callable=AsyncMock,
            return_value=(mock_sessions, None),
        ):
            resp = client.get("/sessions")

        assert resp.status_code == 200
        body = resp.json()
        assert "sessions" in body
        assert len(body["sessions"]) == 2
        assert body["sessions"][0]["sessionId"] == "sess-001"

    # -------------------------------------------------------------------
    # Requirement 3.2: GET /sessions returns 401 for unauthenticated
    # -------------------------------------------------------------------

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Req 3.2: Should return 401 when no auth is provided."""
        client = unauthenticated_client(app)
        resp = client.get("/sessions")
        assert resp.status_code == 401

    # -------------------------------------------------------------------
    # Requirement 3.3: GET /sessions with limit returns at most N sessions
    # -------------------------------------------------------------------

    def test_returns_at_most_n_sessions_with_limit(self, app, make_user, authenticated_client):
        """Req 3.3: Should return at most N sessions when limit=N."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_sessions = [_make_session_metadata(f"sess-{i:03d}") for i in range(3)]

        with patch(
            "apis.app_api.sessions.routes.list_user_sessions",
            new_callable=AsyncMock,
            return_value=(mock_sessions, None),
        ):
            resp = client.get("/sessions?limit=3")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["sessions"]) <= 3

    def test_returns_pagination_token(self, app, make_user, authenticated_client):
        """Req 3.1: Should include next_token when more results exist."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_sessions = [_make_session_metadata("sess-001")]

        with patch(
            "apis.app_api.sessions.routes.list_user_sessions",
            new_callable=AsyncMock,
            return_value=(mock_sessions, "next-page-token"),
        ):
            resp = client.get("/sessions?limit=1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["nextToken"] == "next-page-token"


# ---------------------------------------------------------------------------
# Requirement 3.4: GET /sessions/{session_id}/metadata returns 200
# ---------------------------------------------------------------------------

class TestGetSessionMetadata:
    """GET /sessions/{session_id}/metadata returns session metadata."""

    def test_returns_200_with_metadata(self, app, make_user, authenticated_client):
        """Req 3.4: Should return 200 with session metadata."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_meta = _make_session_metadata("sess-001", user.user_id)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=mock_meta,
        ):
            resp = client.get("/sessions/sess-001/metadata")

        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "sess-001"
        assert body["title"] == "Test Session"

    def test_returns_404_when_not_found(self, app, make_user, authenticated_client):
        """Req 3.4: Should return 404 when session does not exist."""
        user = make_user()
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.get("/sessions/nonexistent/metadata")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Requirement 3.5: PUT /sessions/{session_id}/metadata returns 200
# ---------------------------------------------------------------------------

class TestUpdateSessionMetadata:
    """PUT /sessions/{session_id}/metadata updates and returns metadata."""

    def test_returns_200_with_updated_metadata(self, app, make_user, authenticated_client):
        """Req 3.5: Should return 200 with updated session metadata."""
        user = make_user()
        client = authenticated_client(app, user)

        existing = _make_session_metadata("sess-001", user.user_id)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            new_callable=AsyncMock,
        ):
            resp = client.put(
                "/sessions/sess-001/metadata",
                json={"title": "Updated Title"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Updated Title"
        assert body["sessionId"] == "sess-001"

    def test_creates_new_metadata_when_not_found(self, app, make_user, authenticated_client):
        """Req 3.5: Should create new metadata when session doesn't exist yet."""
        user = make_user()
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            new_callable=AsyncMock,
        ):
            resp = client.put(
                "/sessions/sess-new/metadata",
                json={"title": "Brand New Session"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Brand New Session"


# ---------------------------------------------------------------------------
# Requirement 3.6: DELETE /sessions/{session_id} returns 204
# ---------------------------------------------------------------------------

class TestDeleteSession:
    """DELETE /sessions/{session_id} deletes a session."""

    def test_returns_204_on_success(self, app, make_user, authenticated_client):
        """Req 3.6: Should return 204 when session is deleted."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_service = AsyncMock()
        mock_service.delete_session = AsyncMock(return_value=True)
        mock_service.delete_agentcore_memory = AsyncMock()
        mock_service.delete_session_files = AsyncMock()

        mock_share_service = AsyncMock()
        mock_share_service.delete_shares_for_session = AsyncMock(return_value=0)

        with patch(
            "apis.app_api.sessions.routes.SessionService",
            return_value=mock_service,
        ), patch(
            "apis.app_api.sessions.routes.get_share_service",
            return_value=mock_share_service,
        ):
            resp = client.delete("/sessions/sess-001")

        assert resp.status_code == 204

    def test_returns_404_when_not_found(self, app, make_user, authenticated_client):
        """Req 3.6: Should return 404 when session does not exist."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_service = AsyncMock()
        mock_service.delete_session = AsyncMock(return_value=False)

        with patch(
            "apis.app_api.sessions.routes.SessionService",
            return_value=mock_service,
        ):
            resp = client.delete("/sessions/nonexistent")

        assert resp.status_code == 404

    def test_queues_share_cleanup_on_delete(self, app, make_user, authenticated_client):
        """Deleting a session should queue share snapshot cleanup as a background task."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_service = AsyncMock()
        mock_service.delete_session = AsyncMock(return_value=True)
        mock_service.delete_agentcore_memory = AsyncMock()
        mock_service.delete_session_files = AsyncMock()

        mock_share_service = AsyncMock()
        mock_share_service.delete_shares_for_session = AsyncMock(return_value=2)

        with patch(
            "apis.app_api.sessions.routes.SessionService",
            return_value=mock_service,
        ), patch(
            "apis.app_api.sessions.routes.get_share_service",
            return_value=mock_share_service,
        ):
            resp = client.delete("/sessions/sess-001")

        assert resp.status_code == 204
        # Background task should have been called with the session id
        mock_share_service.delete_shares_for_session.assert_called_once_with("sess-001")


# ---------------------------------------------------------------------------
# Requirement 3.7: POST /sessions/bulk-delete returns 200
# ---------------------------------------------------------------------------

class TestBulkDeleteSessions:
    """POST /sessions/bulk-delete deletes multiple sessions."""

    def test_returns_200_with_results(self, app, make_user, authenticated_client):
        """Req 3.7: Should return 200 with deletion results."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_service = AsyncMock()
        mock_service.delete_session = AsyncMock(return_value=True)
        mock_service.delete_agentcore_memory = AsyncMock()
        mock_service.delete_session_files = AsyncMock()

        mock_share_service = AsyncMock()
        mock_share_service.delete_shares_for_session = AsyncMock(return_value=0)

        with patch(
            "apis.app_api.sessions.routes.SessionService",
            return_value=mock_service,
        ), patch(
            "apis.app_api.sessions.routes.get_share_service",
            return_value=mock_share_service,
        ):
            resp = client.post(
                "/sessions/bulk-delete",
                json={"sessionIds": ["sess-001", "sess-002"]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deletedCount"] == 2
        assert body["failedCount"] == 0
        assert len(body["results"]) == 2
        assert all(r["success"] for r in body["results"])

    def test_partial_failure(self, app, make_user, authenticated_client):
        """Req 3.7: Should report partial failures in results."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_service = AsyncMock()
        # First succeeds, second fails (not found)
        mock_service.delete_session = AsyncMock(side_effect=[True, False])
        mock_service.delete_agentcore_memory = AsyncMock()
        mock_service.delete_session_files = AsyncMock()

        mock_share_service = AsyncMock()
        mock_share_service.delete_shares_for_session = AsyncMock(return_value=0)

        with patch(
            "apis.app_api.sessions.routes.SessionService",
            return_value=mock_service,
        ), patch(
            "apis.app_api.sessions.routes.get_share_service",
            return_value=mock_share_service,
        ):
            resp = client.post(
                "/sessions/bulk-delete",
                json={"sessionIds": ["sess-001", "sess-missing"]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deletedCount"] == 1
        assert body["failedCount"] == 1

    def test_bulk_delete_queues_share_cleanup(self, app, make_user, authenticated_client):
        """Bulk delete should queue share cleanup for each successfully deleted session."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_service = AsyncMock()
        mock_service.delete_session = AsyncMock(side_effect=[True, True])
        mock_service.delete_agentcore_memory = AsyncMock()
        mock_service.delete_session_files = AsyncMock()

        mock_share_service = AsyncMock()
        mock_share_service.delete_shares_for_session = AsyncMock(return_value=1)

        with patch(
            "apis.app_api.sessions.routes.SessionService",
            return_value=mock_service,
        ), patch(
            "apis.app_api.sessions.routes.get_share_service",
            return_value=mock_share_service,
        ):
            resp = client.post(
                "/sessions/bulk-delete",
                json={"sessionIds": ["sess-001", "sess-002"]},
            )

        assert resp.status_code == 200
        assert mock_share_service.delete_shares_for_session.call_count == 2

    def test_rejects_empty_list(self, app, make_user, authenticated_client):
        """Req 3.7: Should return 422 for empty session_ids list."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.post(
            "/sessions/bulk-delete",
            json={"sessionIds": []},
        )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Requirement 3.8: GET /sessions/{session_id}/messages returns 200
# ---------------------------------------------------------------------------

class TestGetSessionMessages:
    """GET /sessions/{session_id}/messages returns message history."""

    def test_returns_200_with_messages(self, app, make_user, authenticated_client):
        """Req 3.8: Should return 200 with message history."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_response = MessagesListResponse(
            messages=[_make_message_response("msg-001"), _make_message_response("msg-002")],
            next_token=None,
        )

        with patch(
            "apis.app_api.sessions.routes.get_messages",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            resp = client.get("/sessions/sess-001/messages")

        assert resp.status_code == 200
        body = resp.json()
        assert "messages" in body
        assert len(body["messages"]) == 2

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Req 3.8: Should return 401 when no auth is provided."""
        client = unauthenticated_client(app)
        resp = client.get("/sessions/sess-001/messages")
        assert resp.status_code == 401

    def test_returns_404_when_session_not_found(self, app, make_user, authenticated_client):
        """Req 3.8: Should return 404 when session has no messages."""
        user = make_user()
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.sessions.routes.get_messages",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("Session not found"),
        ):
            resp = client.get("/sessions/nonexistent/messages")

        assert resp.status_code == 404
