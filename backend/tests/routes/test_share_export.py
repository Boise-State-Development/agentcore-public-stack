"""Tests for share export (conversation fork) logic.

Tests the service-level methods that copy snapshot messages into
AgentCore Memory when a user exports a shared conversation.
"""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.app_api.shares.service import ShareService


# ---------------------------------------------------------------------------
# _snapshot_msg_to_converse — pure function, no mocking needed
# ---------------------------------------------------------------------------


class TestSnapshotMsgToConverse:
    """Unit tests for the MessageResponse→Converse format converter."""

    def test_text_message(self):
        msg = {
            "id": "msg-sess-0",
            "role": "user",
            "content": [{"type": "text", "text": "Hello"}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result == {"role": "user", "content": [{"text": "Hello"}]}

    def test_assistant_with_tool_use(self):
        tool_use_payload = {"toolUseId": "t1", "name": "search", "input": {"q": "test"}}
        msg = {
            "id": "msg-sess-1",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me search."},
                {"type": "toolUse", "toolUse": tool_use_payload},
            ],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["role"] == "assistant"
        assert len(result["content"]) == 2
        assert result["content"][0] == {"text": "Let me search."}
        assert result["content"][1] == {"toolUse": tool_use_payload}

    def test_tool_result_block(self):
        tool_result_payload = {"toolUseId": "t1", "content": [{"text": "result"}]}
        msg = {
            "id": "msg-sess-2",
            "role": "user",
            "content": [{"type": "toolResult", "toolResult": tool_result_payload}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"][0] == {"toolResult": tool_result_payload}

    def test_image_block(self):
        image_payload = {"format": "png", "source": {"bytes": "base64data"}}
        msg = {
            "id": "msg-sess-3",
            "role": "user",
            "content": [{"type": "image", "image": image_payload}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"][0] == {"image": image_payload}

    def test_document_block(self):
        doc_payload = {"format": "pdf", "name": "report", "source": {"bytes": "base64data"}}
        msg = {
            "id": "msg-sess-4",
            "role": "user",
            "content": [{"type": "document", "document": doc_payload}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"][0] == {"document": doc_payload}

    def test_reasoning_content_block(self):
        reasoning_payload = {"reasoningText": {"text": "thinking..."}}
        msg = {
            "id": "msg-sess-5",
            "role": "assistant",
            "content": [{"type": "reasoningContent", "reasoningContent": reasoning_payload}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"][0] == {"reasoningContent": reasoning_payload}

    def test_skips_system_role(self):
        msg = {
            "id": "msg-sess-6",
            "role": "system",
            "content": [{"type": "text", "text": "system prompt"}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        assert ShareService._snapshot_msg_to_converse(msg) is None

    def test_skips_empty_content(self):
        msg = {"id": "msg-sess-7", "role": "user", "content": [], "createdAt": "2025-06-01T00:00:00Z"}
        assert ShareService._snapshot_msg_to_converse(msg) is None

    def test_skips_unknown_block_types(self):
        msg = {
            "id": "msg-sess-8",
            "role": "user",
            "content": [{"type": "unknown", "data": "stuff"}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        assert ShareService._snapshot_msg_to_converse(msg) is None

    def test_mixed_content_skips_empty_text(self):
        """A text block with empty string should be skipped."""
        msg = {
            "id": "msg-sess-9",
            "role": "assistant",
            "content": [
                {"type": "text", "text": ""},
                {"type": "text", "text": "real content"},
            ],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert len(result["content"]) == 1
        assert result["content"][0] == {"text": "real content"}

    def test_strips_metadata_and_id(self):
        """Output should only contain role and content — no id, createdAt, metadata."""
        msg = {
            "id": "msg-sess-10",
            "role": "user",
            "content": [{"type": "text", "text": "hi"}],
            "createdAt": "2025-06-01T00:00:00Z",
            "metadata": {"tokenUsage": {"inputTokens": 10}},
            "citations": [{"url": "https://example.com"}],
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert set(result.keys()) == {"role", "content"}


# ---------------------------------------------------------------------------
# _copy_messages_to_memory — needs AgentCore Memory mocked
# ---------------------------------------------------------------------------


class TestCopyMessagesToMemory:
    """Tests for writing snapshot messages into AgentCore Memory."""

    @pytest.fixture
    def service(self):
        """ShareService with DynamoDB disabled (we only test memory copying)."""
        with patch.dict(os.environ, {"SHARED_CONVERSATIONS_TABLE_NAME": ""}):
            svc = ShareService()
        return svc

    @pytest.mark.asyncio
    async def test_empty_snapshot_returns_zero(self, service):
        count = await service._copy_messages_to_memory("sess-new", "user-1", [])
        assert count == 0

    @pytest.mark.asyncio
    async def test_copies_valid_messages(self, service):
        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:00Z"},
            {"id": "msg-1", "role": "assistant", "content": [{"type": "text", "text": "Hi there"}], "createdAt": "2025-06-01T00:00:01Z"},
        ]

        mock_mgr = MagicMock()
        mock_mgr.append_message = MagicMock()

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"):
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 2
        assert mock_mgr.append_message.call_count == 2

        # Verify first call was the user message in Converse format
        first_call_msg = mock_mgr.append_message.call_args_list[0][0][0]
        assert first_call_msg == {"role": "user", "content": [{"text": "Hello"}]}

        # Verify second call was the assistant message
        second_call_msg = mock_mgr.append_message.call_args_list[1][0][0]
        assert second_call_msg == {"role": "assistant", "content": [{"text": "Hi there"}]}

    @pytest.mark.asyncio
    async def test_skips_unconvertible_messages(self, service):
        snapshot = [
            {"id": "msg-0", "role": "system", "content": [{"type": "text", "text": "system"}], "createdAt": "2025-06-01T00:00:00Z"},
            {"id": "msg-1", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:01Z"},
        ]

        mock_mgr = MagicMock()
        mock_mgr.append_message = MagicMock()

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"):
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 1
        assert mock_mgr.append_message.call_count == 1

    @pytest.mark.asyncio
    async def test_continues_on_individual_message_failure(self, service):
        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "First"}], "createdAt": "2025-06-01T00:00:00Z"},
            {"id": "msg-1", "role": "user", "content": [{"type": "text", "text": "Second"}], "createdAt": "2025-06-01T00:00:01Z"},
            {"id": "msg-2", "role": "assistant", "content": [{"type": "text", "text": "Third"}], "createdAt": "2025-06-01T00:00:02Z"},
        ]

        mock_mgr = MagicMock()
        # Second call raises, first and third succeed
        mock_mgr.append_message = MagicMock(side_effect=[None, RuntimeError("boom"), None])

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"):
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 2  # 1st and 3rd succeeded
        assert mock_mgr.append_message.call_count == 3

    @pytest.mark.asyncio
    async def test_returns_zero_when_memory_id_missing(self, service):
        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:00Z"},
        ]

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "", "AWS_REGION": "us-east-1"}):
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 0


# ---------------------------------------------------------------------------
# export_shared_conversation — integration of the above
# ---------------------------------------------------------------------------


class TestExportSharedConversation:
    """Tests that export creates a session with copied messages."""

    @pytest.fixture
    def service(self):
        with patch.dict(os.environ, {"SHARED_CONVERSATIONS_TABLE_NAME": "shares-table"}):
            with patch("boto3.resource"):
                svc = ShareService()
        return svc

    def _make_share_item(self, messages=None):
        return {
            "share_id": "share-001",
            "session_id": "orig-sess",
            "owner_id": "owner-001",
            "owner_email": "owner@example.com",
            "access_level": "public",
            "created_at": "2025-06-01T00:00:00Z",
            "metadata": {"title": "My Chat"},
            "messages": messages or [
                {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:00Z"},
                {"id": "msg-1", "role": "assistant", "content": [{"type": "text", "text": "Hi"}], "createdAt": "2025-06-01T00:00:01Z"},
            ],
        }

    @pytest.mark.asyncio
    async def test_export_copies_messages_and_sets_count(self, service):
        from apis.shared.auth.models import User

        requester = User(email="viewer@example.com", user_id="viewer-001", name="Viewer", roles=["User"])
        share_item = self._make_share_item()

        with patch.object(service, "_get_share_item", return_value=share_item), \
             patch.object(service, "_check_access"), \
             patch.object(service, "_copy_messages_to_memory", new_callable=AsyncMock, return_value=2) as mock_copy, \
             patch("apis.app_api.shares.service.store_session_metadata", new_callable=AsyncMock) as mock_store:
            result = await service.export_shared_conversation("share-001", requester)

        assert "sessionId" in result
        assert result["title"] == "My Chat (shared)"

        # Verify messages were passed to copy
        mock_copy.assert_called_once()
        call_args = mock_copy.call_args
        assert call_args[0][1] == "viewer-001"  # user_id
        assert len(call_args[0][2]) == 2  # 2 snapshot messages

        # Verify session metadata has correct message_count
        mock_store.assert_called_once()
        stored_meta = mock_store.call_args[1]["session_metadata"]
        assert stored_meta.message_count == 2

    @pytest.mark.asyncio
    async def test_export_empty_snapshot_creates_session_with_zero_messages(self, service):
        from apis.shared.auth.models import User

        requester = User(email="viewer@example.com", user_id="viewer-001", name="Viewer", roles=["User"])
        share_item = self._make_share_item(messages=[])

        with patch.object(service, "_get_share_item", return_value=share_item), \
             patch.object(service, "_check_access"), \
             patch.object(service, "_copy_messages_to_memory", new_callable=AsyncMock, return_value=0) as mock_copy, \
             patch("apis.app_api.shares.service.store_session_metadata", new_callable=AsyncMock):
            result = await service.export_shared_conversation("share-001", requester)

        assert result["title"] == "My Chat (shared)"
        mock_copy.assert_called_once()
