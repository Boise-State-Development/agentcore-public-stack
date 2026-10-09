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

    def test_image_block_becomes_not_copied_note(self):
        """B1: a snapshot image has the display shape (format/data, no source),
        which Bedrock rejects. It must not reach the fork's history."""
        msg = {
            "id": "msg-sess-3",
            "role": "user",
            "content": [{"type": "image", "image": {"format": "png", "data": "aGk="}}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"] == [
            {"text": "[Attachments from the original conversation were not copied: 1 file(s)]"}
        ]

    def test_document_block_becomes_not_copied_note(self):
        """B1 (reproduced on dev): the display-only document block was copied
        verbatim and every later turn failed with 'Missing required parameter in
        messages[0].content[1].document: "source"'."""
        msg = {
            "id": "msg-sess-4",
            "role": "user",
            "content": [
                {"type": "text", "text": "Summarize this"},
                {"type": "document", "document": {"format": "pdf", "name": "report"}},
            ],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"] == [
            {"text": "Summarize this"},
            {"text": "[Attachments from the original conversation were not copied: report]"},
        ]
        assert not any("document" in block or "image" in block for block in result["content"])

    def test_attached_files_marker_names_the_attachments(self):
        """B4: the marker carries the real filenames (inline and diverted), so
        it wins over the sanitized document-block names, and is itself removed."""
        msg = {
            "id": "msg-sess-4b",
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare these\n\n[Attached files: report.pdf, grades.xlsx]"},
                {"type": "document", "document": {"format": "pdf", "name": "report"}},
            ],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"] == [
            {"text": "Compare these"},
            {"text": "[Attachments from the original conversation were not copied: report.pdf, grades.xlsx]"},
        ]

    def test_media_inside_tool_result_becomes_text(self):
        msg = {
            "id": "msg-sess-4c",
            "role": "user",
            "content": [{"type": "toolResult", "toolResult": {
                "toolUseId": "t1",
                "status": "success",
                "content": [
                    {"text": "Took a screenshot"},
                    {"image": {"format": "png", "data": "aGk="}},
                    {"document": {"format": "pdf", "name": "page", "data": "aGk="}},
                ],
            }}],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        tool_result = result["content"][0]["toolResult"]
        assert tool_result["toolUseId"] == "t1"
        assert tool_result["status"] == "success"
        assert tool_result["content"] == [
            {"text": "Took a screenshot"},
            {"text": "[Image from the original conversation was not copied]"},
            {"text": '[Document "page" from the original conversation was not copied]'},
        ]

    def test_user_message_uses_display_text_not_augmented_prompt(self):
        """B3: the fork showed the RAG-augmented prompt as the author's message."""
        augmented = (
            "The following context is retrieved from the assistant's knowledge base...\n"
            "[Context 1] Syllabus excerpt\n\nWhen is the midterm?"
        )
        msg = {
            "id": "msg-sess-4d",
            "role": "user",
            "content": [{"type": "text", "text": augmented}],
            "metadata": {"displayText": "When is the midterm?"},
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"] == [{"text": "When is the midterm?"}]

    def test_display_text_with_attachments_keeps_the_note(self):
        msg = {
            "id": "msg-sess-4e",
            "role": "user",
            "content": [
                {"type": "text", "text": "<interruption_note>x</interruption_note>\n\nRead it\n\n[Attached files: a.pdf]"},
                {"type": "document", "document": {"format": "pdf", "name": "a"}},
            ],
            "metadata": {"displayText": "Read it"},
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"] == [
            {"text": "Read it"},
            {"text": "[Attachments from the original conversation were not copied: a.pdf]"},
        ]

    def test_display_text_ignored_on_assistant_messages(self):
        msg = {
            "id": "msg-sess-4f",
            "role": "assistant",
            "content": [{"type": "text", "text": "Answer"}],
            "metadata": {"displayText": "not mine"},
            "createdAt": "2025-06-01T00:00:00Z",
        }
        assert ShareService._snapshot_msg_to_converse(msg)["content"] == [{"text": "Answer"}]

    def test_tool_results_stay_ahead_of_text(self):
        """A steer lands as text after the batch's tool results; Claude rejects
        a user message whose text precedes its toolResult blocks."""
        tool_result = {"toolUseId": "t1", "content": [{"text": "ok"}]}
        msg = {
            "id": "msg-sess-4g",
            "role": "user",
            "content": [
                {"type": "toolResult", "toolResult": tool_result},
                {"type": "text", "text": "also check BIO 102"},
            ],
            "createdAt": "2025-06-01T00:00:00Z",
        }
        result = ShareService._snapshot_msg_to_converse(msg)
        assert result["content"] == [{"toolResult": tool_result}, {"text": "also check BIO 102"}]

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
        mock_mgr.create_message = MagicMock()

        # Mock SessionMessage.from_message to return a simple object
        mock_session_msg = MagicMock()

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"), \
             patch("strands.types.session.SessionMessage") as mock_session_msg_class:
            mock_session_msg_class.from_message.return_value = mock_session_msg
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 2
        assert mock_mgr.create_message.call_count == 2

        # Verify calls used session_id and "default" namespace
        first_call = mock_mgr.create_message.call_args_list[0]
        assert first_call[0][0] == "sess-new"  # session_id
        assert first_call[0][1] == "default"  # namespace

        second_call = mock_mgr.create_message.call_args_list[1]
        assert second_call[0][0] == "sess-new"
        assert second_call[0][1] == "default"

    @pytest.mark.asyncio
    async def test_writes_agent_record_before_messages(self, service):
        """B2: without the session's AGENT record, Strands' initialize() takes
        the new-agent branch and the fork's first turn runs on an empty history."""
        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:00Z"},
        ]
        mock_mgr = MagicMock()

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"), \
             patch("strands.types.session.SessionMessage"):
            await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert [c[0] for c in mock_mgr.method_calls if c[0] in ("create_agent", "create_message")] == [
            "create_agent", "create_message",
        ]
        session_id, session_agent = mock_mgr.create_agent.call_args[0]
        assert session_id == "sess-new"
        assert session_agent.agent_id == "default"
        assert session_agent.conversation_manager_state["removed_message_count"] == 0
        # Messages go under the same agent id the record names.
        assert mock_mgr.create_message.call_args[0][1] == session_agent.agent_id

    def test_fork_agent_record_restores_into_the_runtime_manager(self):
        """The record's conversation-manager state must be one the runtime's
        manager accepts: ``restore_from_session`` raises on a class mismatch,
        which would fail the fork's first turn outright."""
        from strands.agent.conversation_manager import SlidingWindowConversationManager

        from agents.main_agent.core.agent_factory import AgentFactory

        runtime_manager = AgentFactory.build_conversation_manager()
        state = SlidingWindowConversationManager().get_state()
        assert runtime_manager.restore_from_session(state) is None
        assert runtime_manager.removed_message_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_agent_record", [True, False])
    async def test_runtime_restore_sees_the_copied_conversation(self, service, tmp_path, write_agent_record):
        """B2 end to end against Strands' real ``RepositorySessionManager.initialize``.

        ``FileSessionManager`` shares that base with the AgentCore Memory
        manager, so the export writes into it and a fresh manager restores a
        real ``Agent`` built with the runtime's conversation manager. Without
        the AGENT record (the pre-fix export) the restore loads nothing.
        """
        from types import SimpleNamespace

        from strands import Agent
        from strands.models import BedrockModel
        from strands.session.file_session_manager import FileSessionManager

        from agents.main_agent.core.agent_factory import AgentFactory

        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "augmented"}],
             "metadata": {"displayText": "Hello"}, "createdAt": "2025-06-01T00:00:00Z"},
            {"id": "msg-1", "role": "assistant", "content": [{"type": "text", "text": "Hi there"}],
             "createdAt": "2025-06-01T00:00:01Z"},
        ]

        def export_manager(agentcore_memory_config, region_name):
            mgr = FileSessionManager(session_id="sess-new", storage_dir=str(tmp_path))
            mgr.memory_client = SimpleNamespace(gmdp_client=SimpleNamespace(create_event=lambda **_: None))
            if not write_agent_record:
                mgr.create_agent = lambda *a, **k: None
            return mgr

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager",
                   side_effect=export_manager), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"):
            assert await service._copy_messages_to_memory("sess-new", "user-1", snapshot) == 2

        agent = Agent(
            model=BedrockModel(model_id="test-model", region_name="us-east-1"),
            conversation_manager=AgentFactory.build_conversation_manager(),
            session_manager=FileSessionManager(session_id="sess-new", storage_dir=str(tmp_path)),
        )

        if write_agent_record:
            assert agent.messages == [
                {"role": "user", "content": [{"text": "Hello"}]},
                {"role": "assistant", "content": [{"text": "Hi there"}]},
            ]
        else:
            assert agent.messages == []

    @pytest.mark.asyncio
    async def test_skips_unconvertible_messages(self, service):
        snapshot = [
            {"id": "msg-0", "role": "system", "content": [{"type": "text", "text": "system"}], "createdAt": "2025-06-01T00:00:00Z"},
            {"id": "msg-1", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:01Z"},
        ]

        mock_mgr = MagicMock()
        mock_mgr.create_message = MagicMock()
        mock_session_msg = MagicMock()

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"), \
             patch("strands.types.session.SessionMessage") as mock_session_msg_class:
            mock_session_msg_class.from_message.return_value = mock_session_msg
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 1
        assert mock_mgr.create_message.call_count == 1

    @pytest.mark.asyncio
    async def test_continues_on_individual_message_failure(self, service):
        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "First"}], "createdAt": "2025-06-01T00:00:00Z"},
            {"id": "msg-1", "role": "user", "content": [{"type": "text", "text": "Second"}], "createdAt": "2025-06-01T00:00:01Z"},
            {"id": "msg-2", "role": "assistant", "content": [{"type": "text", "text": "Third"}], "createdAt": "2025-06-01T00:00:02Z"},
        ]

        mock_mgr = MagicMock()
        # Second call raises, first and third succeed
        mock_mgr.create_message = MagicMock(side_effect=[None, RuntimeError("boom"), None])
        mock_session_msg = MagicMock()

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"), \
             patch("strands.types.session.SessionMessage") as mock_session_msg_class:
            mock_session_msg_class.from_message.return_value = mock_session_msg
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 2  # 1st and 3rd succeeded
        assert mock_mgr.create_message.call_count == 3

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


