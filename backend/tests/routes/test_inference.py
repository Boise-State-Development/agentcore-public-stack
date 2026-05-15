"""Tests for Inference API endpoints.

Endpoints under test:
- GET  /ping         → 200 (health check)
- POST /invocations  → streaming response with valid payload
- POST /invocations  → 422 with invalid payload

Requirements: 15.1, 15.2, 15.3
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api.chat.routes import router
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the inference agentcore router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def trusted_user(make_user):
    """A mock user for the trusted auth dependency."""
    return make_user(raw_token="fake-jwt-token")


@pytest.fixture
def authed_app(app, trusted_user):
    """App with get_current_user_trusted overridden to return a mock user."""
    app.dependency_overrides[get_current_user_trusted] = lambda: trusted_user
    return app


@pytest.fixture
def authed_client(authed_app):
    return TestClient(authed_app)


# ---------------------------------------------------------------------------
# Requirement 15.1: GET /ping returns 200
# ---------------------------------------------------------------------------


class TestPing:
    """GET /ping returns 200 with health status."""

    def test_ping_returns_200(self, app):
        """Req 15.1: /ping should return 200."""
        client = TestClient(app)
        resp = client.get("/ping")
        assert resp.status_code == 200

    def test_ping_response_contains_status(self, app):
        """Req 15.1: /ping response should contain status field."""
        client = TestClient(app)
        body = client.get("/ping").json()
        assert "status" in body
        assert body["status"] == "healthy"


# ---------------------------------------------------------------------------
# Requirement 15.2: POST /invocations with valid payload returns streaming
# ---------------------------------------------------------------------------


class TestInvocationsValid:
    """POST /invocations with valid payload returns streaming response."""

    def test_returns_streaming_response(self, authed_app, authed_client):
        """Req 15.2: Valid invocation should return text/event-stream."""
        mock_agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            yield 'event: content_block_start\ndata: {"contentBlockIndex": 0, "type": "text"}\n\n'
            yield 'event: content_block_delta\ndata: {"contentBlockIndex": 0, "type": "text", "text": "Hello"}\n\n'
            yield 'event: content_block_stop\ndata: {"contentBlockIndex": 0}\n\n'
            yield 'event: message_stop\ndata: {"stopReason": "end_turn"}\n\n'
            yield "event: done\ndata: {}\n\n"

        mock_agent.stream_async = fake_stream

        with patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ):
            resp = authed_client.post(
                "/invocations",
                json={
                    "session_id": "sess-001",
                    "message": "Hello, how are you?",
                },
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_streaming_body_contains_events(self, authed_app, authed_client):
        """Req 15.2: Streaming body should contain SSE events."""
        mock_agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            yield "event: done\ndata: {}\n\n"

        mock_agent.stream_async = fake_stream

        with patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ):
            resp = authed_client.post(
                "/invocations",
                json={
                    "session_id": "sess-002",
                    "message": "Test message",
                },
            )

        assert resp.status_code == 200
        body = resp.text
        assert "event: message_start" in body or "event: done" in body


# ---------------------------------------------------------------------------
# Requirement 15.3: POST /invocations with invalid payload returns 422
# ---------------------------------------------------------------------------


class TestInvocationsInvalid:
    """POST /invocations with invalid payload returns 422."""

    def test_missing_required_fields_returns_422(self, authed_app, authed_client):
        """Req 15.3: Missing session_id should return 422."""
        resp = authed_client.post("/invocations", json={})
        assert resp.status_code == 422

    def test_missing_session_id_returns_422(self, authed_app, authed_client):
        """Req 15.3: Missing session_id field should return 422."""
        resp = authed_client.post(
            "/invocations",
            json={"message": "Hello"},
        )
        assert resp.status_code == 422
