"""
Tests for TurnBasedSessionManager — the core runtime loop for agent sessions.

Covers: initialization, message helpers, truncation (Stage 1), summary injection,
DynamoDB state persistence, LTM retrieval, initialization flow, post-turn update
(Stage 2), session interface, and property-based tests.

Architecture: TurnBasedSessionManager inherits from AgentCoreMemorySessionManager.
Tests mock the parent's __init__ to avoid AWS calls and set up required attributes
via the make_session_manager fixture in conftest.py.
"""

import copy
import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState

from .conftest import (
    TABLE_NAME,
    REGION,
    TEST_SESSION_ID,
    TEST_USER_ID,
    make_user_message,
    make_assistant_message,
    make_tool_use_message,
    make_tool_result_message,
    make_tool_result_json_message,
    make_tool_result_image_message,
    make_image_message,
    make_tool_use_string_input_message,
    make_conversation,
    seed_session_record,
)


# ===========================================================================
# Task 1 — Smoke test: fixtures instantiate correctly
# ===========================================================================

class TestFixturesSmoke:

    def test_make_session_manager_no_compaction(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr.message_count == 0
        assert mgr.cancelled is False
        assert mgr.compaction_config is None

    def test_make_session_manager_with_compaction(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        assert mgr.compaction_config.enabled is True
        assert mgr.compaction_config.token_threshold == 1000

    def test_message_builders(self):
        assert make_user_message("hi")["role"] == "user"
        assert make_assistant_message("yo")["role"] == "assistant"
        assert "toolUse" in make_tool_use_message("t1", "calc", {"x": 1})["content"][0]
        assert "toolResult" in make_tool_result_message("t1", "ok")["content"][0]
        assert "json" in make_tool_result_json_message("t1", {"a": 1})["content"][0]["toolResult"]["content"][0]
        assert "image" in make_tool_result_image_message("t1")["content"][0]["toolResult"]["content"][0]
        assert "image" in make_image_message()["content"][0]
        conv = make_conversation(3)
        assert len(conv) == 6

    def test_has_required_parent_attributes(self, make_session_manager):
        """Verify the mock parent __init__ sets up all required attributes."""
        mgr = make_session_manager()
        assert hasattr(mgr, "config")
        assert hasattr(mgr, "memory_client")
        assert hasattr(mgr, "session_id")
        assert hasattr(mgr, "_latest_agent_message")
        assert hasattr(mgr, "_is_new_session")
        assert hasattr(mgr, "has_existing_agent")
        assert mgr.session_id == TEST_SESSION_ID


# ===========================================================================
# Task 2 — Message processing helpers
# ===========================================================================

class TestHasToolResult:

    def test_message_with_tool_result(self, make_session_manager):
        mgr = make_session_manager()
        msg = make_tool_result_message("t1", "result text")
        assert mgr._has_tool_result(msg) is True

    def test_message_without_tool_result(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr._has_tool_result(make_user_message("hello")) is False

    def test_empty_content(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr._has_tool_result({"role": "user", "content": []}) is False

    def test_non_list_content(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr._has_tool_result({"role": "user", "content": "string"}) is False


class TestFindValidCutoffIndices:

    def test_simple_conversation(self, make_session_manager):
        mgr = make_session_manager()
        messages = make_conversation(3)  # u, a, u, a, u, a
        indices = mgr._find_valid_cutoff_indices(messages)
        assert indices == [0, 2, 4]

    def test_tool_results_excluded(self, make_session_manager):
        mgr = make_session_manager()
        messages = [
            make_user_message("q1"),
            make_assistant_message("a1"),
            make_tool_result_message("t1", "result"),  # user role but tool result
            make_assistant_message("a2"),
            make_user_message("q2"),
        ]
        indices = mgr._find_valid_cutoff_indices(messages)
        assert indices == [0, 4]

    def test_empty_messages(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr._find_valid_cutoff_indices([]) == []

    def test_only_assistant_messages(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_assistant_message("a1"), make_assistant_message("a2")]
        assert mgr._find_valid_cutoff_indices(messages) == []


class TestFindProtectedIndices:

    def test_protect_last_2_turns(self, make_session_manager):
        mgr = make_session_manager()
        messages = make_conversation(4)  # indices 0-7, turns at 0,2,4,6
        protected = mgr._find_protected_indices(messages, 2)
        # Last 2 turn starts are at index 4 and 6, so protect 4..7
        assert protected == set(range(4, 8))

    def test_zero_protected_turns(self, make_session_manager):
        mgr = make_session_manager()
        messages = make_conversation(3)
        assert mgr._find_protected_indices(messages, 0) == set()

    def test_more_protected_than_available(self, make_session_manager):
        mgr = make_session_manager()
        messages = make_conversation(2)  # 4 messages, 2 turns
        protected = mgr._find_protected_indices(messages, 10)
        # All messages protected since we only have 2 turns
        assert protected == set(range(0, 4))

    def test_no_valid_cutoffs(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_assistant_message("a1")]
        assert mgr._find_protected_indices(messages, 2) == set()


# ===========================================================================
# Task 3 — Tool content truncation (Stage 1)
# ===========================================================================

class TestTruncateToolContents:

    def test_image_replacement_in_message(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        messages = [make_image_message(b"x" * 100, "png")]
        result, count, saved = mgr._truncate_tool_contents(messages)
        assert count == 1
        assert "Image placeholder" in result[0]["content"][0]["text"]
        assert "image" not in result[0]["content"][0]

    def test_tool_use_dict_input_truncation(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        big_input = {"data": "x" * 200}
        messages = [make_tool_use_message("t1", "calc", big_input)]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 1
        assert "_truncated" in result[0]["content"][0]["toolUse"]["input"]

    def test_tool_use_dict_input_under_threshold(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        small_input = {"x": 1}
        messages = [make_tool_use_message("t1", "calc", small_input)]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 0
        assert result[0]["content"][0]["toolUse"]["input"] == small_input

    def test_tool_use_string_input_truncation(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        messages = [make_tool_use_string_input_message("t1", "run", "y" * 200)]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 1
        assert "truncated" in result[0]["content"][0]["toolUse"]["input"]

    def test_tool_result_text_truncation(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        messages = [make_tool_result_message("t1", "z" * 200)]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 1
        text = result[0]["content"][0]["toolResult"]["content"][0]["text"]
        assert "truncated" in text

    def test_tool_result_json_truncation(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        big_json = {"key": "v" * 200}
        messages = [make_tool_result_json_message("t1", big_json)]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 1
        block = result[0]["content"][0]["toolResult"]["content"][0]
        assert "json" not in block
        assert "truncated" in block["text"]

    def test_tool_result_image_replacement(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        messages = [make_tool_result_image_message("t1", b"img" * 50)]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 1
        block = result[0]["content"][0]["toolResult"]["content"][0]
        assert "Image placeholder" in block["text"]

    def test_protected_indices_skipped(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        messages = [
            make_tool_result_message("t1", "a" * 200),
            make_tool_result_message("t2", "b" * 200),
        ]
        result, count, _ = mgr._truncate_tool_contents(messages, protected_indices={1})
        assert count == 1  # only index 0 truncated
        # Index 1 should be unchanged
        assert result[1]["content"][0]["toolResult"]["content"][0]["text"] == "b" * 200

    def test_mixed_content_types(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        messages = [
            make_image_message(b"x" * 100),
            make_tool_use_message("t1", "calc", {"data": "d" * 200}),
            make_tool_result_message("t2", "r" * 200),
            make_tool_result_json_message("t3", {"big": "j" * 200}),
            make_tool_result_image_message("t4", b"i" * 100),
        ]
        result, count, _ = mgr._truncate_tool_contents(messages)
        assert count == 5

    def test_compaction_disabled_returns_unchanged(self, make_session_manager):
        mgr = make_session_manager()  # no compaction_config
        messages = [make_tool_result_message("t1", "a" * 200)]
        result, count, saved = mgr._truncate_tool_contents(messages)
        assert count == 0
        assert saved == 0

    def test_does_not_mutate_original(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        original = [make_tool_result_message("t1", "a" * 200)]
        original_copy = copy.deepcopy(original)
        mgr._truncate_tool_contents(original)
        assert original == original_copy


# ===========================================================================
# Task 4 — Summary injection
# ===========================================================================

class TestPrependSummary:

    def test_prepends_to_text_block(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_user_message("hello")]
        result = mgr._prepend_summary_to_first_message(messages, "summary text")
        text = result[0]["content"][0]["text"]
        assert "summary text" in text
        assert "hello" in text
        assert text.index("summary text") < text.index("hello")

    def test_inserts_text_block_when_missing(self, make_session_manager):
        mgr = make_session_manager()
        messages = [{"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b"x"}}}]}]
        result = mgr._prepend_summary_to_first_message(messages, "summary")
        assert result[0]["content"][0]["text"].startswith("<conversation_summary>")

    def test_non_user_first_message_unchanged(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_assistant_message("hi")]
        result = mgr._prepend_summary_to_first_message(messages, "summary")
        assert result == messages

    def test_empty_messages_unchanged(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr._prepend_summary_to_first_message([], "summary") == []

    def test_empty_summary_unchanged(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_user_message("hello")]
        result = mgr._prepend_summary_to_first_message(messages, "")
        assert result == messages

    def test_does_not_mutate_original(self, make_session_manager):
        mgr = make_session_manager()
        original = [make_user_message("hello")]
        original_copy = copy.deepcopy(original)
        mgr._prepend_summary_to_first_message(original, "summary")
        assert original == original_copy


# ===========================================================================
# Task 5 — DynamoDB state persistence with moto
# ===========================================================================

class TestGetDynamoDBTable:

    def test_lazy_init_with_env_var(self, make_session_manager, compaction_config, dynamodb_sessions_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE_NAME)
        mgr = make_session_manager(compaction_config=compaction_config)
        table = mgr._get_dynamodb_table()
        assert table is not None

    def test_returns_none_without_env_var(self, make_session_manager, compaction_config, monkeypatch):
        monkeypatch.delenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", raising=False)
        mgr = make_session_manager(compaction_config=compaction_config)
        # Reset class-level cache
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager
        TurnBasedSessionManager._dynamodb_table = None
        TurnBasedSessionManager._dynamodb_table_name = None
        table = mgr._get_dynamodb_table()
        assert table is None


class TestGetSessionViaGSI:

    def test_finds_session(self, make_session_manager, compaction_config, dynamodb_sessions_table):
        mgr = make_session_manager(compaction_config=compaction_config)
        seed_session_record(dynamodb_sessions_table, TEST_SESSION_ID, TEST_USER_ID)
        # Inject the moto table directly
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager
        TurnBasedSessionManager._dynamodb_table = dynamodb_sessions_table
        item = mgr._get_session_via_gsi(dynamodb_sessions_table)
        assert item is not None
        assert item["sessionId"] == TEST_SESSION_ID

    def test_returns_none_when_missing(self, make_session_manager, compaction_config, dynamodb_sessions_table):
        mgr = make_session_manager(compaction_config=compaction_config)
        item = mgr._get_session_via_gsi(dynamodb_sessions_table)
        assert item is None

    def test_rejects_wrong_user(self, make_session_manager, compaction_config, dynamodb_sessions_table):
        mgr = make_session_manager(compaction_config=compaction_config, user_id="wrong-user")
        seed_session_record(dynamodb_sessions_table, TEST_SESSION_ID, TEST_USER_ID)
        item = mgr._get_session_via_gsi(dynamodb_sessions_table)
        assert item is None


class TestCompactionStatePersistence:

    def test_load_returns_default_when_no_session(self, make_session_manager, compaction_config, dynamodb_sessions_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE_NAME)
        mgr = make_session_manager(compaction_config=compaction_config)
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager
        TurnBasedSessionManager._dynamodb_table = dynamodb_sessions_table
        state = mgr._load_compaction_state()
        assert state.checkpoint == 0
        assert state.summary is None

    def test_load_returns_default_when_no_compaction_attr(self, make_session_manager, compaction_config, dynamodb_sessions_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE_NAME)
        mgr = make_session_manager(compaction_config=compaction_config)
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager
        TurnBasedSessionManager._dynamodb_table = dynamodb_sessions_table
        seed_session_record(dynamodb_sessions_table, TEST_SESSION_ID, TEST_USER_ID)
        state = mgr._load_compaction_state()
        assert state.checkpoint == 0

    def test_load_existing_state(self, make_session_manager, compaction_config, dynamodb_sessions_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE_NAME)
        mgr = make_session_manager(compaction_config=compaction_config)
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager
        TurnBasedSessionManager._dynamodb_table = dynamodb_sessions_table
        seed_session_record(
            dynamodb_sessions_table, TEST_SESSION_ID, TEST_USER_ID,
            compaction={"checkpoint": 5, "summary": "test summary", "lastInputTokens": 999},
        )
        state = mgr._load_compaction_state()
        assert state.checkpoint == 5
        assert state.summary == "test summary"
        assert state.last_input_tokens == 999

    def test_save_and_load_roundtrip(self, make_session_manager, compaction_config, dynamodb_sessions_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE_NAME)
        mgr = make_session_manager(compaction_config=compaction_config)
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager
        TurnBasedSessionManager._dynamodb_table = dynamodb_sessions_table
        seed_session_record(dynamodb_sessions_table, TEST_SESSION_ID, TEST_USER_ID)

        state = CompactionState(checkpoint=10, summary="saved summary", last_input_tokens=5000)
        mgr._save_compaction_state(state)

        loaded = mgr._load_compaction_state()
        assert loaded.checkpoint == 10
        assert loaded.summary == "saved summary"
        assert loaded.last_input_tokens == 5000
        assert loaded.updated_at is not None

    def test_load_returns_default_when_no_user_id(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config, user_id=None)
        state = mgr._load_compaction_state()
        assert state.checkpoint == 0

    def test_load_returns_default_when_compaction_disabled(self, make_session_manager, compaction_config_disabled):
        mgr = make_session_manager(compaction_config=compaction_config_disabled)
        state = mgr._load_compaction_state()
        assert state.checkpoint == 0

    def test_save_noop_when_compaction_disabled(self, make_session_manager, compaction_config_disabled):
        mgr = make_session_manager(compaction_config=compaction_config_disabled)
        # Should not raise
        mgr._save_compaction_state(CompactionState(checkpoint=5))


# ===========================================================================
# Task 6 — LTM summary retrieval
# ===========================================================================

class TestGetSummarizationStrategyId:

    def test_returns_cached_id(self, make_session_manager):
        mgr = make_session_manager(summarization_strategy_id="strat-123")
        assert mgr._get_summarization_strategy_id() == "strat-123"

    def test_discovers_from_memory_config(self, make_session_manager):
        mgr = make_session_manager()
        mgr.memory_client.gmcp_client.get_memory.return_value = {
            "memory": {
                "strategies": [
                    {"type": "EXTRACTION", "strategyId": "ext-1"},
                    {"type": "SUMMARIZATION", "strategyId": "sum-1"},
                ]
            }
        }
        assert mgr._get_summarization_strategy_id() == "sum-1"
        # Verify caching
        assert mgr.summarization_strategy_id == "sum-1"

    def test_returns_none_when_no_summarization_strategy(self, make_session_manager):
        mgr = make_session_manager()
        mgr.memory_client.gmcp_client.get_memory.return_value = {
            "memory": {"strategies": [{"type": "EXTRACTION", "strategyId": "ext-1"}]}
        }
        assert mgr._get_summarization_strategy_id() is None

    def test_returns_none_on_error(self, make_session_manager):
        mgr = make_session_manager()
        mgr.memory_client.gmcp_client.get_memory.side_effect = Exception("fail")
        assert mgr._get_summarization_strategy_id() is None


class TestRetrieveSessionSummaries:

    def test_returns_empty_when_no_strategy(self, make_session_manager):
        mgr = make_session_manager()
        mgr.memory_client.gmcp_client.get_memory.return_value = {
            "memory": {"strategies": []}
        }
        assert mgr._retrieve_session_summaries() == []

    @patch("boto3.client")
    def test_parses_memory_records(self, mock_client_fn, make_session_manager):
        mgr = make_session_manager(summarization_strategy_id="strat-1")
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.list_memory_records.return_value = {
            "memoryRecordSummaries": [
                {"content": {"text": "Summary point 1"}},
                {"content": {"text": "Summary point 2"}},
                {"content": {"text": "  "}},  # blank — should be skipped
            ]
        }
        summaries = mgr._retrieve_session_summaries()
        assert summaries == ["Summary point 1", "Summary point 2"]

    @patch("boto3.client")
    def test_returns_empty_on_error(self, mock_client_fn, make_session_manager):
        mgr = make_session_manager(summarization_strategy_id="strat-1")
        mock_client_fn.side_effect = Exception("boom")
        assert mgr._retrieve_session_summaries() == []


class TestGenerateFallbackSummary:

    def test_extracts_user_messages(self, make_session_manager):
        mgr = make_session_manager()
        messages = [
            make_user_message("How do I deploy?"),
            make_assistant_message("Use CDK"),
            make_user_message("What about testing?"),
        ]
        summary = mgr._generate_fallback_summary(messages)
        assert "deploy" in summary.lower()
        assert "testing" in summary.lower()

    def test_skips_tool_results(self, make_session_manager):
        mgr = make_session_manager()
        messages = [
            make_user_message("question"),
            make_tool_result_message("t1", "tool output"),
        ]
        summary = mgr._generate_fallback_summary(messages)
        assert "question" in summary.lower()
        # tool result user message has toolResult in block, not text — so it's skipped
        assert "tool output" not in (summary or "")

    def test_skips_xml_prefixed_lines(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_user_message("<system>ignore this")]
        summary = mgr._generate_fallback_summary(messages)
        assert summary is None

    def test_empty_messages_returns_none(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr._generate_fallback_summary([]) is None

    def test_limits_to_15_points(self, make_session_manager):
        mgr = make_session_manager()
        messages = [make_user_message(f"Topic {i}") for i in range(20)]
        summary = mgr._generate_fallback_summary(messages)
        # Fallback summary limits key_points to last 15
        lines = [line for line in summary.split("\n") if line.startswith("- User:")]
        assert len(lines) == 15


# ===========================================================================
# Task 7 — Initialization flow (SDK override)
# ===========================================================================

class TestInitialize:
    """Test the initialize() override which handles compaction on session restore."""

    def _make_mock_agent(self, messages=None):
        """Create a mock agent for initialize() tests."""
        agent = MagicMock()
        agent.agent_id = "default"
        agent.messages = messages or []
        agent.state = MagicMock()
        agent.conversation_manager.restore_from_session.return_value = []
        agent.conversation_manager.removed_message_count = 0
        return agent

    def _make_mock_session_agent(self):
        """Create a mock SessionAgent for the existing-agent path."""
        session_agent = MagicMock()
        session_agent.state = {}
        session_agent.conversation_manager_state = {}
        return session_agent

    def _make_mock_session_messages(self, messages):
        """Convert raw message dicts to mock SessionMessage objects."""
        session_messages = []
        for msg in messages:
            sm = MagicMock()
            sm.to_message.return_value = msg
            session_messages.append(sm)
        return session_messages

    def test_new_agent_creates_session(self, make_session_manager, compaction_config):
        """When session_agent is None, should create agent and set empty compaction state."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.read_agent = MagicMock(return_value=None)

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        mgr.create_agent.assert_called_once()
        assert mgr.compaction_state is not None
        assert mgr.compaction_state.checkpoint == 0
        assert mgr._valid_cutoff_indices == []

    def test_existing_agent_no_compaction_loads_all_messages(self, make_session_manager):
        """Without compaction config, should load all messages from session."""
        mgr = make_session_manager()  # no compaction_config
        messages = make_conversation(3)  # 6 messages
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        assert len(agent.messages) == 6
        assert mgr.message_count == 6

    def test_existing_agent_compaction_no_checkpoint(self, make_session_manager, compaction_config):
        """Compaction enabled with checkpoint=0 should load all messages with truncation."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = MagicMock(return_value=CompactionState())

        messages = [
            make_user_message("q1"),
            make_assistant_message("a1"),
            make_user_message("q2"),
            make_tool_result_message("t1", "x" * 200),  # will be truncated
        ]
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        # All 4 messages kept (checkpoint=0), but truncation applied
        assert len(agent.messages) == 4
        # Valid cutoffs cached for user text messages (indices 0, 2)
        assert mgr._valid_cutoff_indices == [0, 2]

    def test_existing_agent_compaction_with_checkpoint_slices_messages(self, make_session_manager, compaction_config):
        """Checkpoint > 0 should skip old messages and prepend summary."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = MagicMock(
            return_value=CompactionState(checkpoint=4, summary="old context")
        )

        messages = make_conversation(4)  # 8 messages
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        # Should slice from index 4: 4 messages remain
        assert len(agent.messages) == 4
        # Summary prepended to first user message
        first_text = agent.messages[0]["content"][0]["text"]
        assert "old context" in first_text
        assert "<conversation_summary>" in first_text

    def test_existing_agent_compaction_checkpoint_plus_truncation(self, make_session_manager, compaction_config):
        """Both checkpoint slicing and truncation should apply."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = MagicMock(
            return_value=CompactionState(checkpoint=2)
        )

        messages = [
            make_user_message("old1"),
            make_assistant_message("old2"),
            make_user_message("new1"),
            make_tool_result_message("t1", "r" * 200),  # truncatable
        ]
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        # Sliced from index 2: 2 messages remain
        assert len(agent.messages) == 2

    def test_duplicate_agent_id_raises(self, make_session_manager):
        """Second initialize with same agent_id should raise SessionException."""
        from strands.types.exceptions import SessionException

        mgr = make_session_manager()
        mgr.read_agent = MagicMock(return_value=None)
        agent = self._make_mock_agent()

        mgr.initialize(agent)

        with pytest.raises(SessionException, match="unique"):
            mgr.initialize(agent)

    def test_sets_has_existing_agent_flag(self, make_session_manager):
        """initialize() should mark has_existing_agent = True."""
        mgr = make_session_manager()
        mgr.read_agent = MagicMock(return_value=None)
        agent = self._make_mock_agent()

        assert mgr.has_existing_agent is False
        mgr.initialize(agent)
        assert mgr.has_existing_agent is True

    def test_sets_is_new_session_false(self, make_session_manager):
        """initialize() should mark _is_new_session = False."""
        mgr = make_session_manager()
        mgr.read_agent = MagicMock(return_value=None)
        agent = self._make_mock_agent()

        mgr.initialize(agent)
        assert mgr._is_new_session is False

    def test_caches_all_messages_for_summary(self, make_session_manager, compaction_config):
        """Compaction path should cache shallow copies of all messages for summary generation."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = MagicMock(return_value=CompactionState())

        messages = make_conversation(3)
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        # Should have cached 6 messages for summary generation
        assert len(mgr._all_messages_for_summary) == 6


# ===========================================================================
# Task 8 — Post-turn update (Stage 2, async)
# ===========================================================================

class TestUpdateAfterTurn:

    @pytest.mark.asyncio
    async def test_noop_when_compaction_disabled(self, make_session_manager):
        mgr = make_session_manager()
        await mgr.update_after_turn(50000)
        # No compaction state should be set
        assert mgr.compaction_state is None

    @pytest.mark.asyncio
    async def test_below_threshold_saves_token_count(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        await mgr.update_after_turn(500)  # below 1000 threshold
        assert mgr.compaction_state.last_input_tokens == 500
        mgr._save_compaction_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_above_threshold_creates_checkpoint(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=[])

        # Simulate 5 turns cached during initialize
        messages = make_conversation(5)
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]  # 5 user message indices
        mgr._all_messages_for_summary = messages

        await mgr.update_after_turn(2000)  # above 1000 threshold

        # With 5 turns and protected_turns=3, checkpoint should be at turn 2 start (index 4)
        assert mgr.compaction_state.checkpoint == 4
        assert mgr.compaction_state.last_input_tokens == 2000

    @pytest.mark.asyncio
    async def test_not_enough_turns_keeps_all(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()

        # Only 3 turns = protected_turns, so no compaction possible
        mgr._valid_cutoff_indices = [0, 2, 4]

        await mgr.update_after_turn(2000)
        assert mgr.compaction_state.checkpoint == 0

    @pytest.mark.asyncio
    async def test_empty_cutoff_indices_skips_checkpoint(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()

        mgr._valid_cutoff_indices = []  # no valid cutoffs (e.g., new session)

        await mgr.update_after_turn(2000)
        assert mgr.compaction_state.checkpoint == 0
        mgr._save_compaction_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_checkpoint_unchanged_no_update(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState(checkpoint=4)
        mgr._save_compaction_state = MagicMock()

        # 5 turns — checkpoint would be at index 4 again, same as current
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]

        await mgr.update_after_turn(2000)
        # Checkpoint should remain 4 (no update)
        assert mgr.compaction_state.checkpoint == 4

    @pytest.mark.asyncio
    async def test_checkpoint_advances_when_more_turns(self, make_session_manager, compaction_config):
        """New turns should advance the checkpoint."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState(checkpoint=4)
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=["Updated summary"])

        # 6 turns now — checkpoint should advance
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8, 10]
        mgr._all_messages_for_summary = make_conversation(6)

        await mgr.update_after_turn(2000)
        assert mgr.compaction_state.checkpoint == 6  # [-3] = index 6

    @pytest.mark.asyncio
    async def test_uses_ltm_summaries_when_available(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=["LTM summary 1", "LTM summary 2"])

        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
        mgr._all_messages_for_summary = make_conversation(5)

        await mgr.update_after_turn(2000)
        assert "LTM summary 1" in mgr.compaction_state.summary
        assert "LTM summary 2" in mgr.compaction_state.summary

    @pytest.mark.asyncio
    async def test_falls_back_to_generated_summary(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=[])

        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
        mgr._all_messages_for_summary = make_conversation(5)

        await mgr.update_after_turn(2000)
        assert mgr.compaction_state.summary is not None
        assert "Previous conversation" in mgr.compaction_state.summary

    @pytest.mark.asyncio
    async def test_initializes_compaction_state_if_none(self, make_session_manager, compaction_config):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.compaction_state = None
        mgr._save_compaction_state = MagicMock()

        await mgr.update_after_turn(500)
        assert mgr.compaction_state is not None
        assert mgr.compaction_state.last_input_tokens == 500


# ===========================================================================
# Task 9 — Session interface
# ===========================================================================

class TestFlush:

    def test_returns_last_index_when_messages_exist(self, make_session_manager):
        mgr = make_session_manager()
        mgr.message_count = 5
        assert mgr.flush() == 4

    def test_returns_none_when_empty(self, make_session_manager):
        mgr = make_session_manager()
        assert mgr.flush() is None


class TestAppendMessage:

    def test_increments_message_count(self, make_session_manager):
        mgr = make_session_manager()
        agent = MagicMock()
        msg = {"role": "user", "content": [{"text": "hi"}]}
        with patch(
            "agents.main_agent.session.turn_based_session_manager.AgentCoreMemorySessionManager.append_message"
        ):
            mgr.append_message(msg, agent)
        assert mgr.message_count == 1

    def test_cancelled_skips(self, make_session_manager):
        mgr = make_session_manager()
        mgr.cancelled = True
        agent = MagicMock()
        msg = {"role": "user", "content": [{"text": "hi"}]}
        mgr.append_message(msg, agent)
        assert mgr.message_count == 0

    def test_increments_multiple_times(self, make_session_manager):
        mgr = make_session_manager()
        agent = MagicMock()
        with patch(
            "agents.main_agent.session.turn_based_session_manager.AgentCoreMemorySessionManager.append_message"
        ):
            for i in range(3):
                mgr.append_message({"role": "user", "content": [{"text": f"m{i}"}]}, agent)
        assert mgr.message_count == 3

    def test_filters_empty_text_blocks(self, make_session_manager):
        mgr = make_session_manager()
        agent = MagicMock()
        msg = {"role": "user", "content": [{"text": "  "}]}  # whitespace-only
        mgr.append_message(msg, agent)
        # Empty after filtering — should not increment
        assert mgr.message_count == 0

    def test_keeps_tool_use_blocks(self, make_session_manager):
        mgr = make_session_manager()
        agent = MagicMock()
        msg = make_tool_use_message("t1", "calc", {"x": 1})
        with patch(
            "agents.main_agent.session.turn_based_session_manager.AgentCoreMemorySessionManager.append_message"
        ):
            mgr.append_message(msg, agent)
        assert mgr.message_count == 1


class TestFilterEmptyText:

    def test_removes_empty_text_blocks(self, make_session_manager):
        mgr = make_session_manager()
        msg = {"role": "user", "content": [{"text": ""}, {"text": "keep"}]}
        result = mgr._filter_empty_text(msg)
        assert len(result["content"]) == 1
        assert result["content"][0]["text"] == "keep"

    def test_removes_whitespace_only_text(self, make_session_manager):
        mgr = make_session_manager()
        msg = {"role": "user", "content": [{"text": "   \n  "}]}
        result = mgr._filter_empty_text(msg)
        assert len(result["content"]) == 0

    def test_keeps_non_text_blocks(self, make_session_manager):
        mgr = make_session_manager()
        msg = {"role": "assistant", "content": [
            {"text": ""},
            {"toolUse": {"toolUseId": "t1", "name": "calc", "input": {}}},
        ]}
        result = mgr._filter_empty_text(msg)
        assert len(result["content"]) == 1
        assert "toolUse" in result["content"][0]

    def test_no_content_key_unchanged(self, make_session_manager):
        mgr = make_session_manager()
        msg = {"role": "user"}
        assert mgr._filter_empty_text(msg) == msg

    def test_non_list_content_unchanged(self, make_session_manager):
        mgr = make_session_manager()
        msg = {"role": "user", "content": "string"}
        assert mgr._filter_empty_text(msg) == msg


# ===========================================================================
# Task 10 — End-to-end compaction lifecycle
# ===========================================================================

class TestCompactionLifecycle:
    """Integration-style tests that exercise the full init → stream → update cycle."""

    def _make_mock_agent(self, messages=None):
        agent = MagicMock()
        agent.agent_id = "default"
        agent.messages = messages or []
        agent.state = MagicMock()
        agent.conversation_manager.restore_from_session.return_value = []
        agent.conversation_manager.removed_message_count = 0
        return agent

    def _make_mock_session_messages(self, messages):
        session_messages = []
        for msg in messages:
            sm = MagicMock()
            sm.to_message.return_value = msg
            session_messages.append(sm)
        return session_messages

    def _make_mock_session_agent(self):
        session_agent = MagicMock()
        session_agent.state = {}
        session_agent.conversation_manager_state = {}
        return session_agent

    @pytest.mark.asyncio
    async def test_turn1_new_session_no_checkpoint(self, make_session_manager, compaction_config):
        """Turn 1: New session — no messages, no checkpoint."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr.read_agent = MagicMock(return_value=None)

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        assert mgr._valid_cutoff_indices == []
        assert mgr.compaction_state.checkpoint == 0

        # Simulate under-threshold turn
        mgr._save_compaction_state = MagicMock()
        await mgr.update_after_turn(500)
        assert mgr.compaction_state.checkpoint == 0

    @pytest.mark.asyncio
    async def test_turn6_creates_checkpoint(self, make_session_manager, compaction_config):
        """Turn 6: Enough turns to create a checkpoint (>3 protected)."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = MagicMock(return_value=CompactionState())
        mgr._retrieve_session_summaries = MagicMock(return_value=["Session summary"])

        # Simulate 5 prior turns of history
        messages = make_conversation(5)  # 10 messages
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        # Verify cutoff indices cached
        assert mgr._valid_cutoff_indices == [0, 2, 4, 6, 8]

        # Simulate above-threshold turn
        mgr._save_compaction_state = MagicMock()
        await mgr.update_after_turn(2000)

        # 5 cutoffs, protected=3 → checkpoint at [-3] = index 4
        assert mgr.compaction_state.checkpoint == 4
        assert "Session summary" in mgr.compaction_state.summary

    @pytest.mark.asyncio
    async def test_turn5_applies_checkpoint(self, make_session_manager, compaction_config):
        """Turn 5: Checkpoint was set — messages should be sliced."""
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = MagicMock(
            return_value=CompactionState(checkpoint=4, summary="Prior discussion summary")
        )

        messages = make_conversation(5)  # 10 messages
        session_agent = self._make_mock_session_agent()
        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=self._make_mock_session_messages(messages))
        mgr._is_new_session = False

        agent = self._make_mock_agent()
        mgr.initialize(agent)

        # Messages 0-3 skipped (checkpoint=4), messages 4-9 kept = 6 messages
        assert len(agent.messages) == 6
        # Summary prepended to first message
        first_text = agent.messages[0]["content"][0]["text"]
        assert "Prior discussion summary" in first_text
        # original=10, final=6 (compaction working!)
        assert mgr._total_message_count_at_init == 10


# ===========================================================================
# Task 11 — Property-based tests
# ===========================================================================

try:
    from hypothesis import given, settings, HealthCheck, strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:
    _HAS_HYPOTHESIS = False

if _HAS_HYPOTHESIS:

    # Strategy: generate a list of messages with random content types
    def _st_message_list():
        """Strategy that generates a list of messages with various content types."""
        text_msg = st.builds(
            lambda t: make_user_message(t),
            st.text(min_size=1, max_size=300),
        )
        assistant_msg = st.builds(
            lambda t: make_assistant_message(t),
            st.text(min_size=1, max_size=300),
        )
        tool_result_msg = st.builds(
            lambda t: make_tool_result_message("t1", t),
            st.text(min_size=1, max_size=300),
        )
        image_msg = st.just(make_image_message(b"x" * 50))
        return st.lists(
            st.one_of(text_msg, assistant_msg, tool_result_msg, image_msg),
            min_size=1,
            max_size=20,
        )

    class TestTruncationProperties:

        @given(messages=_st_message_list())
        @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
        def test_truncation_never_increases_message_count(self, messages, make_session_manager, compaction_config):
            mgr = make_session_manager(compaction_config=compaction_config)
            result, _, _ = mgr._truncate_tool_contents(messages)
            assert len(result) == len(messages)

        @given(messages=_st_message_list())
        @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
        def test_protected_indices_never_modified(self, messages, make_session_manager, compaction_config):
            mgr = make_session_manager(compaction_config=compaction_config)
            protected = set(range(len(messages)))  # protect everything
            original = copy.deepcopy(messages)
            result, count, _ = mgr._truncate_tool_contents(messages, protected_indices=protected)
            assert count == 0
            assert result == original
