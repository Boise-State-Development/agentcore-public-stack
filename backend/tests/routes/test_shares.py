"""Tests for share conversation routes.

Endpoints under test:
- POST   /conversations/{session_id}/share   → 201 create share snapshot
- GET    /conversations/{session_id}/shares  → 200 list shares for session
- PATCH  /shares/{share_id}                  → 200 update share settings
- DELETE /shares/{share_id}                  → 204 revoke share
- POST   /shares/{share_id}/export           → 201 export to new conversation
- GET    /shared/{share_id}                  → 200 retrieve shared conversation
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from apis.app_api.shares.routes import conversations_share_router, shares_router, shared_view_router
from apis.app_api.shares.models import ShareResponse, ShareListResponse, SharedConversationResponse
from apis.app_api.shares.service import (
    AccessDeniedError,
    NotOwnerError,
    SessionNotFoundError,
    ShareNotFoundError,
)
from apis.shared.sessions.models import MessageResponse, MessageContent

from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_share_response(
    share_id: str = "share-001",
    session_id: str = "sess-001",
    owner_id: str = "user-001",
    access_level: str = "public",
    allowed_emails: list | None = None,
) -> ShareResponse:
    return ShareResponse(
        share_id=share_id,
        session_id=session_id,
        owner_id=owner_id,
        access_level=access_level,
        allowed_emails=allowed_emails,
        created_at="2025-06-01T00:00:00Z",
        share_url=f"/shared/{share_id}",
    )


def _make_message_response(msg_id: str = "msg-001") -> MessageResponse:
    return MessageResponse(
        id=msg_id,
        role="assistant",
        content=[MessageContent(type="text", text="Hello")],
        created_at="2025-06-01T00:00:00Z",
    )


def _make_shared_conversation_response(
    share_id: str = "share-001",
) -> SharedConversationResponse:
    return SharedConversationResponse(
        share_id=share_id,
        title="Test Conversation",
        access_level="public",
        created_at="2025-06-01T00:00:00Z",
        owner_id="user-001",
        messages=[_make_message_response()],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Minimal FastAPI app mounting share routers."""
    _app = FastAPI()
    _app.include_router(conversations_share_router)
    _app.include_router(shares_router)
    _app.include_router(shared_view_router)
    return _app


@pytest.fixture
def mock_share_service():
    """Patch get_share_service so routes use our mock."""
    mock = AsyncMock()
    with patch("apis.app_api.shares.routes.get_share_service", return_value=mock):
        yield mock


# ---------------------------------------------------------------------------
# Requirement 1: Share Conversation Snapshot Creation
# ---------------------------------------------------------------------------

