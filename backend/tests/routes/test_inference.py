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
        """Req 15.1: /ping returns the AgentCore health contract.

        Status must be a valid AgentCore PingStatus value, and the response
        must carry an integer ``time_of_last_update``; without that field the
        platform idle-reaps the microVM mid-stream
        (bedrock-agentcore-sdk-python#471).
        """
        client = TestClient(app)
        body = client.get("/ping").json()
        assert body["status"] in {"Healthy", "HealthyBusy"}
        assert isinstance(body["time_of_last_update"], int)


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
# session_title SSE: concurrent title generation pushed mid-stream
# ---------------------------------------------------------------------------


class TestSessionTitleEvent:
    """First-turn streams interleave a `session_title` SSE event once the
    concurrent title-generation task finishes, so the client can rename the
    conversation while the response is still pending."""

    @staticmethod
    def _make_agent():
        """Fake agent whose stream yields real suspension points so the
        concurrently scheduled title task gets a chance to run — mirrors the
        real Bedrock stream, which awaits network I/O between events."""
        import asyncio

        mock_agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            await asyncio.sleep(0)
            yield 'event: content_block_delta\ndata: {"contentBlockIndex": 0, "type": "text", "text": "Hi"}\n\n'
            await asyncio.sleep(0)
            yield "event: done\ndata: {}\n\n"

        mock_agent.stream_async = fake_stream
        return mock_agent

    def _post_first_turn(self, authed_client, title_result, is_new_session=True):
        async def fake_title(**kwargs):
            return title_result

        async def fake_ensure(*args, **kwargs):
            return is_new_session

        with patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=self._make_agent(),
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes.ensure_session_metadata_exists",
            side_effect=fake_ensure,
        ), patch(
            "apis.inference_api.chat.routes.generate_conversation_title",
            side_effect=fake_title,
        ):
            return authed_client.post(
                "/invocations",
                json={"session_id": "sess-title-1", "message": "Explain SSE"},
            )

    def test_first_turn_emits_session_title_event(self, authed_app, authed_client):
        """A new session's stream carries the generated title mid-stream."""
        resp = self._post_first_turn(authed_client, "SSE Streaming Explained")

        assert resp.status_code == 200
        body = resp.text
        assert "event: session_title" in body
        assert '"title": "SSE Streaming Explained"' in body
        assert '"sessionId": "sess-title-1"' in body
        # At most once per stream.
        assert body.count("event: session_title") == 1
        # Interleaved into the stream, not appended after the fact: it must
        # appear before the terminal `done` frame the agent emitted last.
        assert body.index("event: session_title") < body.rindex("event: done")

    def test_placeholder_title_is_not_emitted(self, authed_app, authed_client):
        """Generation failure returns the placeholder — nothing is pushed;
        the SPA's post-close fallback owns that path."""
        resp = self._post_first_turn(authed_client, "New Conversation")

        assert resp.status_code == 200
        assert "event: session_title" not in resp.text

    def test_existing_session_emits_no_title_event(self, authed_app, authed_client):
        """Non-first turns never kick off title generation, so no event."""
        resp = self._post_first_turn(
            authed_client, "Should Never Appear", is_new_session=False
        )

        assert resp.status_code == 200
        assert "event: session_title" not in resp.text


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


# ---------------------------------------------------------------------------
# PR-7: default agent_type flips to "skill"
# ---------------------------------------------------------------------------


