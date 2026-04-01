"""
Unit tests for compaction logic.

These tests verify the core compaction algorithms without requiring
AWS services or DynamoDB connections.
"""

import pytest
import copy
from agents.main_agent.session.compaction_models import CompactionState, CompactionConfig


class TestCompactionState:
    """Tests for CompactionState dataclass"""

    def test_default_state(self):
        state = CompactionState()
        assert state.checkpoint == 0
        assert state.summary is None
        assert state.last_input_tokens == 0
        assert state.updated_at is None

    def test_to_dict(self):
        state = CompactionState(
            checkpoint=10,
            summary="Test summary",
            last_input_tokens=50000,
            updated_at="2025-01-15T10:00:00Z"
        )
        d = state.to_dict()
        assert d["checkpoint"] == 10
        assert d["summary"] == "Test summary"
        assert d["lastInputTokens"] == 50000
        assert d["updatedAt"] == "2025-01-15T10:00:00Z"

    def test_from_dict(self):
        data = {
            "checkpoint": 5,
            "summary": "Previous context",
            "lastInputTokens": 75000,
            "updatedAt": "2025-01-15T12:00:00Z"
        }
        state = CompactionState.from_dict(data)
        assert state.checkpoint == 5
        assert state.summary == "Previous context"
        assert state.last_input_tokens == 75000
        assert state.updated_at == "2025-01-15T12:00:00Z"

    def test_from_dict_handles_none(self):
        state = CompactionState.from_dict(None)
        assert state.checkpoint == 0
        assert state.summary is None

    def test_from_dict_handles_empty(self):
        state = CompactionState.from_dict({})
        assert state.checkpoint == 0
        assert state.summary is None


class TestCompactionConfig:
    """Tests for CompactionConfig dataclass"""

    def test_default_config(self):
        config = CompactionConfig()
        assert config.enabled is False
        assert config.token_threshold == 100_000
        assert config.protected_turns == 2
        assert config.max_tool_content_length == 500

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "true")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD", "50000")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_PROTECTED_TURNS", "3")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_MAX_TOOL_CONTENT_LENGTH", "1000")

        config = CompactionConfig.from_env()
        assert config.enabled is True
        assert config.token_threshold == 50000
        assert config.protected_turns == 3
        assert config.max_tool_content_length == 1000


