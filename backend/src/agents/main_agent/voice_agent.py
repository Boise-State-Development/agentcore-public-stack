"""
Voice Agent — Bidirectional speech-to-speech agent using Nova Sonic.

Extends BaseAgent with BidiAgent (Strands bidirectional agent) for
real-time voice interaction. Shares session history with text ChatAgent
for voice-text continuity.

Requires: strands-agents[bidi] extra for BidiAgent and BidiNovaSonicModel.

Based on the voice agent pattern from:
https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import logging
import os
import sys
import types
from typing import Any, AsyncGenerator, List, Optional

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.config.constants import EnvVars, Defaults

logger = logging.getLogger(__name__)

# Optional imports — BidiAgent requires the strands bidi extra
try:
    from strands.agent.bidi import BidiAgent
    from strands.models.bidi_nova_sonic import BidiNovaSonicModel
    BIDI_AVAILABLE = True
except ImportError:
    BIDI_AVAILABLE = False
    logger.info("BidiAgent not available — install strands-agents[bidi] for voice support")


# Mock PyAudio to avoid dependency — browser uses Web Audio API
if "pyaudio" not in sys.modules:
    _fake_pyaudio = types.ModuleType("pyaudio")
    _fake_pyaudio.PyAudio = type("PyAudio", (), {})
    sys.modules["pyaudio"] = _fake_pyaudio


class VoiceAgent(BaseAgent):
    """
    Bidirectional voice agent using AWS Nova Sonic 2.

    Provides:
    - Real-time speech-to-speech via BidiNovaSonicModel
    - Voice-text continuity (loads previous text chat history)
    - Separate agent_id ("voice") to avoid session state conflicts
    - Configurable voice, sample rate, and model via environment variables

    Usage:
        agent = VoiceAgent(session_id="sess-123", enabled_tools=[...])
        await agent.start()
        await agent.send_audio(audio_base64, sample_rate=16000)
        async for event in agent.stream_async(""):
            # BidiOutputEvent, BidiAudioStreamEvent, etc.
    """

    def __init__(self, voice: Optional[str] = None, **kwargs):
        """
        Initialize voice agent.

        Args:
            voice: Voice name override ("matthew", "tiffany", "amy").
                   Defaults to NOVA_SONIC_VOICE env var or "tiffany".
            **kwargs: All BaseAgent constructor args
        """
        self._voice = voice or os.environ.get(
            EnvVars.NOVA_SONIC_VOICE, Defaults.NOVA_SONIC_VOICE
        )
        self._bidi_agent: Any = None
        super().__init__(**kwargs)

    def _create_agent(self) -> None:
        """Create BidiAgent with Nova Sonic model and shared tools."""
        if not BIDI_AVAILABLE:
            raise RuntimeError(
                "Voice agent requires BidiAgent. "
                "Install with: uv sync --extra bidi"
            )

        try:
            tools = self._build_filtered_tools()

            # Configure Nova Sonic 2 model
            model_id = os.environ.get(
                EnvVars.NOVA_SONIC_MODEL_ID, Defaults.NOVA_SONIC_MODEL_ID
            )

            model = BidiNovaSonicModel(
                model_id=model_id,
                provider_config={
                    "audio": {
                        "voice": self._voice,
                        "input_rate": Defaults.NOVA_SONIC_INPUT_RATE,
                        "output_rate": Defaults.NOVA_SONIC_OUTPUT_RATE,
                        "channels": 1,
                        "format": "pcm",
                    },
                },
                client_config={"region": os.environ.get(EnvVars.AWS_REGION, Defaults.AWS_REGION)},
            )

            # Build voice-specific system prompt
            voice_prompt = self._build_voice_system_prompt()

            # Load text history for voice-text continuity
            initial_messages = self._load_text_history()

            # Create BidiAgent with separate agent_id
            self._bidi_agent = BidiAgent(
                model=model,
                tools=tools,
                system_prompt=voice_prompt,
                agent_id=Defaults.VOICE_AGENT_ID,
                session_manager=self.session_manager,
                messages=initial_messages,
            )

            # Also store as self.agent for BaseAgent compatibility
            self.agent = self._bidi_agent

            logger.info(
                f"VoiceAgent created: model={model_id}, voice={self._voice}, "
                f"tools={len(tools)}, history_messages={len(initial_messages)}"
            )

        except Exception as e:
            logger.error(f"Error creating voice agent: {e}")
            raise

    def _build_voice_system_prompt(self) -> str:
        """Build system prompt optimized for voice interaction."""
        base = self.system_prompt if isinstance(self.system_prompt, str) else ""
        voice_addendum = (
            "\n\n## Voice Interaction Guidelines\n"
            "- Keep responses concise and conversational\n"
            "- Avoid long lists or complex formatting (the user is listening)\n"
            "- Use natural speech patterns\n"
            "- Confirm understanding before taking actions\n"
        )
        return base + voice_addendum

    def _load_text_history(self) -> list:
        """
        Load recent text chat history for voice-text continuity.

        Reads messages from the text agent's session to provide context
        for the voice conversation.
        """
        max_messages = int(os.environ.get(
            EnvVars.NOVA_SONIC_MAX_MESSAGES, str(Defaults.NOVA_SONIC_MAX_MESSAGES)
        ))

        try:
            if hasattr(self.session_manager, "list_messages"):
                messages = self.session_manager.list_messages()
                if messages and len(messages) > max_messages:
                    messages = messages[-max_messages:]
                return messages or []
        except Exception as e:
            logger.warning(f"Could not load text history: {e}")

        return []

    async def start(self) -> None:
        """Start the bidirectional voice connection."""
        if self._bidi_agent and hasattr(self._bidi_agent, "start"):
            await self._bidi_agent.start()

    async def send_audio(self, audio_base64: str, sample_rate: int = 16000) -> None:
        """
        Send audio data to the voice agent.

        Args:
            audio_base64: Base64-encoded PCM audio
            sample_rate: Audio sample rate (default 16kHz)
        """
        if self._bidi_agent and hasattr(self._bidi_agent, "send_audio"):
            await self._bidi_agent.send_audio(audio_base64, sample_rate)

    async def send_text(self, text: str) -> None:
        """
        Send text input to the voice agent (fallback from audio).

        Args:
            text: Text message to send
        """
        if self._bidi_agent and hasattr(self._bidi_agent, "send_text"):
            await self._bidi_agent.send_text(text)

    async def stream_async(
        self, message: str, session_id: Optional[str] = None, files: Optional[List] = None, citations: Optional[List] = None, original_message: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream voice agent events.

        For voice agents, events include audio stream data, text transcriptions,
        and tool use events.

        Args:
            message: Text message (may be empty for audio-only)
            session_id: Session identifier
            files: Not used for voice
            citations: Not used for voice
            original_message: Not used for voice

        Yields:
            str: SSE formatted events
        """
        if not self._bidi_agent:
            self._create_agent()

        if hasattr(self._bidi_agent, "stream_async"):
            async for event in self._bidi_agent.stream_async(message):
                yield str(event)

    async def stop(self) -> None:
        """Stop the bidirectional voice connection."""
        if self._bidi_agent and hasattr(self._bidi_agent, "stop"):
            await self._bidi_agent.stop()
