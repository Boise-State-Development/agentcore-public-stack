"""Tests for voice WebSocket route — auth, message dispatch, debug endpoints."""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from apis.inference_api.chat.voice_routes import (
    _extract_user_from_token,
    _active_sessions,
    router,
)


class TestExtractUserFromToken:
    """Req VR-1: JWT token extraction for WebSocket auth."""

    def test_valid_token_extracts_user_id(self):
        import jwt as pyjwt
        token = pyjwt.encode({"sub": "user-123", "email": "test@example.com"}, "secret")
        result = _extract_user_from_token(token)
        assert result is not None
        assert result["user_id"] == "user-123"
        assert result["email"] == "test@example.com"
        assert result["raw_token"] == token

    def test_missing_sub_returns_none(self):
        import jwt as pyjwt
        token = pyjwt.encode({"email": "no-sub@example.com"}, "secret")
        result = _extract_user_from_token(token)
        assert result is None

    def test_empty_token_returns_none(self):
        assert _extract_user_from_token("") is None

    def test_none_token_returns_none(self):
        assert _extract_user_from_token(None) is None

    def test_malformed_token_returns_none(self):
        result = _extract_user_from_token("not.a.valid.jwt.token.at.all")
        assert result is None

    def test_preferred_username_fallback(self):
        import jwt as pyjwt
        token = pyjwt.encode({"sub": "user-456", "preferred_username": "jdoe"}, "secret")
        result = _extract_user_from_token(token)
        assert result["email"] == "jdoe"


class TestActiveSessionsManagement:
    """Req VR-2: Active session tracking."""

    def test_sessions_dict_exists(self):
        assert isinstance(_active_sessions, dict)

    def test_sessions_can_be_added_and_removed(self):
        _active_sessions["test-session"] = MagicMock()
        assert "test-session" in _active_sessions
        del _active_sessions["test-session"]
        assert "test-session" not in _active_sessions


class TestVoiceAgentLazyImport:
    """Req VR-3: Lazy VoiceAgent import."""

    def test_get_voice_agent_class_returns_class(self):
        from apis.inference_api.chat.voice_routes import _get_voice_agent_class
        cls = _get_voice_agent_class()
        assert cls is not None
        assert cls.__name__ == "VoiceAgent"

    def test_get_voice_agent_class_caches(self):
        from apis.inference_api.chat.voice_routes import _get_voice_agent_class
        cls1 = _get_voice_agent_class()
        cls2 = _get_voice_agent_class()
        assert cls1 is cls2


class TestDebugEndpoints:
    """Req VR-4: Debug endpoint behavior."""

    @pytest.fixture(autouse=True)
    def clean_sessions(self):
        """Ensure clean session state for each test."""
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self):
        from apis.inference_api.chat.voice_routes import list_voice_sessions
        result = await list_voice_sessions()
        assert result["count"] == 0
        assert result["active_sessions"] == []

    @pytest.mark.asyncio
    async def test_list_sessions_with_entries(self):
        from apis.inference_api.chat.voice_routes import list_voice_sessions
        _active_sessions["sess-1"] = MagicMock()
        _active_sessions["sess-2"] = MagicMock()
        result = await list_voice_sessions()
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_stop_session_not_found(self):
        from apis.inference_api.chat.voice_routes import stop_voice_session
        result = await stop_voice_session("nonexistent")
        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_stop_session_calls_stop(self):
        from apis.inference_api.chat.voice_routes import stop_voice_session
        mock_agent = AsyncMock()
        _active_sessions["sess-stop"] = mock_agent
        result = await stop_voice_session("sess-stop")
        assert result["status"] == "stopped"
        mock_agent.stop.assert_called_once()
        assert "sess-stop" not in _active_sessions


class TestVoiceSessionOwnership:
    """A voice connect must not open another user's session id.

    `_ensure_session_metadata` creates a META row for the caller on whatever
    session id the config frame names. On an id another user owns, that row is
    a cross-user fork, and the text route's ownership guard then reads it as
    the caller's own session and lets their next text turn through.
    """

    @staticmethod
    def _websocket(session_id: str, user_id: str) -> MagicMock:
        ws = MagicMock()
        ws.headers = {}
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.receive_json = AsyncMock(
            return_value={"type": "config", "session_id": session_id, "user_id": user_id}
        )
        return ws

    @pytest.mark.asyncio
    async def test_session_owned_by_another_user_is_refused_before_anything_runs(self):
        from apis.inference_api.chat import voice_routes

        ws = self._websocket("owners-session", "intruder")
        with patch.object(
            voice_routes, "session_owned_by_other_user", new=AsyncMock(return_value=True)
        ) as owned, patch.object(
            voice_routes, "_ensure_session_metadata", new=AsyncMock()
        ) as ensure, patch.object(
            voice_routes, "_get_voice_agent_class"
        ) as agent_class, patch.object(
            voice_routes, "_always_on_tool_ids_for_voice", new=AsyncMock(return_value=[])
        ):
            await voice_routes.voice_stream(ws)

        owned.assert_awaited_once_with("owners-session", "intruder")
        ensure.assert_not_awaited()
        agent_class.assert_not_called()
        ws.close.assert_awaited_once()
        assert ws.close.await_args.kwargs["code"] == 4004
        assert "owners-session" not in voice_routes._active_sessions

    @pytest.mark.asyncio
    async def test_own_or_new_session_proceeds_to_the_agent(self):
        from apis.inference_api.chat import voice_routes

        class _Stop(Exception):
            pass

        ws = self._websocket("my-session", "owner")
        with patch.object(
            voice_routes, "session_owned_by_other_user", new=AsyncMock(return_value=False)
        ), patch.object(
            voice_routes, "_get_voice_agent_class", side_effect=_Stop
        ) as agent_class, patch.object(
            voice_routes, "_always_on_tool_ids_for_voice", new=AsyncMock(return_value=[])
        ):
            try:
                await voice_routes.voice_stream(ws)
            except _Stop:
                pass

        agent_class.assert_called_once()