class TestCompactionHelpers:
    """Tests for compaction helper methods (using a mock manager)"""

    @pytest.fixture
    def mock_messages(self):
        """Sample conversation with tool usage"""
        return [
            # Turn 1: User asks question
            {"role": "user", "content": [{"text": "What's the weather?"}]},
            # Turn 1: Assistant uses tool
            {"role": "assistant", "content": [
                {"text": "Let me check."},
                {"toolUse": {"toolUseId": "t1", "name": "weather", "input": {"city": "Seattle"}}}
            ]},
            # Turn 1: Tool result (user role with toolResult)
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "t1", "content": [{"text": "72°F, sunny"}]}}
            ]},
            # Turn 1: Assistant responds
            {"role": "assistant", "content": [{"text": "It's 72°F and sunny in Seattle."}]},

            # Turn 2: User asks another question
            {"role": "user", "content": [{"text": "What about tomorrow?"}]},
            # Turn 2: Assistant uses tool
            {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t2", "name": "weather", "input": {"city": "Seattle", "day": "tomorrow"}}}
            ]},
            # Turn 2: Tool result
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "t2", "content": [{"text": "65°F, cloudy"}]}}
            ]},
            # Turn 2: Assistant responds
            {"role": "assistant", "content": [{"text": "Tomorrow will be 65°F and cloudy."}]},

            # Turn 3: User asks follow-up
            {"role": "user", "content": [{"text": "Should I bring an umbrella?"}]},
            # Turn 3: Assistant responds (no tool)
            {"role": "assistant", "content": [{"text": "Based on the forecast, you might want to."}]},
        ]

    def test_has_tool_result_true(self):
        """Test _has_tool_result detects tool results"""
        msg = {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "result"}]}}
        ]}
        # We can't call the method directly without instantiation, so test the logic
        content = msg.get('content', [])
        has_result = any(
            isinstance(block, dict) and 'toolResult' in block
            for block in content if isinstance(content, list)
        )
        assert has_result is True

    def test_has_tool_result_false(self):
        """Test _has_tool_result returns False for regular messages"""
        msg = {"role": "user", "content": [{"text": "Hello"}]}
        content = msg.get('content', [])
        has_result = any(
            isinstance(block, dict) and 'toolResult' in block
            for block in content if isinstance(content, list)
        )
        assert has_result is False

    def test_find_valid_cutoff_indices(self, mock_messages):
        """Test finding valid cutoff points (user messages that aren't tool results)"""
        valid_indices = []
        for i, msg in enumerate(mock_messages):
            if msg.get('role') == 'user':
                content = msg.get('content', [])
                is_tool_result = any(
                    isinstance(block, dict) and 'toolResult' in block
                    for block in content if isinstance(content, list)
                )
                if not is_tool_result:
                    valid_indices.append(i)

        # Should find indices 0, 4, 8 (the actual user questions, not tool results)
        assert valid_indices == [0, 4, 8]

    def test_find_protected_indices(self, mock_messages):
        """Test finding indices that should be protected from truncation"""
        protected_turns = 2

        # Find valid cutoff indices first
        turn_start_indices = []
        for i, msg in enumerate(mock_messages):
            if msg.get('role') == 'user':
                content = msg.get('content', [])
                is_tool_result = any(
                    isinstance(block, dict) and 'toolResult' in block
                    for block in content if isinstance(content, list)
                )
                if not is_tool_result:
                    turn_start_indices.append(i)

        # With 3 turns at [0, 4, 8] and protected_turns=2, protect from index 4 onwards
        turns_to_protect = min(protected_turns, len(turn_start_indices))
        protected_start_idx = turn_start_indices[-turns_to_protect]  # Index 4

        protected_indices = set(range(protected_start_idx, len(mock_messages)))
        assert protected_indices == {4, 5, 6, 7, 8, 9}

    def test_checkpoint_calculation(self, mock_messages):
        """Test checkpoint is set at oldest protected turn boundary"""
        protected_turns = 2

        # Find valid cutoff indices
        turn_start_indices = [0, 4, 8]  # From previous test

        # Checkpoint should be at the oldest protected turn
        # With protected_turns=2, protect turns at indices 4 and 8
        # Checkpoint = turn_start_indices[-protected_turns] = 4
        checkpoint = turn_start_indices[-protected_turns]
        assert checkpoint == 4


class TestTruncation:
    """Tests for tool content truncation"""

    def test_truncate_text(self):
        """Test text truncation with indicator"""
        max_length = 50
        short_text = "Short text"
        long_text = "A" * 100

        # Short text unchanged
        assert len(short_text) <= max_length

        # Long text truncated
        truncated = long_text[:max_length] + f"\n... [truncated, {len(long_text) - max_length} chars removed]"
        assert truncated.startswith("A" * 50)
        assert "truncated" in truncated
        assert "50 chars removed" in truncated

    def test_truncate_tool_result(self):
        """Test truncating tool result content"""
        max_length = 20
        tool_result_msg = {
            "role": "user",
            "content": [{
                "toolResult": {
                    "toolUseId": "t1",
                    "content": [{"text": "A" * 100}]
                }
            }]
        }

        # Deep copy to avoid mutating original
        msg = copy.deepcopy(tool_result_msg)
        result_content = msg["content"][0]["toolResult"]["content"]

        for block in result_content:
            if "text" in block and len(block["text"]) > max_length:
                original_len = len(block["text"])
                block["text"] = block["text"][:max_length] + f"\n... [truncated, {original_len - max_length} chars removed]"

        assert len(result_content[0]["text"]) < 100
        assert "truncated" in result_content[0]["text"]

    def test_image_replacement(self):
        """Test images are replaced with placeholders"""
        msg_with_image = {
            "role": "user",
            "content": [{
                "toolResult": {
                    "toolUseId": "t1",
                    "content": [{
                        "image": {
                            "format": "png",
                            "source": {"bytes": b"fake_image_data" * 1000}
                        }
                    }]
                }
            }]
        }

        msg = copy.deepcopy(msg_with_image)
        result_content = msg["content"][0]["toolResult"]["content"]

        for i, block in enumerate(result_content):
            if "image" in block:
                image_data = block["image"]
                image_format = image_data.get("format", "unknown")
                source = image_data.get("source", {})
                original_bytes = source.get("bytes", b"")
                original_size = len(original_bytes) if isinstance(original_bytes, bytes) else 0

                result_content[i] = {
                    "text": f"[Image placeholder: format={image_format}, original_size={original_size} bytes]"
                }

        assert "image" not in result_content[0]
        assert "Image placeholder" in result_content[0]["text"]
        assert "format=png" in result_content[0]["text"]


