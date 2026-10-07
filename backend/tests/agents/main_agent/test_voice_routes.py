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


def _token(sub: str, **claims) -> str:
    import jwt as pyjwt
    return pyjwt.encode({"sub": sub, **claims}, "secret")


class TestVoiceIdentityComesFromTheToken:
    """A client-named user_id never becomes the voice identity.

    The config frame, the query string and the ``user-id`` custom header are
    all client-supplied, and the Runtime's ``/ws`` is reachable by anyone
    holding a BFF-client token (forward-auth MCP servers receive them), so the
    identity is the ``sub`` of the bearer the Runtime's authorizer checked on
    the upgrade. Anything that disagrees closes the socket with 4001 before
    VoiceAgent, history or metadata are touched.
    """

    @staticmethod
    def _websocket(config: dict, *, bearer: str = "") -> MagicMock:
        ws = MagicMock()
        ws.headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.receive_json = AsyncMock(return_value={"type": "config", **config})
        return ws

    @staticmethod
    async def _connect(ws: MagicMock, **params) -> MagicMock:
        """Run voice_stream; return the VoiceAgent class mock (construction stops the run)."""
        from apis.inference_api.chat import voice_routes

        agent_class = MagicMock(side_effect=RuntimeError("stop at VoiceAgent"))
        with patch.object(
            voice_routes, "_get_voice_agent_class", return_value=agent_class
        ), patch.object(
            voice_routes, "_always_on_tool_ids_for_voice", new=AsyncMock(return_value=[])
        ), patch.object(
            voice_routes, "_ensure_session_metadata", new=AsyncMock()
        ) as ensure:
            await voice_routes.voice_stream(ws, **params)
        ensure.assert_not_awaited()
        return agent_class

    @staticmethod
    def _assert_refused(ws: MagicMock, agent_class: MagicMock) -> None:
        agent_class.assert_not_called()
        ws.close.assert_awaited_once()
        assert ws.close.await_args.kwargs["code"] == 4001

    @pytest.mark.asyncio
    async def test_frame_user_id_for_another_user_never_reaches_voice_agent(self):
        token = _token("attacker")
        ws = self._websocket(
            {"session_id": "s1", "user_id": "victim", "auth_token": token}, bearer=token
        )

        agent_class = await self._connect(ws)

        self._assert_refused(ws, agent_class)

    @pytest.mark.asyncio
    async def test_forged_frame_token_for_another_user_is_refused(self):
        """The frame token is decoded unverified, so it cannot outrank the upgrade's."""
        ws = self._websocket(
            {"session_id": "s1", "auth_token": _token("victim", **{"cognito:groups": ["admin"]})},
            bearer=_token("attacker"),
        )

        agent_class = await self._connect(ws)

        self._assert_refused(ws, agent_class)

    @pytest.mark.asyncio
    async def test_query_or_custom_header_user_id_for_another_user_is_refused(self):
        token = _token("attacker")
        ws = self._websocket({"session_id": "s1"}, bearer=token)
        ws.headers["x-amzn-bedrock-agentcore-runtime-custom-user-id"] = "victim"

        agent_class = await self._connect(ws, user_id="victim")

        self._assert_refused(ws, agent_class)

    @pytest.mark.asyncio
    async def test_frame_token_without_an_upgrade_token_is_refused(self):
        ws = self._websocket({"session_id": "s1", "user_id": "victim", "auth_token": _token("victim")})

        agent_class = await self._connect(ws)

        self._assert_refused(ws, agent_class)

    @pytest.mark.asyncio
    async def test_relayed_frame_matching_the_token_reaches_voice_agent_as_the_sub(self):
        """The app-api relay's shape: same token on the upgrade and in the frame."""
        token = _token("user-1")
        ws = self._websocket(
            {"session_id": "s1", "user_id": "user-1", "auth_token": token}, bearer=token
        )

        agent_class = await self._connect(ws)

        agent_class.assert_called_once()
        assert agent_class.call_args.kwargs["user_id"] == "user-1"
        assert agent_class.call_args.kwargs["auth_token"] == token

    @pytest.mark.asyncio
    async def test_no_claimed_user_id_takes_the_sub(self):
        token = _token("user-1")
        ws = self._websocket({"session_id": "s1"}, bearer=token)

        agent_class = await self._connect(ws)

        assert agent_class.call_args.kwargs["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_skip_auth_local_dev_accepts_the_query_user_id(self, monkeypatch):
        monkeypatch.setenv("SKIP_AUTH", "true")
        ws = self._websocket({"session_id": "s1"})

        agent_class = await self._connect(ws, user_id="dev-user")

        assert agent_class.call_args.kwargs["user_id"] == "dev-user"

    @pytest.mark.asyncio
    async def test_without_skip_auth_a_query_user_id_alone_is_refused(self, monkeypatch):
        monkeypatch.delenv("SKIP_AUTH", raising=False)
        ws = self._websocket({"session_id": "s1"})

        agent_class = await self._connect(ws, user_id="dev-user")

        self._assert_refused(ws, agent_class)


class TestHandshakeBearerToken:
    def test_reads_the_authorization_bearer(self):
        from apis.inference_api.chat.voice_routes import _handshake_bearer_token
        ws = MagicMock()
        ws.headers = {"authorization": "Bearer abc.def.ghi"}
        assert _handshake_bearer_token(ws) == "abc.def.ghi"

    def test_ignores_the_browser_subprotocol(self):
        from apis.inference_api.chat.voice_routes import _handshake_bearer_token
        ws = MagicMock()
        ws.headers = {
            "sec-websocket-protocol": "base64UrlBearerAuthorization.YWJj, base64UrlBearerAuthorization"
        }
        assert _handshake_bearer_token(ws) == ""


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
