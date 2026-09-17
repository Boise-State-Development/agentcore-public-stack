"""The chat agent must not run under Strands' default 40-message window.

docs/specs/compaction-model-relative-thresholds.md §3.0: compaction owns
history size; the SDK window would slide the front of the list every turn
past 40 messages (a prefix re-write per turn) and move the coordinates the
compaction checkpoint is expressed in.
"""

from strands.agent.conversation_manager import SlidingWindowConversationManager

from agents.main_agent.config.constants import Defaults
from agents.main_agent.core.agent_factory import AgentFactory


def test_default_window_is_large_not_forty(monkeypatch):
    monkeypatch.delenv("AGENTCORE_CONVERSATION_WINDOW_MESSAGES", raising=False)
    cm = AgentFactory.build_conversation_manager()
    assert isinstance(cm, SlidingWindowConversationManager)
    assert cm.window_size == Defaults.CONVERSATION_WINDOW_MESSAGES
    assert cm.window_size >= 1000
    # Overflow recovery keeps tool-result truncation on.
    assert cm.should_truncate_results is True


def test_env_override_restores_sdk_default(monkeypatch):
    monkeypatch.setenv("AGENTCORE_CONVERSATION_WINDOW_MESSAGES", "40")
    assert AgentFactory.build_conversation_manager().window_size == 40


def test_garbage_env_falls_back(monkeypatch):
    monkeypatch.setenv("AGENTCORE_CONVERSATION_WINDOW_MESSAGES", "lots")
    assert AgentFactory.build_conversation_manager().window_size == Defaults.CONVERSATION_WINDOW_MESSAGES
