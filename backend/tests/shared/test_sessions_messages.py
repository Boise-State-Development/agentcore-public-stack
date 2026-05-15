"""Task 11: Sessions messages tests (mock AgentCore Memory)."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestGetMessages:
    @pytest.mark.asyncio
    async def test_get_messages_from_cloud(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_msgs = [
            MagicMock(message={"role": "user", "content": [{"text": "hello"}]}),
            MagicMock(message={"role": "assistant", "content": [{"text": "hi"}]}),
        ]
        mock_session_mgr = MagicMock()
        mock_session_mgr.list_messages.return_value = mock_msgs

        with patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=mock_session_mgr), \
             patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"), \
             patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True), \
             patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}), \
             patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]):
            from apis.shared.sessions.messages import get_messages_from_cloud
            result = await get_messages_from_cloud("s1", "u1")
            assert len(result.messages) == 2
            assert result.messages[0].role == "user"

    @pytest.mark.asyncio
    async def test_get_messages_pagination(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_msgs = [MagicMock(message={"role": "user", "content": [{"text": f"msg{i}"}]}) for i in range(10)]
        mock_session_mgr = MagicMock()
        mock_session_mgr.list_messages.return_value = mock_msgs

        with patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=mock_session_mgr), \
             patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"), \
             patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True), \
             patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}), \
             patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]):
            from apis.shared.sessions.messages import get_messages_from_cloud
            result = await get_messages_from_cloud("s1", "u1", limit=3)
            assert len(result.messages) == 3
            assert result.next_token is not None

    @pytest.mark.asyncio
    async def test_get_messages_not_available(self):
        from apis.shared.sessions.messages import get_messages
        with patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", False):
            with pytest.raises(RuntimeError, match="bedrock_agentcore"):
                await get_messages("s1", "u1")