class TestCreateShare:
    """POST /conversations/{session_id}/share"""

    def test_create_public_share_returns_201(self, app, make_user, authenticated_client, mock_share_service):
        """Req 1.1: Create public share returns 201 with share details."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = _make_share_response()
        mock_share_service.create_share = AsyncMock(return_value=expected)

        resp = client.post(
            "/conversations/sess-001/share",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["shareId"] == "share-001"
        assert body["accessLevel"] == "public"
        assert body["shareUrl"] == "/shared/share-001"

    def test_create_specific_share_returns_201(self, app, make_user, authenticated_client, mock_share_service):
        """Req 1.2: Create specific share with allowed emails returns 201."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = _make_share_response(
            access_level="specific",
            allowed_emails=["test@example.com", "other@example.com"],
        )
        mock_share_service.create_share = AsyncMock(return_value=expected)

        resp = client.post(
            "/conversations/sess-001/share",
            json={"accessLevel": "specific", "allowedEmails": ["other@example.com"]},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["accessLevel"] == "specific"
        assert "other@example.com" in body["allowedEmails"]

    def test_create_specific_share_without_emails_returns_422(self, app, make_user, authenticated_client):
        """Req 1.3: Specific access with empty allowed_emails returns 422."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.post(
            "/conversations/sess-001/share",
            json={"accessLevel": "specific", "allowedEmails": []},
        )

        assert resp.status_code == 422

    def test_create_private_share_returns_422(self, app, make_user, authenticated_client):
        """Private access level is no longer valid and returns 422."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.post(
            "/conversations/sess-001/share",
            json={"accessLevel": "private"},
        )

        assert resp.status_code == 422

    def test_create_share_non_owner_returns_403(self, app, make_user, authenticated_client, mock_share_service):
        """Req 1.5: Non-owner gets 403."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.create_share = AsyncMock(side_effect=NotOwnerError())

        resp = client.post(
            "/conversations/sess-001/share",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 403

    def test_create_share_session_not_found_returns_404(self, app, make_user, authenticated_client, mock_share_service):
        """Req 1.6: Non-existent session returns 404."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.create_share = AsyncMock(
            side_effect=SessionNotFoundError("sess-999")
        )

        resp = client.post(
            "/conversations/sess-999/share",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 404
        assert "sess-999" in resp.json()["detail"]

    def test_unauthenticated_create_returns_401(self, app, unauthenticated_client):
        """Unauthenticated request returns 401."""
        client = unauthenticated_client(app)

        resp = client.post(
            "/conversations/sess-001/share",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 401

    def test_create_multiple_shares_for_same_session(self, app, make_user, authenticated_client, mock_share_service):
        """Multiple shares can be created for the same session."""
        user = make_user()
        client = authenticated_client(app, user)

        share1 = _make_share_response(share_id="share-001")
        share2 = _make_share_response(share_id="share-002")
        mock_share_service.create_share = AsyncMock(side_effect=[share1, share2])

        resp1 = client.post("/conversations/sess-001/share", json={"accessLevel": "public"})
        resp2 = client.post("/conversations/sess-001/share", json={"accessLevel": "public"})

        assert resp1.status_code == 201
        assert resp2.status_code == 201
        assert resp1.json()["shareId"] != resp2.json()["shareId"]


# ---------------------------------------------------------------------------
# Requirement 2: Shared Conversation Retrieval
# ---------------------------------------------------------------------------

class TestGetSharedConversation:
    """GET /shared/{share_id}"""

    def test_get_public_share_returns_200(self, app, make_user, authenticated_client, mock_share_service):
        """Req 2.1: Public share returns snapshot data."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = _make_shared_conversation_response()
        mock_share_service.get_shared_conversation = AsyncMock(return_value=expected)

        resp = client.get("/shared/share-001")

        assert resp.status_code == 200
        body = resp.json()
        assert body["shareId"] == "share-001"
        assert body["title"] == "Test Conversation"
        assert len(body["messages"]) == 1

    def test_get_share_access_denied_returns_403(self, app, make_user, authenticated_client, mock_share_service):
        """Req 2.3/2.4: Access denied returns 403."""
        user = make_user(user_id="other-user")
        client = authenticated_client(app, user)

        mock_share_service.get_shared_conversation = AsyncMock(
            side_effect=AccessDeniedError()
        )

        resp = client.get("/shared/share-001")

        assert resp.status_code == 403
        assert resp.json()["detail"] == "Access denied"

    def test_get_share_not_found_returns_404(self, app, make_user, authenticated_client, mock_share_service):
        """Req 2.7: Non-existent share returns 404."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.get_shared_conversation = AsyncMock(
            side_effect=ShareNotFoundError()
        )

        resp = client.get("/shared/share-999")

        assert resp.status_code == 404

    def test_unauthenticated_get_returns_401(self, app, unauthenticated_client):
        """Req 2.6: Unauthenticated request returns 401."""
        client = unauthenticated_client(app)

        resp = client.get("/shared/share-001")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Requirement 3: Share Link Revocation (now by share_id)
# ---------------------------------------------------------------------------

class TestRevokeShare:
    """DELETE /shares/{share_id}"""

    def test_revoke_share_returns_204(self, app, make_user, authenticated_client, mock_share_service):
        """Req 3.1: Successful revocation returns 204."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.revoke_share = AsyncMock(return_value=None)

        resp = client.delete("/shares/share-001")

        assert resp.status_code == 204

    def test_revoke_share_non_owner_returns_403(self, app, make_user, authenticated_client, mock_share_service):
        """Req 3.2: Non-owner revocation returns 403."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.revoke_share = AsyncMock(side_effect=NotOwnerError())

        resp = client.delete("/shares/share-001")

        assert resp.status_code == 403

    def test_revoke_share_not_found_returns_404(self, app, make_user, authenticated_client, mock_share_service):
        """Req 3.3: Revoking non-existent share returns 404."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.revoke_share = AsyncMock(side_effect=ShareNotFoundError())

        resp = client.delete("/shares/share-999")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Requirement 4: Share Access Level and Allowed Emails Update (now by share_id)
# ---------------------------------------------------------------------------

class TestUpdateShare:
    """PATCH /shares/{share_id}"""

    def test_update_to_public_returns_200(self, app, make_user, authenticated_client, mock_share_service):
        """Req 4.1: Update to public clears allowed_emails."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = _make_share_response(access_level="public", allowed_emails=None)
        mock_share_service.update_share = AsyncMock(return_value=expected)

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["accessLevel"] == "public"
        assert body.get("allowedEmails") is None

    def test_update_to_specific_returns_200(self, app, make_user, authenticated_client, mock_share_service):
        """Req 4.2: Update to specific with emails returns 200."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = _make_share_response(
            access_level="specific",
            allowed_emails=["test@example.com", "new@example.com"],
        )
        mock_share_service.update_share = AsyncMock(return_value=expected)

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "specific", "allowedEmails": ["new@example.com"]},
        )

        assert resp.status_code == 200
        assert resp.json()["accessLevel"] == "specific"

    def test_update_to_specific_without_emails_returns_422(self, app, make_user, authenticated_client):
        """Req 4.3: Specific without emails returns 422."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "specific", "allowedEmails": []},
        )

        assert resp.status_code == 422

    def test_update_to_private_returns_422(self, app, make_user, authenticated_client):
        """Private access level is no longer valid and returns 422."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "private"},
        )

        assert resp.status_code == 422

    def test_update_non_owner_returns_403(self, app, make_user, authenticated_client, mock_share_service):
        """Req 4.6: Non-owner update returns 403."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.update_share = AsyncMock(side_effect=NotOwnerError())

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 403

    def test_update_no_active_share_returns_404(self, app, make_user, authenticated_client, mock_share_service):
        """Req 4.7: Update with no active share returns 404."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.update_share = AsyncMock(side_effect=ShareNotFoundError())

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "public"},
        )

        assert resp.status_code == 404

    def test_update_invalid_access_level_returns_422(self, app, make_user, authenticated_client):
        """Req 4.8: Invalid access_level returns 422."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.patch(
            "/shares/share-001",
            json={"accessLevel": "invalid_level"},
        )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List shares for session
