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


class TestArchiveFallback:
    """When Memory holds no events (expired), the route reads the conversation archive."""

    @staticmethod
    def _memory(messages):
        mgr = MagicMock()
        mgr.list_messages.return_value = messages
        return patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=mgr)

    @staticmethod
    def _patches():
        return [
            patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"),
            patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True),
            patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}),
            patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]),
        ]

    async def _get(self, memory_messages, session_id="s1", **kwargs):
        from contextlib import ExitStack

        from apis.shared.sessions.messages import get_messages_from_cloud

        with ExitStack() as stack:
            for p in [self._memory(memory_messages), *self._patches()]:
                stack.enter_context(p)
            return await get_messages_from_cloud(session_id, "u1", **kwargs)

    @pytest.fixture()
    def archive(self, aws, monkeypatch):
        import boto3

        from apis.shared.conversation_archive import build_turn, put_turns, reset_bucket_cache

        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", "test-conversation-archive")
        reset_bucket_cache()
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-conversation-archive")

        def write(session_id, index, user_text, assistant_text):
            put_turns([build_turn(
                user_id="u1", session_id=session_id, message_index=index, user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": [{"text": assistant_text}]}] if assistant_text else [],
                created_at="2026-05-01T10:00:00+00:00",
            )])

        yield write
        reset_bucket_cache()

    @pytest.mark.asyncio
    async def test_empty_memory_serves_the_archived_turns_at_their_original_ids(self, archive):
        archive("s1", 0, "first question", "first answer")
        archive("s1", 4, "second question", "second answer")

        result = await self._get([])

        assert [(m.id, m.role, m.content[0].text) for m in result.messages] == [
            ("msg-s1-0", "user", "first question"),
            ("msg-s1-1", "assistant", "first answer"),
            ("msg-s1-4", "user", "second question"),
            ("msg-s1-5", "assistant", "second answer"),
        ]
        assert result.messages[0].created_at == "2026-05-01T10:00:00+00:00"
        assert result.next_token is None

    @pytest.mark.asyncio
    async def test_a_turn_without_a_reply_is_just_the_user_message(self, archive):
        archive("s1", 0, "unanswered", "")
        result = await self._get([])
        assert [m.id for m in result.messages] == ["msg-s1-0"]

    @pytest.mark.asyncio
    async def test_archived_messages_paginate_like_memory_ones(self, archive):
        for i in range(3):
            archive("s1", i * 2, f"q{i}", f"a{i}")

        first = await self._get([], limit=4)
        second = await self._get([], limit=4, next_token=first.next_token)

        assert [m.id for m in first.messages] == ["msg-s1-0", "msg-s1-1", "msg-s1-2", "msg-s1-3"]
        assert [m.id for m in second.messages] == ["msg-s1-4", "msg-s1-5"]
        assert second.next_token is None

    @pytest.mark.asyncio
    async def test_memory_with_events_never_reads_the_archive(self, archive):
        archive("s1", 0, "archived copy", "old")
        with patch("apis.shared.conversation_archive.read_session_turns", side_effect=AssertionError("read")):
            result = await self._get([MagicMock(message={"role": "user", "content": [{"text": "live"}]})])
        assert [m.content[0].text for m in result.messages] == ["live"]

    @pytest.mark.asyncio
    async def test_nothing_archived_is_the_same_empty_history(self, archive):
        result = await self._get([])
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_preview_sessions_never_read_the_archive(self, archive):
        with patch("apis.shared.conversation_archive.read_session_turns", side_effect=AssertionError("read")):
            result = await self._get([], session_id="preview-abc")
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_an_archive_failure_falls_back_to_the_empty_history(self, archive):
        with patch("apis.shared.conversation_archive.read_session_turns", side_effect=RuntimeError("boom")):
            result = await self._get([])
        assert result.messages == []