class TestDefaultAgentTypeFlip:
    """When the client omits agent_type, the turn routes through SkillAgent."""

    @pytest.fixture(autouse=True)
    def _skills_enabled(self, monkeypatch):
        # The skill-default behavior only applies when the feature is on; off
        # by default, every turn is forced to chat (covered in
        # tests/routes/test_agent_mode_policy.py).
        monkeypatch.setenv("SKILLS_ENABLED", "true")

    def _mock_agent(self):
        agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield "event: done\ndata: {}\n\n"

        agent.stream_async = fake_stream
        return agent

    def test_omitted_agent_type_defaults_to_skill(self, authed_app, authed_client):
        get_agent_mock = MagicMock(return_value=self._mock_agent())
        resolve_mock = AsyncMock(return_value=["web_research"])
        with patch(
            "apis.inference_api.chat.routes.get_agent", get_agent_mock
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes._resolve_accessible_skill_ids",
            resolve_mock,
        ):
            resp = authed_client.post(
                "/invocations",
                json={"session_id": "sess-skill", "message": "hi"},
            )
            _ = resp.text  # force the streaming generator to run

        assert resp.status_code == 200
        # Skills were resolved even though the client sent no agent_type,
        # and get_agent was built as a skill agent with them threaded in.
        resolve_mock.assert_awaited()
        kwargs = get_agent_mock.call_args.kwargs
        assert kwargs["agent_type"] == "skill"
        assert kwargs["accessible_skill_ids"] == ["web_research"]

    def test_explicit_chat_opts_out(self, authed_app, authed_client):
        get_agent_mock = MagicMock(return_value=self._mock_agent())
        resolve_mock = AsyncMock(return_value=["web_research"])
        with patch(
            "apis.inference_api.chat.routes.get_agent", get_agent_mock
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes._resolve_accessible_skill_ids",
            resolve_mock,
        ):
            resp = authed_client.post(
                "/invocations",
                json={
                    "session_id": "sess-chat",
                    "message": "hi",
                    "agent_type": "chat",
                },
            )
            _ = resp.text

        assert resp.status_code == 200
        # Explicit chat → no skill resolution, no skills forwarded.
        resolve_mock.assert_not_awaited()
        kwargs = get_agent_mock.call_args.kwargs
        assert kwargs["agent_type"] == "chat"
        assert kwargs["accessible_skill_ids"] is None


# ---------------------------------------------------------------------------
# Single-flight concurrency guard (follow-up to PR #653)
# See docs/specs/session-single-flight-guard.md
# ---------------------------------------------------------------------------


class TestInvocationsSingleFlight:
    """POST /invocations acquires a per-session lease and rejects duplicates."""

    def _mock_agent(self):
        agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield "event: done\ndata: {}\n\n"

        agent.stream_async = fake_stream
        return agent

    def test_duplicate_concurrent_invocation_returns_409(self, authed_app, authed_client):
        """A second turn while the first holds the lease is rejected with 409."""
        from apis.shared.sessions.session_lease import SessionBusyError

        with patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=self._mock_agent(),
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            # Patched at the source module — the route imports it locally at
            # call time, so this binding is what it resolves.
            "apis.shared.sessions.session_lease.acquire_session_lease",
            AsyncMock(side_effect=SessionBusyError("sess-dup")),
        ):
            resp = authed_client.post(
                "/invocations",
                json={"session_id": "sess-dup", "message": "hi"},
            )

        assert resp.status_code == 409
        assert "already streaming" in resp.text.lower()

    def test_lease_released_after_stream_completes(self, authed_app, authed_client):
        """The happy path releases the lease when the SSE stream ends."""
        from apis.shared.sessions.session_lease import SessionLease

        sentinel = SessionLease(session_id="sess-ok", user_id="u1", owner="owner-xyz")
        release_mock = AsyncMock()

        with patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=self._mock_agent(),
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.shared.sessions.session_lease.acquire_session_lease",
            AsyncMock(return_value=sentinel),
        ), patch(
            "apis.shared.sessions.session_lease.release_session_lease",
            release_mock,
        ):
            resp = authed_client.post(
                "/invocations",
                json={"session_id": "sess-ok", "message": "hi"},
            )
            _ = resp.text  # drive the streaming generator (and its finally) to completion

        assert resp.status_code == 200
        release_mock.assert_awaited_with(sentinel)

    def test_preview_session_skips_the_guard(self, authed_app, authed_client):
        """Preview sessions never touch the lease (they don't persist)."""
        acquire_mock = AsyncMock(return_value=None)

        with patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=self._mock_agent(),
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.shared.sessions.session_lease.acquire_session_lease",
            acquire_mock,
        ):
            resp = authed_client.post(
                "/invocations",
                json={"session_id": "preview-abc", "message": "hi"},
            )
            _ = resp.text

        assert resp.status_code == 200
        acquire_mock.assert_not_awaited()
