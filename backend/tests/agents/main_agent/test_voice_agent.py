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

    def test_provider_takes_audio_voice_and_region_kwargs(self):
        """1.57 split `voice` out of the audio config; region stays a kwarg."""
        source = self._provider_source()
        assert "audio: BedrockNovaSonicAudioConfig | None = None" in source
        assert "voice: str =" in source
        assert "region: str | None = None" in source
        assert "provider_config" not in source

    def test_audio_config_carries_the_per_direction_rates_we_send(self):
        """VoiceAgent sends {"input": {"sample_rate"}, "output": {"sample_rate"}}."""
        from strands.experimental.bidi.models.configs import (
            BedrockNovaSonicAudioConfig,
            BedrockNovaSonicAudioStreamConfig,
        )

        assert {"input", "output"} == set(BedrockNovaSonicAudioConfig.__annotations__)
        assert set(BedrockNovaSonicAudioStreamConfig.__annotations__) == {"sample_rate"}
        assert Defaults.NOVA_SONIC_INPUT_RATE in (8000, 16000, 24000)
        assert Defaults.NOVA_SONIC_OUTPUT_RATE in (8000, 16000, 24000)

    def test_agent_send_accepts_the_input_shapes_we_use(self):
        """send_audio / send_text build `audio_delta` and `text` dicts."""
        import importlib.util
        import pathlib

        spec = importlib.util.find_spec("strands.experimental.bidi")
        source = (pathlib.Path(spec.origin).parent / "agent" / "agent.py").read_text()
        assert '"audio_delta" in content_data' in source
        assert '"text" in content_data' in source

    def test_output_event_names_the_wire_adapter_translates(self):
        """If upstream renames these again, VoiceWireAdapter must follow."""
        import importlib.util
        import pathlib

        spec = importlib.util.find_spec("strands.experimental.bidi")
        source = (pathlib.Path(spec.origin).parent / "types" / "events.py").read_text()
        for name in (
            "bidi_response_start",
            "bidi_response_stop",
            "bidi_transcript_start",
            "bidi_transcript_delta",
            "bidi_transcript_stop",
            "bidi_audio_start",
            "bidi_audio_delta",
            "bidi_audio_stop",
            "bidi_barge_in",
            "bidi_usage",
            "bidi_connection_stop",
        ):
            assert f'"type": "{name}"' in source, name

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


class TestVoiceSelection:
    """The voice a session speaks with is the user's, validated, never a bad id."""

    def _agent(self, voice=None, monkeypatch=None):
        from agents.main_agent.voice_agent import VoiceAgent

        agent = VoiceAgent.__new__(VoiceAgent)
        # Only the constructor's voice resolution is under test; skip BaseAgent.
        with patch.object(BaseAgent, "__init__", return_value=None):
            VoiceAgent.__init__(agent, voice=voice)
        return agent

    def test_defaults_to_the_platform_voice(self, monkeypatch):
        monkeypatch.delenv(EnvVars.NOVA_SONIC_VOICE, raising=False)
        assert self._agent().voice_id == Defaults.NOVA_SONIC_VOICE

    def test_uses_a_requested_catalog_voice(self, monkeypatch):
        monkeypatch.delenv(EnvVars.NOVA_SONIC_VOICE, raising=False)
        assert self._agent(voice="Carlos").voice_id == "carlos"

    def test_unknown_request_falls_back_to_the_env_default(self, monkeypatch):
        monkeypatch.setenv(EnvVars.NOVA_SONIC_VOICE, "matthew")
        assert self._agent(voice="siri").voice_id == "matthew"

    def test_bad_env_default_never_reaches_bedrock(self, monkeypatch):
        monkeypatch.setenv(EnvVars.NOVA_SONIC_VOICE, "alexa")
        assert self._agent(voice=None).voice_id == "tiffany"


class TestVoicePresentationInPrompt:
    """Amazon's guidance: tell the model the voice's gender for languages that conjugate it."""

    def _prompt_for(self, voice_id):
        from agents.main_agent.voice_agent import VoiceAgent

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.system_prompt = "Base."
        agent._voice = voice_id
        return agent._build_voice_system_prompt()

    def test_feminine_voice(self):
        prompt = self._prompt_for("carolina")
        assert 'voice "Carolina"' in prompt
        assert "use the feminine form" in prompt

    def test_masculine_voice(self):
        prompt = self._prompt_for("leo")
        assert 'voice "Leo"' in prompt
        assert "use the masculine form" in prompt

    def test_guidelines_still_precede_the_voice_line(self):
        prompt = self._prompt_for("tiffany")
        assert prompt.index("Voice Interaction Guidelines") < prompt.index('voice "Tiffany"')


