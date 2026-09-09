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


class TestBidiProviderContract:
    """Bind the voice provider import against the pinned SDK.

    ``voice_agent`` imports the provider inside a ``try/except ImportError``
    that degrades to ``BIDI_AVAILABLE = False`` and one INFO line. A rename
    upstream therefore does not crash — it silently turns voice off. That is
    exactly what strands-agents 1.55.0 did: ``models.nova_sonic``'s
    ``BidiNovaSonicModel`` became ``models.bedrock``'s
    ``BedrockNovaSonicModel``.

    These assertions read the pinned SDK's *source*, not a live import, because
    ``tests.yml`` installs ``--extra agentcore --extra dev`` but not
    ``--extra bidi``: the provider module ships in the base wheel while its
    runtime dependencies do not, so importing it here would fail on CI even
    when the pin is correct.
    """

    def _provider_source(self):
        import importlib.util
        import pathlib

        spec = importlib.util.find_spec("strands.experimental.bidi")
        assert spec is not None and spec.origin, "strands bidi package not found"
        provider = pathlib.Path(spec.origin).parent / "models" / "bedrock.py"
        assert provider.is_file(), (
            f"{provider} is missing — the bidi provider module was renamed again; "
            "update the import in agents/main_agent/voice_agent.py"
        )
        return provider.read_text()

    def test_provider_module_defines_the_class_we_import(self):
        assert "class BedrockNovaSonicModel" in self._provider_source()

    def test_voice_agent_imports_the_current_provider_name(self):
        """Read the module's import statements, not its prose.

        The comment above the import names the old symbol on purpose, so match
        against the parsed AST rather than the raw text.
        """
        import ast
        import inspect

        import agents.main_agent.voice_agent as va

        imported = {
            f"{node.module}.{alias.name}"
            for node in ast.walk(ast.parse(inspect.getsource(va)))
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        assert (
            "strands.experimental.bidi.models.bedrock.BedrockNovaSonicModel" in imported
        )
        assert not any("BidiNovaSonicModel" in name for name in imported), (
            f"stale 1.51 provider name still imported: {sorted(imported)}"
        )

    def test_provider_takes_flattened_audio_and_region_kwargs(self):
        """1.55.0 replaced provider_config/client_config with audio/region."""
        source = self._provider_source()
        assert "audio: AudioConfig | None = None" in source
        assert "region: str | None = None" in source
        assert "provider_config" not in source

    def test_audio_config_still_carries_the_five_keys_we_send(self):
        from strands.experimental.bidi.types.model import AudioConfig

        assert {"voice", "input_rate", "output_rate", "channels", "format"} <= set(
            AudioConfig.__annotations__
        )

    def test_nova_sonic_usage_is_still_cumulative(self):
        """VoiceAgent de-cumulates bidi_usage; a switch to deltas would double-count."""
        assert "usage_is_cumulative = True" in self._provider_source()


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
