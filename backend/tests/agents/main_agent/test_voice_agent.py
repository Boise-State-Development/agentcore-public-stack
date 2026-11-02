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

        spec = importlib.util.find_spec("strands.bidi")
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
            "strands.bidi.models.bedrock.BedrockNovaSonicModel" in imported
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
        from strands.bidi.models.configs import (
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

        spec = importlib.util.find_spec("strands.bidi")
        source = (pathlib.Path(spec.origin).parent / "agent" / "agent.py").read_text()
        assert 'case {"audio_delta": audio, **rest} if not rest:' in source
        assert 'case {"text": text, **rest} if not rest:' in source

    def test_output_event_names_the_wire_adapter_translates(self):
        """If upstream renames these again, VoiceWireAdapter must follow."""
        import importlib.util
        import pathlib

        spec = importlib.util.find_spec("strands.bidi")
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

    def test_session_manager_rides_in_as_a_hook(self):
        """1.57.2 removed BidiAgent(session_manager=); persistence now depends on `hooks=`.

        A stale kwarg raises TypeError at voice start; dropping it instead would
        silently stop saving voice transcripts. Pin both halves: the SDK still
        fires the events the manager listens on, and VoiceAgent hands it over.
        """
        import importlib.util
        import inspect
        import pathlib

        import agents.main_agent.voice_agent as va

        spec = importlib.util.find_spec("strands.bidi")
        source = (pathlib.Path(spec.origin).parent / "agent" / "agent.py").read_text()
        init = source[source.index("def __init__") : source.index('"""', source.index("def __init__"))]
        # 1.57.2 removed `session_manager=` (#4698) and 1.61.1 restored it,
        # typed `SessionManager[LocalAgent]`. We deliberately stay on `hooks=`:
        # it is the one spelling that worked across all three versions, and
        # switching now would be a behaviour change riding in on a bump.
        assert "hooks: list[HookProvider] | None = None" in init
        assert "AgentInitializedEvent" in source and "MessageAddedEvent" in source

        create = inspect.getsource(va.VoiceAgent._create_agent)
        assert "hooks=[self.session_manager]" in create
        assert "session_manager=self.session_manager" not in create

    def test_nova_sonic_usage_is_reported_as_deltas(self):
        """1.58.0 (#4886) removed `usage_is_cumulative`; events are now per-event deltas.

        `VoiceAgent._apply_usage_snapshot` sums them. If a later SDK goes back
        to cumulative totals, summing would multiply a session's bill, so pin
        both halves of the contract: the flag stays gone, and the provider
        keeps reading Nova's `delta` bucket rather than `total`.
        """
        source = self._provider_source()
        assert "usage_is_cumulative" not in source, (
            "the provider declares usage_is_cumulative again — _apply_usage_snapshot "
            "sums deltas and would now over-count; see voice_agent.py"
        )
        assert 'nova_event["usageEvent"]["details"]["delta"]' in source

    def test_usage_events_carry_the_modality_split_upstream(self):
        """We deleted our own splitter when the provider started emitting one.

        If these keys move, the speech/text buckets silently fall back to zero
        and a voice session prices entirely on the text rate card.
        """
        source = self._provider_source()
        assert "input_token_details" in source and "output_token_details" in source
        assert '"audio":' in source and '"text":' in source


class TestBidiImportsResolve:
    """Every name `voice_agent` imports from `strands.bidi` must still exist.

    `voice_agent` wraps those imports in `try/except ImportError` that degrades
    to `BIDI_AVAILABLE = False` and one INFO line, so a rename upstream does
    not crash — it turns voice off everywhere, quietly. `TestBidiProviderContract`
    checks a hand-written list of symbols and therefore only catches the ones
    somebody thought to add: on the strands-agents 1.61.1 bump it passed clean
    while `strands.bidi.types.events.ModalityUsage` had been deleted and voice
    was dead.

    This reads the imports out of our own AST instead, so a new import is
    covered the day it is written. It reads the SDK's *source* rather than
    importing it, because CI installs `--extra agentcore --extra dev` but not
    `--extra bidi`: the modules ship in the base wheel, their runtime
    dependencies do not.
    """

    def test_every_bidi_symbol_we_import_exists_in_the_installed_sdk(self):
        import ast
        import importlib.util
        import inspect
        import pathlib
        import re

        import agents.main_agent.voice_agent as va

        imports = [
            (node.module, alias.name)
            for node in ast.walk(ast.parse(inspect.getsource(va)))
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("strands.bidi")
            for alias in node.names
        ]
        assert imports, "no strands.bidi imports found — did voice_agent move?"

        missing = []
        for module, name in imports:
            spec = importlib.util.find_spec(module)
            if spec is None or not spec.origin:
                missing.append(f"{module} (module not found)")
                continue
            source = pathlib.Path(spec.origin).read_text()
            # A name is defined if it is declared here or re-exported from here.
            defined = re.search(
                rf"^(class|def|async def)\s+{re.escape(name)}\b|^{re.escape(name)}\s*[:=]"
                rf"|\b{re.escape(name)}\b",
                source,
                re.MULTILINE,
            )
            if not defined:
                missing.append(f"{module}.{name}")

        assert not missing, (
            "voice_agent imports names the installed strands SDK no longer defines: "
            f"{missing}. The import is swallowed into BIDI_AVAILABLE=False, so voice "
            "would switch off silently rather than fail."
        )

    def test_the_import_block_still_degrades_rather_than_raising(self):
        """The swallow is deliberate; this pins that it is still a swallow."""
        import agents.main_agent.voice_agent as va

        assert isinstance(va.BIDI_AVAILABLE, bool)


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
    """Nova's speech/text split, and the delta arithmetic behind it.

    strands-agents 1.58.0 (#4886) changed two things at once: `bidi_usage`
    events became PER-EVENT DELTAS (`usage_is_cumulative` was deleted) and
    `BidiUsageEvent.as_dict()` went snake_case. Both are absorbed in
    `VoiceWireAdapter._usage`, which still emits the cumulative camelCase
    shape `voice-chat.service.ts` and `_apply_usage_snapshot` are written
    against. These are the real SDK payloads, as captured from a live Nova
    Sonic turn.
    """

    # Two consecutive usage deltas exactly as the 1.61.1 provider emits them.
    DELTA_ONE = {
        "type": "bidi_usage",
        "input_tokens": 11,
        "output_tokens": 22,
        "total_tokens": 33,
        "input_token_details": {"audio": 10, "text": 1},
        "output_token_details": {"audio": 20, "text": 2},
    }
    DELTA_TWO = {
        "type": "bidi_usage",
        "input_tokens": 110,
        "output_tokens": 220,
        "total_tokens": 330,
        "input_token_details": {"audio": 100, "text": 10},
        "output_token_details": {"audio": 200, "text": 20},
    }

    @staticmethod
    def _agent():
        from agents.main_agent.voice_agent import VoiceAgent, VoiceWireAdapter

        agent = VoiceAgent.__new__(VoiceAgent)
        agent._wire = VoiceWireAdapter()
        agent._accumulated_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        agent._per_turn_usage = []
        agent._turn_count = 0
        agent._response_start_count = 0
        return agent

    @staticmethod
    def _bidi(*payloads):
        class _Ev:
            def __init__(self, payload):
                self._payload = payload

            def as_dict(self):
                return dict(self._payload)

        class _Bidi:
            async def receive(self):
                for payload in payloads:
                    yield _Ev(payload)

        return _Bidi()

    def test_the_wire_accumulates_deltas_into_cumulative_totals(self):
        """⚠️ Regression guard for the 1.58.0 cumulative -> delta flip.

        If someone makes the adapter pass deltas straight through, this fails
        with the SECOND delta's numbers instead of the running total. Both
        consumers — `_apply_usage_snapshot` here and `voice-chat.service.ts`
        in the SPA — replace rather than sum, on this wire's promise that the
        counts are cumulative. Passing deltas through would bill every voice
        session for only its final event.
        """
        from agents.main_agent.voice_agent import VoiceWireAdapter

        wire = VoiceWireAdapter()

        first = wire.translate(dict(self.DELTA_ONE))[0]
        assert first["inputTokens"] == 11

        second = wire.translate(dict(self.DELTA_TWO))[0]
        assert second["inputTokens"] == 121, "deltas must accumulate, not replace"
        assert second["outputTokens"] == 242
        assert second["totalTokens"] == 363
        assert second["inputTokens"] != self.DELTA_TWO["input_tokens"]

    def test_the_wire_reads_snake_case_and_emits_camel_case(self):
        """1.57.2 emitted `inputTokens`; 1.61.1 emits `input_tokens`.

        A camelCase read of a 1.61.1 event yields None and the totals stay at
        zero — which is how a live voice turn priced at $0 before this branch.
        """
        from agents.main_agent.voice_agent import VoiceWireAdapter

        out = VoiceWireAdapter().translate(dict(self.DELTA_ONE))[0]

        assert out["inputTokens"] == 11 and out["totalTokens"] == 33
        assert "input_tokens" not in out

    def test_the_wire_still_understands_pre_1_58_events(self):
        """Old spellings keep working, so either SDK generation lands the same."""
        from agents.main_agent.voice_agent import VoiceWireAdapter

        out = VoiceWireAdapter().translate(
            {
                "type": "bidi_usage",
                "inputTokens": 1_300,
                "outputTokens": 2_400,
                "totalTokens": 3_700,
                "modality_details": [
                    {"modality": "audio", "input_tokens": 1_000, "output_tokens": 2_000},
                    {"modality": "text", "input_tokens": 300, "output_tokens": 400},
                ],
            }
        )[0]

        assert out["inputTokens"] == 1_300
        assert out["modality_details"][0] == {
            "modality": "audio", "input_tokens": 1_000, "output_tokens": 2_000,
        }

    def test_an_event_with_no_split_carries_no_rows(self):
        from agents.main_agent.voice_agent import VoiceWireAdapter

        out = VoiceWireAdapter().translate(
            {"type": "bidi_usage", "input_tokens": 7, "output_tokens": 8, "total_tokens": 15}
        )[0]

        assert out["inputTokens"] == 7
        assert "modality_details" not in out

    def test_unknown_modalities_are_ignored_not_crashed(self):
        """TokenDetails also allows image / video / cache_read / reasoning."""
        from agents.main_agent.voice_agent import VoiceWireAdapter

        out = VoiceWireAdapter().translate(
            {"type": "bidi_usage", "input_tokens": 5, "output_tokens": 0, "total_tokens": 5,
             "input_token_details": {"audio": 4, "image": 1, "cache_read": 9}}
        )[0]

        rows = {r["modality"]: r for r in out["modality_details"]}
        assert rows["audio"]["input_tokens"] == 4
        assert set(rows) == {"audio", "text"}

    @pytest.mark.asyncio
    async def test_receive_events_accumulates_the_split_and_prices_it(self, monkeypatch):
        agent = self._agent()
        agent._bidi_agent = self._bidi(self.DELTA_ONE, self.DELTA_TWO)

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

        assert len(events) == 2
        usage = agent.accumulated_usage
        assert usage["inputTokens"] == 121
        assert usage["speechInputTokens"] == 110
        assert usage["textInputTokens"] == 11
        assert usage["speechOutputTokens"] == 220
        assert usage["textOutputTokens"] == 22

        expected_in = (110 * 3.0 + 11 * 0.319) / 1_000_000
        expected_out = (220 * 12.0 + 22 * 2.651) / 1_000_000
        assert events[-1]["cost"]["inputCost"] == pytest.approx(expected_in)
        assert events[-1]["cost"]["outputCost"] == pytest.approx(expected_out)
        assert events[-1]["cost"]["total"] == pytest.approx(expected_in + expected_out)

    @pytest.mark.asyncio
    async def test_drain_keeps_the_split_and_the_running_total(self):
        """Usage arriving after stop() shares the adapter, so totals keep adding up."""
        agent = self._agent()
        agent._bidi_agent = self._bidi(self.DELTA_ONE)
        [e async for e in agent.receive_events()]

        agent._bidi_agent = self._bidi(self.DELTA_TWO)
        await agent.drain_remaining_events(timeout=1.0)

        assert agent.accumulated_usage["inputTokens"] == 121
        assert agent.accumulated_usage["speechInputTokens"] == 110
        assert agent.accumulated_usage["textOutputTokens"] == 22


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

    def test_completed_block_events_do_not_reach_the_wire(self):
        """1.57.2 repeats each finished transcript as a block event; the deltas already carried it."""
        out = self._turn(
            {"type": "bidi_response_start", "response_id": "r1"},
            {"type": "bidi_transcript_delta", "delta": "hi", "role": "assistant", "content_id": "a"},
            {"type": "bidi_transcript_stop", "transcript": "hi", "role": "assistant", "content_id": "a"},
            {"type": "bidi_transcript_block", "text": "hi", "role": "assistant", "content_id": "a"},
            {"type": "bidi_response_stop", "response_id": "r1"},
        )
        assert "bidi_transcript_block" not in [e["type"] for e in out]
        assert [e["type"] for e in out].count("bidi_transcript_stream") == 1

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
