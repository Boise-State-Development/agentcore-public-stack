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
            "apis.app_api.sessions.routes.session_exists_for_other_user",
            new_callable=AsyncMock,
            return_value=False,
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

    def test_rejects_disabled_prompt(self, app, make_user, authenticated_client):
        """A disabled prompt cannot be selected — 400."""
        user = make_user()
        client = authenticated_client(app, user)

        # Service returns None (None for missing OR disabled).
        mock_service = MagicMock()
        mock_service.get_enabled_prompt = AsyncMock(return_value=None)

        with patch(
            "apis.app_api.sessions.routes.get_system_prompts_service",
            return_value=mock_service,
        ):
            resp = client.put(
                "/sessions/sess-001/metadata",
                json={"selectedPromptId": "disabled-prompt"},
            )

        assert resp.status_code == 400
        assert "not found or not enabled" in resp.json()["detail"]

    def test_null_selected_prompt_clears_selection(self, app, make_user, authenticated_client):
        """Sending selectedPromptId: null clears the persisted selection."""
        from apis.shared.sessions.models import SessionPreferences
        user = make_user()
        client = authenticated_client(app, user)

        existing = _make_session_metadata("sess-001", user.user_id)
        existing.preferences = SessionPreferences(selected_prompt_id="some-old-id")

        captured = {}

        async def capture_store(*, session_id, user_id, session_metadata):
            captured["metadata"] = session_metadata

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            side_effect=capture_store,
        ):
            resp = client.put(
                "/sessions/sess-001/metadata",
                json={"selectedPromptId": None},
            )

        assert resp.status_code == 200
        # The persisted preferences should have selected_prompt_id cleared.
        assert captured["metadata"].preferences.selected_prompt_id is None

    def test_omitted_selected_prompt_leaves_selection_unchanged(self, app, make_user, authenticated_client):
        """Omitting selectedPromptId entirely must not clear the existing value."""
        from apis.shared.sessions.models import SessionPreferences
        user = make_user()
        client = authenticated_client(app, user)

        existing = _make_session_metadata("sess-001", user.user_id)
        existing.preferences = SessionPreferences(selected_prompt_id="keep-me")

        captured = {}

        async def capture_store(*, session_id, user_id, session_metadata):
            captured["metadata"] = session_metadata

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            side_effect=capture_store,
        ):
            # Update title only — no selectedPromptId field at all.
            resp = client.put(
                "/sessions/sess-001/metadata",
                json={"title": "New title"},
            )

        assert resp.status_code == 200
        assert captured["metadata"].preferences.selected_prompt_id == "keep-me"

    def test_agent_type_persists_and_merges(self, app, make_user, authenticated_client):
        """agentType (skills-mode) lands in preferences without clobbering others."""
        from apis.shared.sessions.models import SessionPreferences
        user = make_user()
        client = authenticated_client(app, user)

        existing = _make_session_metadata("sess-001", user.user_id)
        existing.preferences = SessionPreferences(selected_prompt_id="keep-me")

        captured = {}

        async def capture_store(*, session_id, user_id, session_metadata):
            captured["metadata"] = session_metadata

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            side_effect=capture_store,
        ):
            resp = client.put(
                "/sessions/sess-001/metadata",
                json={"agentType": "chat"},
            )

        assert resp.status_code == 200
        prefs = captured["metadata"].preferences
        assert prefs.agent_type == "chat"
        assert prefs.selected_prompt_id == "keep-me"

    def test_agent_type_set_on_brand_new_session(self, app, make_user, authenticated_client):
        """agentType alone is enough to create the preferences object."""
        user = make_user()
        client = authenticated_client(app, user)

        captured = {}

        async def capture_store(*, session_id, user_id, session_metadata):
            captured["metadata"] = session_metadata

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "apis.app_api.sessions.routes.session_exists_for_other_user",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            side_effect=capture_store,
        ):
            resp = client.put(
                "/sessions/sess-new/metadata",
                json={"agentType": "skill"},
            )

        assert resp.status_code == 200
        assert captured["metadata"].preferences.agent_type == "skill"

    def test_agent_type_rejects_unknown_mode(self, app, make_user, authenticated_client):
        """agentType is constrained to the skill/chat mode pair."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.put(
            "/sessions/sess-001/metadata",
            json={"agentType": "voice"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Session-metadata ownership: PUT cannot land on another user's session
# ---------------------------------------------------------------------------


class TestUpdateSessionMetadataOwnership:
    """PUT /sessions/{session_id}/metadata refuses to write when the session
    already exists for a different user. Behaves like GET in that case —
    returns 404 — so non-owners cannot enumerate session ids by probing."""

    def test_returns_404_when_session_exists_for_another_user(
        self, app, make_user, authenticated_client
    ):
        """The session id is taken; the caller is not its owner. 404."""
        user = make_user(user_id="user-attacker")
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "apis.app_api.sessions.routes.session_exists_for_other_user",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            new_callable=AsyncMock,
        ) as mock_store:
            resp = client.put(
                "/sessions/victim-session-id/metadata",
                json={"title": "IDOR-ATTEMPT"},
            )

        assert resp.status_code == 404
        # And no write must have happened.
        mock_store.assert_not_called()

    def test_persists_no_data_when_existence_check_fails(
        self, app, make_user, authenticated_client
    ):
        """If the existence check returns True the write must not run, even
        if the per-user fetch returned None (the create-new path)."""
        user = make_user(user_id="user-attacker")
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "apis.app_api.sessions.routes.session_exists_for_other_user",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            new_callable=AsyncMock,
        ) as mock_store:
            client.put(
                "/sessions/victim-session-id/metadata",
                json={"title": "anything", "starred": True},
            )

        mock_store.assert_not_called()

    def test_brand_new_session_id_creates_normally(
        self, app, make_user, authenticated_client
    ):
        """When the session id is genuinely free, the create-new branch
        still runs."""
        user = make_user(user_id="u-1")
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "apis.app_api.sessions.routes.session_exists_for_other_user",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            new_callable=AsyncMock,
        ) as mock_store:
            resp = client.put(
                "/sessions/fresh-session-id/metadata",
                json={"title": "First conversation"},
            )

        assert resp.status_code == 200
        mock_store.assert_awaited_once()

    def test_owner_update_does_not_invoke_existence_check(
        self, app, make_user, authenticated_client
    ):
        """When the user already owns the session, the existence-check
        helper is unnecessary — the per-user fetch returned a record."""
        user = make_user(user_id="u-1")
        client = authenticated_client(app, user)

        existing = _make_session_metadata("owned-session", user.user_id)

        with patch(
            "apis.app_api.sessions.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "apis.app_api.sessions.routes.session_exists_for_other_user",
            new_callable=AsyncMock,
            return_value=False,
        ) as exists_check, patch(
            "apis.app_api.sessions.routes.store_session_metadata",
            new_callable=AsyncMock,
        ):
            resp = client.put(
                "/sessions/owned-session/metadata",
                json={"title": "Renamed"},
            )

        assert resp.status_code == 200
        exists_check.assert_not_called()


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