class TestUsageModalitySplit:
    """Nova's usageEvent split survives the provider, and prices each bucket."""

    NOVA_USAGE = {
        "totalInputTokens": 1_300,
        "totalOutputTokens": 2_400,
        "totalTokens": 3_700,
        "details": {
            "delta": {"input": {"speechTokens": 10, "textTokens": 1}, "output": {"speechTokens": 20, "textTokens": 2}},
            "total": {
                "input": {"speechTokens": 1_000, "textTokens": 300},
                "output": {"speechTokens": 2_000, "textTokens": 400},
            },
        },
    }

    def test_split_rows_come_from_details_total_not_delta(self):
        from agents.main_agent.voice_agent import usage_modality_details

        rows = usage_modality_details(self.NOVA_USAGE)
        assert rows == [
            {"modality": "audio", "input_tokens": 1_000, "output_tokens": 2_000},
            {"modality": "text", "input_tokens": 300, "output_tokens": 400},
        ]

    def test_no_details_means_no_rows(self):
        from agents.main_agent.voice_agent import usage_modality_details

        assert usage_modality_details({"totalInputTokens": 5, "totalOutputTokens": 6}) == []
        assert usage_modality_details({"details": {}}) == []

    @pytest.mark.asyncio
    async def test_receive_events_accumulates_the_split_and_prices_it(self, monkeypatch):
        from agents.main_agent.voice_agent import VoiceAgent, VoiceWireAdapter

        agent = VoiceAgent.__new__(VoiceAgent)
        agent._wire = VoiceWireAdapter()
        agent._accumulated_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        agent._per_turn_usage = []
        agent._turn_count = 0
        agent._response_start_count = 0

        usage_event = {
            "type": "bidi_usage",
            "inputTokens": 1_300,
            "outputTokens": 2_400,
            "totalTokens": 3_700,
            "modality_details": [
                {"modality": "audio", "input_tokens": 1_000, "output_tokens": 2_000},
                {"modality": "text", "input_tokens": 300, "output_tokens": 400},
            ],
        }

        class _Ev:
            def as_dict(self):
                return dict(usage_event)

        class _Bidi:
            async def receive(self):
                yield _Ev()

        agent._bidi_agent = _Bidi()

        pricing = {
            "inputPricePerMtok": 0.319,
            "outputPricePerMtok": 2.651,
            "speechInputPricePerMtok": 3.0,
            "speechOutputPricePerMtok": 12.0,
        }

        async def fake_pricing(_model_id):
            return pricing

        import apis.shared.costs.pricing_config as pc

        monkeypatch.setattr(pc, "get_model_pricing", fake_pricing)
        monkeypatch.setenv(EnvVars.NOVA_SONIC_MODEL_ID, "amazon.nova-2-sonic-v1:0")

        events = [e async for e in agent.receive_events()]
        assert len(events) == 1
        usage = agent.accumulated_usage
        assert usage["speechInputTokens"] == 1_000
        assert usage["textInputTokens"] == 300
        assert usage["speechOutputTokens"] == 2_000
        assert usage["textOutputTokens"] == 400

        expected_in = (1_000 * 3.0 + 300 * 0.319) / 1_000_000
        expected_out = (2_000 * 12.0 + 400 * 2.651) / 1_000_000
        assert events[0]["cost"]["inputCost"] == pytest.approx(expected_in)
        assert events[0]["cost"]["outputCost"] == pytest.approx(expected_out)
        assert events[0]["cost"]["total"] == pytest.approx(expected_in + expected_out)