class TestForkFeedsTheConversationArchive:
    """A fork never runs a turn, so the runtime's after-`done` archive write
    never sees it; the export path archives the copied turns itself, under the
    forker's id, positioned as the new session's message ids count them
    (docs/specs/conversation-search.md §4)."""

    @pytest.fixture
    def service(self):
        with patch.dict(os.environ, {"SHARED_CONVERSATIONS_TABLE_NAME": ""}):
            svc = ShareService()
        return svc

    @pytest.mark.asyncio
    async def test_copy_reports_the_messages_that_landed_in_order(self, service):
        snapshot = [
            {"id": "m0", "role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"id": "m1", "role": "system", "content": [{"type": "text", "text": "dropped"}]},
            {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
        ]
        written = []
        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=MagicMock()), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"), \
             patch("strands.types.session.SessionMessage"):
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot, written=written)

        assert count == 2
        assert written == [
            {"role": "user", "content": [{"text": "Hello"}]},
            {"role": "assistant", "content": [{"text": "Hi"}]},
        ]

    @pytest.mark.asyncio
    async def test_export_archives_the_copied_turns_under_the_forker(self, monkeypatch):
        from apis.shared.auth.models import User
        from apis.shared.conversation_archive import drain_pending

        monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
        with patch.dict(os.environ, {"SHARED_CONVERSATIONS_TABLE_NAME": "shares-table"}), patch("boto3.resource"):
            service = ShareService()
        requester = User(email="v@example.com", user_id="viewer-001", name="Viewer", roles=["User"])
        share_item = {
            "share_id": "share-001", "session_id": "orig", "owner_id": "owner-001",
            "access_level": "public", "created_at": "2025-06-01T00:00:00Z",
            "metadata": {"title": "Chat"}, "messages": [{"id": "x"}],
        }
        copied = [
            {"role": "user", "content": [{"text": "q1"}]},
            {"role": "assistant", "content": [{"text": "a1"}]},
            {"role": "user", "content": [{"text": "q2"}]},
            {"role": "assistant", "content": [{"text": "a2"}]},
        ]

        async def _fake_copy(session_id, user_id, snapshot, written=None):
            written.extend(copied)
            return len(copied)

        archived = []

        async def _fake_write(turns):
            archived.extend(turns)
            return len(turns)

        with patch.object(service, "_get_share_item", return_value=share_item), \
             patch.object(service, "_check_access"), \
             patch.object(service, "_copy_messages_to_memory", side_effect=_fake_copy), \
             patch("apis.app_api.shares.service.store_session_metadata", new_callable=AsyncMock), \
             patch("apis.shared.conversation_archive.write_turns", _fake_write):
            result = await service.export_shared_conversation("share-001", requester)
            await drain_pending()

        assert [(t.user_id, t.session_id, t.message_index, t.user_text) for t in archived] == [
            ("viewer-001", result["sessionId"], 0, "q1"),
            ("viewer-001", result["sessionId"], 2, "q2"),
        ]

    @pytest.mark.asyncio
    async def test_flag_off_archives_nothing(self, monkeypatch):
        monkeypatch.delenv("CONVERSATION_INDEX_ENABLED", raising=False)
        with patch("apis.shared.conversation_archive.schedule") as schedule:
            ShareService._archive_forked_turns(
                "sess", "user", [{"role": "user", "content": [{"text": "q"}]}], None
            )
        schedule.assert_not_called()


