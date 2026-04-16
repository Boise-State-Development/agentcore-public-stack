"""Tests for VoiceAgent — module-level and class-level behavior."""

import pytest
from unittest.mock import patch, MagicMock

from agents.main_agent.config.constants import Defaults, EnvVars
from agents.main_agent.base_agent import BaseAgent


class TestVoiceAgentImport:
    """Req VA-1: VoiceAgent is importable and conditionally available."""

    def test_voice_agent_module_importable(self):
        # The module itself should always be importable
        import agents.main_agent.voice_agent as va
        assert hasattr(va, "VoiceAgent")
        assert hasattr(va, "BIDI_AVAILABLE")

    def test_voice_agent_is_base_agent_subclass(self):
        from agents.main_agent.voice_agent import VoiceAgent
        assert issubclass(VoiceAgent, BaseAgent)


class TestVoiceConstants:
    """Req VA-2: Voice configuration constants."""

    def test_default_voice(self):
        assert Defaults.NOVA_SONIC_VOICE == "tiffany"

    def test_default_model_id(self):
        assert Defaults.NOVA_SONIC_MODEL_ID == "amazon.nova-2-sonic-v1:0"

    def test_default_sample_rates(self):
        assert Defaults.NOVA_SONIC_INPUT_RATE == 16000
        assert Defaults.NOVA_SONIC_OUTPUT_RATE == 16000

    def test_default_max_messages(self):
        assert Defaults.NOVA_SONIC_MAX_MESSAGES == 20

    def test_voice_agent_id(self):
        assert Defaults.VOICE_AGENT_ID == "voice"

    def test_env_var_names(self):
        assert EnvVars.NOVA_SONIC_MODEL_ID == "NOVA_SONIC_MODEL_ID"
        assert EnvVars.NOVA_SONIC_VOICE == "NOVA_SONIC_VOICE"
        assert EnvVars.NOVA_SONIC_MAX_MESSAGES == "NOVA_SONIC_MAX_MESSAGES"


class TestVoiceAgentRegistration:
    """Req VA-3: VoiceAgent factory registration."""

    def test_voice_type_in_available_if_bidi_installed(self):
        from agents.main_agent.voice_agent import BIDI_AVAILABLE
        from agents.main_agent.agent_types import get_available_types

        if BIDI_AVAILABLE:
            assert "voice" in get_available_types()

    def test_chat_and_skill_always_available(self):
        from agents.main_agent.agent_types import get_available_types
        types = get_available_types()
        assert "chat" in types
        assert "skill" in types


class TestVoiceAgentTextHistory:
    """Req VA-4: Voice-text continuity."""

    def test_load_text_history_passes_limit(self):
        from agents.main_agent.voice_agent import VoiceAgent

        # Mock SessionMessage objects with to_message()
        mock_msgs = []
        for i in range(10):
            m = MagicMock()
            m.to_message.return_value = {"role": "user", "content": [{"text": f"msg {i}"}]}
            mock_msgs.append(m)

        mock_session = MagicMock()
        mock_session.list_messages.return_value = mock_msgs

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.session_manager = mock_session
        agent.session_id = "test-session"

        with patch.dict("os.environ", {EnvVars.NOVA_SONIC_MAX_MESSAGES: "10"}):
            messages = agent._load_text_history()

        # Verify limit is passed to list_messages
        mock_session.list_messages.assert_called_once_with(
            session_id="test-session",
            agent_id="default",
            limit=10,
        )
        # Messages are converted to dicts via to_dict()
        assert len(messages) == 10
        assert messages[0]["role"] == "user"

    def test_load_text_history_handles_empty(self):
        from agents.main_agent.voice_agent import VoiceAgent

        mock_session = MagicMock()
        mock_session.list_messages.return_value = []

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.session_manager = mock_session
        agent.session_id = "test-session"

        messages = agent._load_text_history()
        assert messages == []

    def test_load_text_history_handles_error(self):
        from agents.main_agent.voice_agent import VoiceAgent

        mock_session = MagicMock()
        mock_session.list_messages.side_effect = RuntimeError("connection failed")

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.session_manager = mock_session
        agent.session_id = "test-session"

        messages = agent._load_text_history()
        assert messages == []


class TestVoiceSystemPrompt:
    """Req VA-5: Voice-optimized system prompt."""

    def test_voice_prompt_adds_guidelines(self):
        from agents.main_agent.voice_agent import VoiceAgent

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.system_prompt = "You are a helpful assistant."

        prompt = agent._build_voice_system_prompt()
        assert "Voice Interaction Guidelines" in prompt
        assert "concise and conversational" in prompt

    def test_voice_prompt_preserves_base(self):
        from agents.main_agent.voice_agent import VoiceAgent

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.system_prompt = "Base prompt here."

        prompt = agent._build_voice_system_prompt()
        assert "Base prompt here." in prompt


class TestPyAudioMock:
    """Req VA-6: PyAudio mock is in place."""

    def test_pyaudio_in_sys_modules(self):
        import sys
        # After importing voice_agent, pyaudio should be mocked
        import agents.main_agent.voice_agent  # noqa: F401
        assert "pyaudio" in sys.modules