class TestProviderSubclass:
    """The usage-detail subclass only exists with the bidi extra installed."""

    def test_subclass_overrides_the_converter_the_provider_still_has(self):
        import agents.main_agent.voice_agent as va

        if not va.BIDI_AVAILABLE:
            pytest.skip("strands-agents[bidi] not installed")
        assert issubclass(va.NovaSonicModelWithUsageDetails, va.BedrockNovaSonicModel)
        # The hook we override must still be the provider's name for it.
        assert callable(getattr(va.BedrockNovaSonicModel, "_convert_nova_event", None))

    def test_override_signature_matches_the_providers(self):
        """The provider calls the hook positionally; a stale arity raises on every event.

        1.57 added ``response_state``. A name-only check passed straight through
        that change, so pin the parameter list itself.
        """
        import inspect

        import agents.main_agent.voice_agent as va

        if not va.BIDI_AVAILABLE:
            pytest.skip("strands-agents[bidi] not installed")
        ours = list(inspect.signature(va.NovaSonicModelWithUsageDetails._convert_nova_event).parameters)
        theirs = list(inspect.signature(va.BedrockNovaSonicModel._convert_nova_event).parameters)
        assert ours == theirs

    def test_converter_keeps_the_split_on_the_usage_event(self):
        import agents.main_agent.voice_agent as va

        if not va.BIDI_AVAILABLE:
            pytest.skip("strands-agents[bidi] not installed")
        model = va.NovaSonicModelWithUsageDetails.__new__(va.NovaSonicModelWithUsageDetails)
        events = model._convert_nova_event({"usageEvent": TestUsageModalitySplit.NOVA_USAGE}, MagicMock())
        assert len(events) == 1
        d = events[0].as_dict()
        assert d["inputTokens"] == 1_300 and d["outputTokens"] == 2_400
        assert [r["modality"] for r in d["modality_details"]] == ["audio", "text"]
        assert d["modality_details"][0]["input_tokens"] == 1_000

    def test_converter_leaves_other_events_to_the_provider(self):
        import agents.main_agent.voice_agent as va

        if not va.BIDI_AVAILABLE:
            pytest.skip("strands-agents[bidi] not installed")
        model = va.NovaSonicModelWithUsageDetails.__new__(va.NovaSonicModelWithUsageDetails)
        state = MagicMock()
        with patch.object(va.BedrockNovaSonicModel, "_convert_nova_event", return_value=["delegated"]) as sup:
            assert model._convert_nova_event({"completionStart": {"completionId": "c1"}}, state) == ["delegated"]
            sup.assert_called_once_with({"completionStart": {"completionId": "c1"}}, state)

    @pytest.mark.asyncio
    async def test_drain_keeps_the_split_too(self):
        """Usage that only arrives after stop() must price on the same buckets as the live stream."""
        from agents.main_agent.voice_agent import VoiceAgent, VoiceWireAdapter

        class _Ev:
            def as_dict(self):
                return {
                    "type": "bidi_usage",
                    "inputTokens": 1_300,
                    "outputTokens": 2_400,
                    "totalTokens": 3_700,
                    "modality_details": [
                        {"modality": "audio", "input_tokens": 1_000, "output_tokens": 2_000},
                        {"modality": "text", "input_tokens": 300, "output_tokens": 400},
                    ],
                }

        class _Bidi:
            async def receive(self):
                yield _Ev()

        agent = VoiceAgent.__new__(VoiceAgent)
        agent._bidi_agent = _Bidi()
        agent._wire = VoiceWireAdapter()
        agent._accumulated_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        agent._per_turn_usage = []
        agent._turn_count = 0

        await agent.drain_remaining_events(timeout=1.0)

        assert agent.accumulated_usage["speechInputTokens"] == 1_000
        assert agent.accumulated_usage["textOutputTokens"] == 400
        assert agent.accumulated_usage["totalTokens"] == 3_700


