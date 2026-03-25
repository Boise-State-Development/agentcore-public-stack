"""
Tests for CompactionState and CompactionConfig dataclasses.

Validates: Requirements 16.1–16.4, 16.6–16.7
"""

import pytest
from agents.main_agent.session.compaction_models import CompactionState, CompactionConfig


# ---------------------------------------------------------------------------
# CompactionState defaults (Req 16.1)
# ---------------------------------------------------------------------------

class TestCompactionStateDefaults:
    def test_default_checkpoint_is_zero(self):
        state = CompactionState()
        assert state.checkpoint == 0

    def test_default_summary_is_none(self):
        state = CompactionState()
        assert state.summary is None

    def test_default_last_input_tokens_is_zero(self):
        state = CompactionState()
        assert state.last_input_tokens == 0

    def test_default_updated_at_is_none(self):
        state = CompactionState()
        assert state.updated_at is None


# ---------------------------------------------------------------------------
# CompactionState.to_dict camelCase keys (Req 16.2)
# ---------------------------------------------------------------------------

class TestCompactionStateToDict:
    def test_to_dict_has_camel_case_keys(self):
        state = CompactionState(
            checkpoint=5,
            summary="test summary",
            last_input_tokens=1234,
            updated_at="2024-01-01T00:00:00Z",
        )
        d = state.to_dict()
        assert set(d.keys()) == {"checkpoint", "summary", "lastInputTokens", "updatedAt"}

    def test_to_dict_values_match(self):
        state = CompactionState(
            checkpoint=10,
            summary="hello",
            last_input_tokens=500,
            updated_at="2024-06-15T12:00:00Z",
        )
        d = state.to_dict()
        assert d["checkpoint"] == 10
        assert d["summary"] == "hello"
        assert d["lastInputTokens"] == 500
        assert d["updatedAt"] == "2024-06-15T12:00:00Z"

    def test_to_dict_with_defaults(self):
        state = CompactionState()
        d = state.to_dict()
        assert d["checkpoint"] == 0
        assert d["summary"] is None
        assert d["lastInputTokens"] == 0
        assert d["updatedAt"] is None


# ---------------------------------------------------------------------------
# CompactionState.from_dict valid dictionary (Req 16.3)
# ---------------------------------------------------------------------------

class TestCompactionStateFromDictValid:
    def test_from_dict_reconstructs_state(self):
        data = {
            "checkpoint": 7,
            "summary": "conversation summary",
            "lastInputTokens": 9999,
            "updatedAt": "2024-03-20T08:30:00Z",
        }
        state = CompactionState.from_dict(data)
        assert state.checkpoint == 7
        assert state.summary == "conversation summary"
        assert state.last_input_tokens == 9999
        assert state.updated_at == "2024-03-20T08:30:00Z"

    def test_from_dict_partial_data_fills_defaults(self):
        data = {"checkpoint": 3}
        state = CompactionState.from_dict(data)
        assert state.checkpoint == 3
        assert state.summary is None
        assert state.last_input_tokens == 0
        assert state.updated_at is None


# ---------------------------------------------------------------------------
# CompactionState.from_dict with None or empty dict (Req 16.4)
# ---------------------------------------------------------------------------

class TestCompactionStateFromDictNoneEmpty:
    def test_from_dict_none_returns_default(self):
        state = CompactionState.from_dict(None)
        assert state.checkpoint == 0
        assert state.summary is None
        assert state.last_input_tokens == 0

    def test_from_dict_empty_dict_returns_default(self):
        state = CompactionState.from_dict({})
        assert state.checkpoint == 0
        assert state.summary is None
        assert state.last_input_tokens == 0


# ---------------------------------------------------------------------------
# CompactionConfig.from_env with env vars (Req 16.6)
# ---------------------------------------------------------------------------

class TestCompactionConfigFromEnv:
    def test_from_env_reads_all_variables(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "true")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD", "50000")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_PROTECTED_TURNS", "4")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_MAX_TOOL_CONTENT_LENGTH", "1000")

        config = CompactionConfig.from_env()
        assert config.enabled is True
        assert config.token_threshold == 50000
        assert config.protected_turns == 4
        assert config.max_tool_content_length == 1000

    def test_from_env_enabled_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "True")
        config = CompactionConfig.from_env()
        assert config.enabled is True

    def test_from_env_enabled_false_string(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "false")
        config = CompactionConfig.from_env()
        assert config.enabled is False


# ---------------------------------------------------------------------------
# CompactionConfig.from_env without env vars — defaults (Req 16.7)
# ---------------------------------------------------------------------------

class TestCompactionConfigDefaults:
    def test_from_env_defaults_when_no_vars(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", raising=False)
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD", raising=False)
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_PROTECTED_TURNS", raising=False)
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_MAX_TOOL_CONTENT_LENGTH", raising=False)

        config = CompactionConfig.from_env()
        assert config.enabled is True
        assert config.token_threshold == 100_000
        assert config.protected_turns == 3
        assert config.max_tool_content_length == 500
