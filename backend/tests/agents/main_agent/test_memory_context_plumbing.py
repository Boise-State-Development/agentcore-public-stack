"""The Memory-Space block travels as ``memory_context``, not inside the prompt
(Shared Projects 2.2): ChatAgent hands it to the factory, the voice agent keeps
it in its prompt, and the paused-turn snapshot persists it for resume."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.voice_agent import VoiceAgent

BLOCK = '<memory_space scope="agent" name="Notes" note="...">\n\nx\n\n</memory_space>'


def _chat_shell(memory_context):
    agent = ChatAgent.__new__(ChatAgent)
    agent.system_prompt = "BASE PROMPT"
    agent.memory_context = memory_context
    agent.model_config = MagicMock()
    agent.session_manager = MagicMock()
    agent._accessible_skill_ids = None
    agent.session_id = None  # no offloader
    agent.user_id = None
    agent._build_filtered_tools = lambda: []
    agent._create_hooks = lambda: []
    return agent


@pytest.mark.parametrize("memory", [BLOCK, None])
def test_chat_agent_hands_memory_context_to_the_factory(memory):
    agent = _chat_shell(memory)
    with patch("agents.main_agent.chat_agent.AgentFactory.create_agent") as create:
        agent._create_agent()
    kwargs = create.call_args.kwargs
    assert kwargs["memory_context"] == memory
    assert "memory_space" not in kwargs["system_prompt"], "memory must not be inside the prompt text"


def test_voice_agent_keeps_memory_in_its_prompt():
    agent = VoiceAgent.__new__(VoiceAgent)
    agent.system_prompt = "BASE PROMPT"
    agent.memory_context = BLOCK
    prompt = agent._build_voice_system_prompt()
    assert prompt.startswith("BASE PROMPT\n\n" + BLOCK)
    assert "Voice Interaction Guidelines" in prompt


@pytest.mark.asyncio
async def test_paused_snapshot_persists_memory_context():
    from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    wrapper = SimpleNamespace(_construction_snapshot={"enabled_tools": [], "memory_context": BLOCK})
    with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
        await StreamCoordinator()._persist_paused_turn_snapshot(agent, "sess-1", "user-1", wrapper)
    assert write.await_args.args[2].memory_context == BLOCK


@pytest.mark.parametrize("memory", [BLOCK, None])
def test_base_agent_snapshots_memory_context_and_keeps_it_out_of_the_prompt(memory):
    from agents.main_agent import base_agent as ba

    class Probe(ba.BaseAgent):
        def _create_agent(self):
            pass

        async def stream_async(self, *a, **k):  # pragma: no cover - abstract stub
            yield None

    with patch.object(ba, "create_default_registry", return_value=MagicMock()), \
         patch.object(ba, "ToolFilter", return_value=MagicMock()), \
         patch.object(ba.BaseAgent, "_register_external_mcp_tools", lambda self: None), \
         patch.object(ba, "GatewayIntegration", return_value=MagicMock()), \
         patch.object(ba, "SessionFactory") as sf, \
         patch.object(ba, "StreamCoordinator", return_value=MagicMock()):
        sf.create_session_manager.return_value = MagicMock()
        agent = Probe(session_id="s", user_id="u", system_prompt="Agent instructions.", memory_context=memory)

    assert agent.memory_context == memory
    assert agent._construction_snapshot["memory_context"] == memory
    assert agent._construction_snapshot["system_prompt"] == "Agent instructions."
    assert "memory_space" not in agent.system_prompt