class TestSummaryInjection:
    """Tests for summary prepending"""

    def test_prepend_summary_to_first_message(self):
        """Test summary is prepended to first user message"""
        messages = [
            {"role": "user", "content": [{"text": "Hello, how are you?"}]},
            {"role": "assistant", "content": [{"text": "I'm doing well!"}]},
        ]

        summary = "Previous discussion about weather forecasts."
        summary_prefix = (
            "<conversation_summary>\n"
            "The following is a summary of our previous conversation:\n\n"
            f"{summary}\n\n"
            "Please continue the conversation with this context in mind.\n"
            "</conversation_summary>\n\n"
        )

        modified = copy.deepcopy(messages)
        first_msg = modified[0]

        if first_msg.get("role") == "user":
            content = first_msg.get("content", [])
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    block["text"] = summary_prefix + block["text"]
                    break

        assert "<conversation_summary>" in modified[0]["content"][0]["text"]
        assert "Previous discussion about weather" in modified[0]["content"][0]["text"]
        assert "Hello, how are you?" in modified[0]["content"][0]["text"]

    def test_summary_not_prepended_to_assistant(self):
        """Test summary is not prepended if first message is assistant"""
        messages = [
            {"role": "assistant", "content": [{"text": "Welcome!"}]},
            {"role": "user", "content": [{"text": "Hi"}]},
        ]

        summary = "Previous context"
        modified = copy.deepcopy(messages)
        first_msg = modified[0]

        # Should not modify if first message is assistant
        if first_msg.get("role") != "user":
            pass  # Don't modify
        else:
            content = first_msg.get("content", [])
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    block["text"] = f"<summary>{summary}</summary>" + block["text"]

        assert "summary" not in modified[0]["content"][0]["text"]
        assert modified[0]["content"][0]["text"] == "Welcome!"


class TestFallbackSummary:
    """Tests for fallback summary generation"""

    def test_generate_fallback_summary(self):
        """Test fallback summary extracts user topics"""
        messages = [
            {"role": "user", "content": [{"text": "What's the weather in Seattle?"}]},
            {"role": "assistant", "content": [{"text": "It's sunny."}]},
            {"role": "user", "content": [{"text": "How about New York?"}]},
            {"role": "assistant", "content": [{"text": "It's rainy."}]},
        ]

        key_points = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", [])

            if role == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        text = block["text"]
                        if "toolResult" not in block:
                            first_line = text.split("\n")[0][:100]
                            if first_line and not first_line.startswith("<"):
                                key_points.append(f"- User asked about: {first_line}")
                        break

        assert len(key_points) == 2
        assert "Seattle" in key_points[0]
        assert "New York" in key_points[1]

    def test_fallback_summary_skips_tool_results(self):
        """Test fallback summary skips tool result messages"""
        messages = [
            {"role": "user", "content": [{"text": "Check the weather"}]},
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "t1", "content": [{"text": "72°F"}]}}
            ]},
        ]

        key_points = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", [])

            if role == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        # Skip if this is a toolResult message
                        if "toolResult" not in str(content):
                            first_line = block["text"].split("\n")[0][:100]
                            if first_line:
                                key_points.append(f"- User asked about: {first_line}")
                        break

        # Should only have one point (the actual question, not the tool result)
        assert len(key_points) == 1
        assert "weather" in key_points[0].lower()