class TestVoiceWireAdapter:
    """Strands 1.57 Bidi events -> the 1.55 wire contract the SPA speaks."""

    def _turn(self, *events):
        from agents.main_agent.voice_agent import VoiceWireAdapter

        adapter = VoiceWireAdapter()
        out = []
        for event in events:
            out.extend(adapter.translate(event))
        return out

    def test_user_speech_is_flushed_before_the_assistant_starts(self):
        out = self._turn(
            {"type": "bidi_response_start", "response_id": "r1"},
            {"type": "bidi_transcript_start", "role": "user", "content_id": "u"},
            {"type": "bidi_transcript_delta", "delta": "hello", "role": "user", "content_id": "u"},
            {"type": "bidi_transcript_stop", "transcript": "hello", "role": "user", "content_id": "u"},
            {"type": "bidi_transcript_start", "role": "assistant", "content_id": "a"},
            {"type": "bidi_transcript_delta", "delta": "hi there", "role": "assistant", "content_id": "a"},
            {"type": "bidi_audio_start"},
            {"type": "bidi_audio_delta", "audio": "QUJD", "format": "pcm", "sample_rate": 16000, "channels": 1},
            {"type": "bidi_audio_stop"},
            {"type": "bidi_transcript_stop", "transcript": "hi there", "role": "assistant", "content_id": "a"},
            {"type": "bidi_response_stop", "response_id": "r1"},
        )
        assert [e["type"] for e in out] == [
            "bidi_transcript_stream",
            "bidi_response_start",
            "bidi_transcript_stream",
            "bidi_audio_stream",
            "bidi_response_complete",
        ]
        assert out[0]["role"] == "user" and out[0]["delta"] == {"text": "hello"}
        assert out[1]["response_id"] == "r1"
        assert out[2]["role"] == "assistant" and out[2]["is_final"] is True
        assert out[3] == {
            "type": "bidi_audio_stream",
            "audio": "QUJD",
            "format": "pcm",
            "sample_rate": 16000,
            "channels": 1,
        }
        assert out[4] == {"type": "bidi_response_complete", "response_id": "r1", "stop_reason": "complete"}

    def test_exactly_one_start_per_response(self):
        out = self._turn(
            {"type": "bidi_response_start", "response_id": "r1"},
            {"type": "bidi_transcript_start", "role": "assistant", "content_id": "a"},
            {"type": "bidi_audio_start"},
            {"type": "bidi_response_stop", "response_id": "r1"},
            {"type": "bidi_response_start", "response_id": "r2"},
            {"type": "bidi_audio_start"},
            {"type": "bidi_response_stop", "response_id": "r2"},
        )
        assert [e["type"] for e in out].count("bidi_response_start") == 2

    def test_barge_in_maps_to_interruption_and_an_interrupted_complete(self):
        out = self._turn(
            {"type": "bidi_response_start", "response_id": "r1"},
            {"type": "bidi_audio_start"},
            {"type": "bidi_barge_in", "reason": "user_speech"},
            {"type": "bidi_response_stop", "response_id": "r1"},
        )
        assert out[1] == {"type": "bidi_interruption", "reason": "user_speech"}
        assert out[-1]["stop_reason"] == "interrupted"

    def test_a_response_with_no_assistant_output_still_opens_before_it_closes(self):
        out = self._turn(
            {"type": "bidi_response_start", "response_id": "r1"},
            {"type": "bidi_response_stop", "response_id": "r1"},
        )
        assert [e["type"] for e in out] == ["bidi_response_start", "bidi_response_complete"]

    def test_connection_stop_maps_to_close_and_the_rest_pass_through(self):
        out = self._turn(
            {"type": "bidi_connection_start", "connection_id": "c", "model": "m"},
            {"type": "bidi_usage", "inputTokens": 1, "outputTokens": 2, "totalTokens": 3},
            {"type": "bidi_connection_stop", "connection_id": "c", "reason": "complete"},
        )
        assert out[0]["type"] == "bidi_connection_start"
        assert out[1]["type"] == "bidi_usage" and out[1]["totalTokens"] == 3
        assert out[2] == {"type": "bidi_connection_close", "connection_id": "c", "reason": "complete"}


class TestVoiceAgentSend:
    """send_audio / send_text speak the 1.57 BidiAgent.send() input shapes."""

    def _agent(self):
        from unittest.mock import AsyncMock

        from agents.main_agent.voice_agent import VoiceAgent

        agent = VoiceAgent.__new__(VoiceAgent)
        agent._bidi_agent = MagicMock()
        agent._bidi_agent.send = AsyncMock()
        return agent

    @pytest.mark.asyncio
    async def test_send_audio_decodes_base64_into_an_audio_delta(self):
        import base64

        agent = self._agent()
        await agent.send_audio(base64.b64encode(b"\x00\x01").decode(), sample_rate=16000)
        agent._bidi_agent.send.assert_awaited_once_with(
            {"audio_delta": {"format": "pcm", "source": {"bytes": b"\x00\x01"}}}
        )

    @pytest.mark.asyncio
    async def test_send_text_sends_a_text_block(self):
        agent = self._agent()
        await agent.send_text("hello")
        agent._bidi_agent.send.assert_awaited_once_with({"text": "hello"})

    @pytest.mark.asyncio
    async def test_receive_events_counts_turns_on_the_translated_stream(self):
        from agents.main_agent.voice_agent import VoiceWireAdapter

        class _Evt(dict):
            def as_dict(self):
                return dict(self)

        raw = [
            _Evt(type="bidi_response_start", response_id="r1"),
            _Evt(type="bidi_audio_start"),
            _Evt(type="bidi_usage", inputTokens=10, outputTokens=5, totalTokens=15),
            _Evt(type="bidi_response_stop", response_id="r1"),
        ]

        async def _receive():
            for event in raw:
                yield event

        agent = self._agent()
        agent._bidi_agent.receive = _receive
        agent._wire = VoiceWireAdapter()
        agent._accumulated_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        agent._per_turn_usage = []
        agent._turn_count = 0
        agent._response_start_count = 0

        with patch("apis.shared.costs.pricing_config.get_model_pricing", side_effect=RuntimeError("no pricing")):
            out = [e async for e in agent.receive_events()]

        assert [e["type"] for e in out] == ["bidi_response_start", "bidi_usage", "bidi_response_complete"]
        assert agent.response_start_count == 1
        assert agent.turn_count == 1
        assert agent.per_turn_usage == [{"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}]