# ---------------------------------------------------------------------------

class TestListSharesForSession:
    """GET /conversations/{session_id}/shares"""

    def test_list_shares_returns_200(self, app, make_user, authenticated_client, mock_share_service):
        """Returns list of shares for a session."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = ShareListResponse(shares=[
            _make_share_response(share_id="share-001"),
            _make_share_response(share_id="share-002"),
        ])
        mock_share_service.get_shares_for_session = AsyncMock(return_value=expected)

        resp = client.get("/conversations/sess-001/shares")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["shares"]) == 2
        assert body["shares"][0]["shareId"] == "share-001"
        assert body["shares"][1]["shareId"] == "share-002"

    def test_list_shares_empty_returns_200(self, app, make_user, authenticated_client, mock_share_service):
        """Returns empty list when no shares exist."""
        user = make_user()
        client = authenticated_client(app, user)

        expected = ShareListResponse(shares=[])
        mock_share_service.get_shares_for_session = AsyncMock(return_value=expected)

        resp = client.get("/conversations/sess-001/shares")

        assert resp.status_code == 200
        assert resp.json()["shares"] == []

    def test_list_shares_unauthenticated_returns_401(self, app, unauthenticated_client):
        """Unauthenticated request returns 401."""
        client = unauthenticated_client(app)

        resp = client.get("/conversations/sess-001/shares")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Export shared conversation
# ---------------------------------------------------------------------------

class TestExportSharedConversation:
    """POST /shares/{share_id}/export"""

    def test_export_returns_201(self, app, make_user, authenticated_client, mock_share_service):
        """Successful export returns 201 with new session details."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.export_shared_conversation = AsyncMock(
            return_value={"sessionId": "new-sess-001", "title": "Test Conversation (shared)"}
        )

        resp = client.post("/shares/share-001/export")

        assert resp.status_code == 201
        body = resp.json()
        assert body["sessionId"] == "new-sess-001"
        assert body["title"] == "Test Conversation (shared)"

    def test_export_access_denied_returns_403(self, app, make_user, authenticated_client, mock_share_service):
        """Export with no access returns 403."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.export_shared_conversation = AsyncMock(
            side_effect=AccessDeniedError()
        )

        resp = client.post("/shares/share-001/export")

        assert resp.status_code == 403

    def test_export_not_found_returns_404(self, app, make_user, authenticated_client, mock_share_service):
        """Export of non-existent share returns 404."""
        user = make_user()
        client = authenticated_client(app, user)

        mock_share_service.export_shared_conversation = AsyncMock(
            side_effect=ShareNotFoundError()
        )

        resp = client.post("/shares/share-999/export")

        assert resp.status_code == 404

    def test_export_unauthenticated_returns_401(self, app, unauthenticated_client):
        """Unauthenticated export returns 401."""
        client = unauthenticated_client(app)

        resp = client.post("/shares/share-001/export")

        assert resp.status_code == 401
