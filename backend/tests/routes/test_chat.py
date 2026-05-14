"""Tests for chat routes.

Endpoints under test:
- POST /chat/generate-title  → 200 with generated title
- POST /chat/generate-title  → 401 for unauthenticated request

Requirements: 5.1, 5.2
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from apis.app_api.chat.routes import router


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