# ---------------------------------------------------------------------------
# Share-fork events skip long-term extraction (Shared Projects Phase 0.2)
# ---------------------------------------------------------------------------


class _RecordingDataPlane:
    """Stands in for the bedrock-agentcore data-plane client; records create_event kwargs."""

    def __init__(self):
        self.calls = []

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        return {"event": {"eventId": f"evt-{len(self.calls)}"}}


class TestForkSkipsLongTermExtraction:
    def test_wrapper_sets_skip_and_keeps_an_explicit_mode(self):
        from types import SimpleNamespace

        from apis.app_api.shares.service import _skip_long_term_extraction

        dp = _RecordingDataPlane()
        mgr = SimpleNamespace(memory_client=SimpleNamespace(gmdp_client=dp))
        _skip_long_term_extraction(mgr)

        mgr.memory_client.gmdp_client.create_event(memoryId="mem-1", actorId="user-1")
        mgr.memory_client.gmdp_client.create_event(memoryId="mem-1", extractionMode="OTHER")

        assert dp.calls[0]["extractionMode"] == "SKIP"
        assert dp.calls[0]["memoryId"] == "mem-1"
        assert dp.calls[1]["extractionMode"] == "OTHER"

    def test_sdk_create_event_reaches_the_wrapped_data_plane_call(self):
        """The wrapper relies on the pinned SDK funnelling MemoryClient.create_event
        into gmdp_client.create_event. If an SDK upgrade stops doing that, fork
        messages would silently feed extraction again; this test catches it."""
        from types import SimpleNamespace

        from bedrock_agentcore.memory.client import MemoryClient

        from apis.app_api.shares.service import _skip_long_term_extraction

        client = MemoryClient(region_name="us-west-2")
        dp = _RecordingDataPlane()
        client.gmdp_client = dp
        _skip_long_term_extraction(SimpleNamespace(memory_client=client))

        client.create_event(
            memory_id="mem-1",
            actor_id="user-1",
            session_id="sess-1",
            messages=[("Hello", "USER")],
        )

        assert len(dp.calls) == 1
        assert dp.calls[0]["extractionMode"] == "SKIP"
        assert dp.calls[0]["actorId"] == "user-1"

    @pytest.mark.asyncio
    async def test_copy_messages_wraps_the_managers_client(self):
        with patch.dict(os.environ, {"SHARED_CONVERSATIONS_TABLE_NAME": ""}):
            service = ShareService()
        snapshot = [
            {"id": "msg-0", "role": "user", "content": [{"type": "text", "text": "Hello"}], "createdAt": "2025-06-01T00:00:00Z"},
        ]
        dp = _RecordingDataPlane()
        mock_mgr = MagicMock()
        mock_mgr.memory_client.gmdp_client = dp
        mock_mgr.create_message = MagicMock(
            side_effect=lambda *a, **k: mock_mgr.memory_client.gmdp_client.create_event(memoryId="mem-123")
        )

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-east-1"}), \
             patch("bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager", return_value=mock_mgr), \
             patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"), \
             patch("strands.types.session.SessionMessage"):
            count = await service._copy_messages_to_memory("sess-new", "user-1", snapshot)

        assert count == 1
        assert dp.calls == [{"memoryId": "mem-123", "extractionMode": "SKIP"}]
