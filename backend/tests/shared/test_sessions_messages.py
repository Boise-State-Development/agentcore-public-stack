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

    @pytest.mark.asyncio
    async def test_ui_resources_sidecar_hydrates_with_origin_override(self, monkeypatch):
        """Persisted MCP App UI resources ride the first-page response shaped
        like the live `ui_resource` event, with the process sandbox origin
        preferred over the value captured at write time."""
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AGENTCORE_MCP_APPS_SANDBOX_ORIGIN", "https://fresh.example")

        mock_session_mgr = MagicMock()
        mock_session_mgr.list_messages.return_value = [
            MagicMock(message={"role": "assistant", "content": [{"text": "hi"}]}),
        ]

        fake_store = MagicMock()
        fake_store.list_for_session.return_value = [
            {
                "toolUseId": "tu-1",
                "resourceUri": "ui://srv/widget",
                "html": "<main>app</main>",
                "mimeType": "text/html;profile=mcp-app",
                "csp": {"connectDomains": ["https://api.test"]},
                "permissions": {"clipboardWrite": {}},
                "sandboxOrigin": "https://stale.example",
            }
        ]

        with patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=mock_session_mgr), \
             patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"), \
             patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True), \
             patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}), \
             patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]), \
             patch("apis.shared.mcp_apps.ui_resource_store.get_ui_resource_store", return_value=fake_store):
            from apis.shared.sessions.messages import get_messages_from_cloud
            result = await get_messages_from_cloud("s1", "u1")

        assert len(result.ui_resources) == 1
        res = result.ui_resources[0]
        assert res["type"] == "ui_resource"
        assert res["toolUseId"] == "tu-1"
        assert res["html"] == "<main>app</main>"
        assert res["csp"] == {"connectDomains": ["https://api.test"]}
        # Fresh process origin wins over the persisted (possibly stale) one.
        assert res["sandboxOrigin"] == "https://fresh.example"

    @pytest.mark.asyncio
    async def test_ui_resources_omitted_on_subsequent_pages(self, monkeypatch):
        """Resources ride only the first page (no incoming next_token) — the
        SPA keys by toolUseId and holds them all regardless of page."""
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_session_mgr = MagicMock()
        mock_session_mgr.list_messages.return_value = [
            MagicMock(message={"role": "user", "content": [{"text": f"m{i}"}]}) for i in range(5)
        ]
        fake_store = MagicMock()
        fake_store.list_for_session.return_value = [{"toolUseId": "tu-1", "html": "<x/>"}]

        import base64
        page2 = base64.b64encode(b"2").decode()

        with patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=mock_session_mgr), \
             patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"), \
             patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True), \
             patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}), \
             patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]), \
             patch("apis.shared.mcp_apps.ui_resource_store.get_ui_resource_store", return_value=fake_store):
            from apis.shared.sessions.messages import get_messages_from_cloud
            result = await get_messages_from_cloud("s1", "u1", limit=2, next_token=page2)

        assert result.ui_resources == []
