"""Tests for agent type registry and factory function."""

import pytest
from agents.main_agent.agent_types import (
    create_agent,
    register_agent_type,
    get_available_types,
    _AGENT_TYPES,
)
from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.main_agent import MainAgent


class TestGetAvailableTypes:
    """Req AT-1: Available types discovery."""

    def test_chat_is_registered_by_default(self):
        assert "chat" in get_available_types()

    def test_returns_list_of_strings(self):
        types = get_available_types()
        assert isinstance(types, list)
        assert all(isinstance(t, str) for t in types)


class TestRegisterAgentType:
    """Req AT-2: Dynamic agent type registration."""

    def test_register_new_type(self):
        class DummyAgent(BaseAgent):
            def _create_agent(self): pass
            async def stream_async(self, message, **kwargs): yield ""

        register_agent_type("dummy", DummyAgent)
        assert "dummy" in get_available_types()

        # Cleanup
        del _AGENT_TYPES["dummy"]

    def test_register_overwrites_existing(self):
        original = _AGENT_TYPES.get("chat")
        try:
            register_agent_type("chat", MainAgent)
            assert _AGENT_TYPES["chat"] is MainAgent
        finally:
            _AGENT_TYPES["chat"] = original


class TestCreateAgent:
    """Req AT-3: Factory function routing."""

    def test_unknown_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown agent_type 'nonexistent'"):
            create_agent("nonexistent", session_id="test")

    def test_error_message_lists_available_types(self):
        with pytest.raises(ValueError, match="chat"):
            create_agent("nonexistent", session_id="test")


class TestMainAgentBackwardCompat:
    """Req AT-4: MainAgent remains a valid ChatAgent subclass."""

    def test_mainagent_is_subclass_of_chatagent(self):
        assert issubclass(MainAgent, ChatAgent)

    def test_mainagent_is_subclass_of_baseagent(self):
        assert issubclass(MainAgent, BaseAgent)

    def test_chatagent_is_subclass_of_baseagent(self):
        assert issubclass(ChatAgent, BaseAgent)
