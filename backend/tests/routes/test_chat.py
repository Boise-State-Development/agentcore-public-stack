"""Tests for chat routes.

Endpoints under test:
- POST /chat/generate-title  → 200 with generated title
- POST /chat/generate-title  → 401 for unauthenticated request
- POST /chat/agent-stream           → streaming response with text/event-stream
- POST /chat/multimodal       → streaming response

Requirements: 5.1, 5.2, 5.3, 5.4
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.chat.routes import router
from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the chat router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 5.1: POST /chat/generate-title returns 200 with generated title
# ---------------------------------------------------------------------------


class TestGenerateTitle:
    """POST /chat/generate-title returns a generated title."""

    def test_returns_200_with_generated_title(self, app, make_user, authenticated_client):
        """Req 5.1: Should return 200 with a generated title."""
        user = make_user()
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.chat.routes.generate_conversation_title",
            new_callable=AsyncMock,
            return_value="My Generated Title",
        ):
            resp = client.post(
                "/chat/generate-title",
                json={"session_id": "sess-001", "input": "Tell me about AWS"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "My Generated Title"
        assert body["session_id"] == "sess-001"

    def test_returns_fallback_title_on_error(self, app, make_user, authenticated_client):
        """Req 5.1: Should return fallback title when generation fails."""
        user = make_user()
        client = authenticated_client(app, user)

        with patch(
            "apis.app_api.chat.routes.generate_conversation_title",
            new_callable=AsyncMock,
            side_effect=Exception("Bedrock error"),
        ):
            resp = client.post(
                "/chat/generate-title",
                json={"session_id": "sess-001", "input": "Hello"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "New Conversation"
        assert body["session_id"] == "sess-001"

    # -------------------------------------------------------------------
    # Requirement 5.2: POST /chat/generate-title returns 401 unauthenticated
    # -------------------------------------------------------------------

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Req 5.2: Should return 401 when no auth is provided."""
        client = unauthenticated_client(app)
        resp = client.post(
            "/chat/generate-title",
            json={"session_id": "sess-001", "input": "Hello"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Requirement 5.3: POST /chat/agent-stream returns streaming response
# ---------------------------------------------------------------------------


class TestChatStream:
    """POST /chat/agent-stream returns a streaming response."""

    def test_returns_streaming_response(self, app, make_user, authenticated_client):
        """Req 5.3: Should return streaming response with text/event-stream."""
        user = make_user()
        client = authenticated_client(app, user)

        # Mock the agent returned by get_agent
        mock_agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            yield "event: done\ndata: {}\n\n"

        mock_agent.stream_async = fake_stream
        mock_agent.session_manager = MagicMock()
        mock_agent.session_manager.flush = MagicMock()

        with patch(
            "apis.app_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.app_api.chat.routes.get_tool_access_service",
        ) as mock_tool_svc, patch(
            "apis.app_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.app_api.chat.routes.get_session_metadata",
            new_callable=AsyncMock,
            return_value=None,
        ):
            mock_tool_access = AsyncMock()
            mock_tool_access.check_access_and_filter = AsyncMock(
                return_value=(["tool1"], [])
            )
            mock_tool_svc.return_value = mock_tool_access

            resp = client.post(
                "/chat/agent-stream",
                json={
                    "session_id": "sess-001",
                    "message": "Hello, how are you?",
                },
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Req 5.3: Should return 401 when no auth is provided."""
        client = unauthenticated_client(app)
        resp = client.post(
            "/chat/agent-stream",
            json={"session_id": "sess-001", "message": "Hello"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Requirement 5.4: POST /chat/multimodal returns streaming response
# ---------------------------------------------------------------------------


class TestChatMultimodal:
    """POST /chat/multimodal returns a streaming response."""

    def test_returns_streaming_response(self, app, make_user, authenticated_client):
        """Req 5.4: Should return streaming response for multimodal input."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.post(
            "/chat/multimodal",
            json={
                "session_id": "sess-001",
                "message": "Describe this image",
                "files": [
                    {
                        "filename": "test.png",
                        "content_type": "image/png",
                        "bytes": "aGVsbG8=",
                    }
                ],
            },
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_returns_streaming_response_without_files(self, app, make_user, authenticated_client):
        """Req 5.4: Should return streaming response even without files."""
        user = make_user()
        client = authenticated_client(app, user)

        resp = client.post(
            "/chat/multimodal",
            json={
                "session_id": "sess-001",
                "message": "Just a text message",
            },
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Req 5.4: Should return 401 when no auth is provided."""
        client = unauthenticated_client(app)
        resp = client.post(
            "/chat/multimodal",
            json={"session_id": "sess-001", "message": "Hello"},
        )
        assert resp.status_code == 401
