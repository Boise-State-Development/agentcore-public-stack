"""
Voice Agent — Bidirectional speech-to-speech agent using Nova Sonic.

Extends BaseAgent with BidiAgent (Strands bidirectional agent) for
real-time voice interaction. Shares session history with text ChatAgent
for voice-text continuity.

Requires: strands-agents[bidi] extra for BidiAgent and BidiNovaSonicModel.

Based on the voice agent pattern from:
https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import asyncio
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
    from strands.experimental.bidi import BidiAgent
    from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
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
        # Nova Sonic bidi_usage events report CUMULATIVE token counts (not deltas).
        # _accumulated_usage stores the latest cumulative snapshot from the stream.
        self._accumulated_usage: dict = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        self._per_turn_usage: List[dict] = []  # Snapshot of usage per completed turn
        self._turn_count: int = 0  # Completed turns (bidi_response_complete)
        self._response_start_count: int = 0  # Started turns (bidi_response_start)
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

        Reads messages from the text (default) agent's session to provide
        context for the voice conversation. Uses agent_id="default" to
        read from the text chat agent's history.
        """
        max_messages = int(os.environ.get(
            EnvVars.NOVA_SONIC_MAX_MESSAGES, str(Defaults.NOVA_SONIC_MAX_MESSAGES)
        ))

        try:
            if hasattr(self.session_manager, "list_messages"):
                # AgentCoreMemorySessionManager.list_messages requires session_id and agent_id
                # Use "default" to read the text chat agent's history
                session_messages = self.session_manager.list_messages(
                    session_id=self.session_id,
                    agent_id="default",
                    limit=max_messages,
                )
                if not session_messages:
                    return []

                # Convert SessionMessage objects to plain message dicts for BidiAgent
                # to_message() returns {"role": "user"|"assistant", "content": [...]}
                # (to_dict() wraps it in metadata — Nova Sonic needs the inner message)
                return [msg.to_message() for msg in session_messages]
        except Exception as e:
            logger.warning(f"Could not load text history: {e}")

        return []

    async def start(self) -> None:
        """Start the bidirectional voice connection."""
        if not self._bidi_agent:
            self._create_agent()
        await self._bidi_agent.start()

    async def send_audio(self, audio_base64: str, sample_rate: int = 16000) -> None:
        """
        Send audio data to the voice agent via BidiAgent.send().

        Args:
            audio_base64: Base64-encoded PCM audio
            sample_rate: Audio sample rate (default 16kHz)
        """
        if not self._bidi_agent:
            raise RuntimeError("Voice agent not started")

        await self._bidi_agent.send({
            "type": "bidi_audio_input",
            "audio": audio_base64,
            "format": "pcm",
            "sample_rate": sample_rate,
            "channels": 1,
        })

    async def send_text(self, text: str) -> None:
        """
        Send text input to the voice agent via BidiAgent.send().

        Args:
            text: Text message to send
        """
        if not self._bidi_agent:
            raise RuntimeError("Voice agent not started")

        await self._bidi_agent.send({
            "type": "bidi_text_input",
            "text": text,
            "role": "user",
        })

    @property
    def accumulated_usage(self) -> dict:
        """Token usage accumulated across all voice turns."""
        return self._accumulated_usage

    @property
    def turn_count(self) -> int:
        """Number of completed assistant response turns."""
        return self._turn_count

    @property
    def response_start_count(self) -> int:
        """Number of started assistant responses (may exceed turn_count if interrupted)."""
        return self._response_start_count

    @property
    def per_turn_usage(self) -> List[dict]:
        """Token usage snapshots for each completed turn."""
        return self._per_turn_usage

    @property
    def voice_model_id(self) -> str:
        """Nova Sonic model ID used by this agent."""
        return os.environ.get(EnvVars.NOVA_SONIC_MODEL_ID, Defaults.NOVA_SONIC_MODEL_ID)

    async def receive_events(self) -> AsyncGenerator[dict, None]:
        """
        Receive and transform events from BidiAgent for WebSocket transmission.

        Wraps BidiAgent.receive() and converts typed event objects to plain
        dicts via as_dict(). This is the primary event source for the voice
        WebSocket route.

        Intercepts bidi_usage events to accumulate token counts, calculate
        real-time cost, and enrich the event with a cost breakdown before
        forwarding to the client.

        Yields:
            dict: Event dictionaries suitable for JSON serialization
        """
        if not self._bidi_agent:
            raise RuntimeError("Voice agent not started")

        # Lazy-loaded pricing for real-time cost calculation
        pricing_dict: Optional[dict] = None
        pricing_loaded = False

        async for event in self._bidi_agent.receive():
            if hasattr(event, "as_dict"):
                event_dict = event.as_dict()
            else:
                event_dict = {"type": "unknown", "data": str(event)}

            event_type = event_dict.get("type", "")

            # Log non-audio event types for debugging (skip audio to avoid noise)
            if event_type not in ("bidi_audio_stream",):
                if any(k in event_dict for k in ("usage", "inputTokens", "outputTokens", "totalTokens")):
                    logger.info(f"Voice event: type={event_type}, keys={list(event_dict.keys())}")
                else:
                    logger.info(f"Voice event: type={event_type}")

            # Track started turns (bidi_response_start)
            if event_type == "bidi_response_start":
                self._response_start_count += 1
                logger.info(f"Voice response started (count={self._response_start_count})")

            # Update cumulative token usage from bidi_usage events.
            # Nova Sonic reports CUMULATIVE totals in each event (not deltas),
            # so we replace rather than sum.
            if event_type == "bidi_usage":
                usage = event_dict.get("usage", event_dict)
                for key in ("inputTokens", "outputTokens", "totalTokens"):
                    self._accumulated_usage[key] = usage.get(key, self._accumulated_usage[key])
                logger.info(f"Voice bidi_usage snapshot: {usage}")
                logger.info(f"Voice usage current: {self._accumulated_usage}")

                # Calculate real-time cost and enrich the event for the client.
                # Pricing is fetched once and cached for the session lifetime.
                if not pricing_loaded:
                    pricing_loaded = True
                    try:
                        from apis.app_api.costs.pricing_config import get_model_pricing
                        pricing_dict = await get_model_pricing(self.voice_model_id)
                    except Exception as e:
                        logger.debug(f"Voice pricing unavailable: {e}")

                if pricing_dict and self._accumulated_usage.get("totalTokens", 0) > 0:
                    try:
                        from apis.app_api.costs.calculator import CostCalculator
                        total_cost, breakdown = CostCalculator.calculate_message_cost(
                            self._accumulated_usage, pricing_dict
                        )
                        event_dict["cost"] = {
                            "total": total_cost,
                            "inputCost": breakdown.input_cost,
                            "outputCost": breakdown.output_cost,
                            "cacheReadCost": breakdown.cache_read_cost,
                            "cacheWriteCost": breakdown.cache_write_cost,
                        }
                    except Exception as e:
                        logger.debug(f"Voice cost calculation error: {e}")

            # Track completed assistant turns and snapshot cumulative usage at turn boundary
            if event_type == "bidi_response_complete":
                self._per_turn_usage.append(self._accumulated_usage.copy())
                self._turn_count += 1
                logger.info(f"Voice turn {self._turn_count} complete, cumulative usage: {self._accumulated_usage}")

            yield event_dict

    async def stream_async(
        self,
        message: str,
        session_id: Optional[str] = None,
        files: Optional[List] = None,
        citations: Optional[List] = None,
        original_message: Optional[str] = None,
        interrupt_responses: Optional[List] = None,
    ) -> AsyncGenerator[str, None]:
        """
        BaseAgent interface compatibility — not used for voice mode.

        Voice mode uses start() + send_audio()/send_text() + receive_events() + stop()
        instead of the request-response stream_async() pattern.
        """
        logger.warning("stream_async() called on VoiceAgent — use receive_events() instead")
        return
        yield  # Make this a generator

    async def drain_remaining_events(self, timeout: float = 3.0) -> None:
        """Drain remaining events from BidiAgent to capture final usage data.

        After stop() is called, the BidiAgent may still have queued events
        (especially bidi_usage) that were not consumed because the
        _send_to_client task was cancelled on client disconnect. This method
        consumes those remaining events with a timeout to ensure accumulated
        usage totals are complete before finalization.
        """
        if not self._bidi_agent:
            return
        try:
            async with asyncio.timeout(timeout):
                async for event in self._bidi_agent.receive():
                    if hasattr(event, "as_dict"):
                        event_dict = event.as_dict()
                    else:
                        continue
                    event_type = event_dict.get("type", "")
                    if event_type == "bidi_usage":
                        usage = event_dict.get("usage", event_dict)
                        for key in ("inputTokens", "outputTokens", "totalTokens"):
                            self._accumulated_usage[key] = usage.get(key, self._accumulated_usage[key])
                        logger.info(f"Drained bidi_usage: {usage}, current: {self._accumulated_usage}")
                    elif event_type == "bidi_response_complete":
                        self._per_turn_usage.append(self._accumulated_usage.copy())
                        self._turn_count += 1
                        logger.info(f"Drained turn {self._turn_count} complete")
        except (TimeoutError, asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception as e:
            logger.debug(f"Event drain ended: {e}")

    async def stop(self) -> None:
        """Stop the bidirectional voice connection.

        Handles CancelledError from the BidiAgent's internal stream teardown
        gracefully — the Nova Sonic SDK may cancel pending futures during
        shutdown, which is expected and not an error.
        """
        if self._bidi_agent and hasattr(self._bidi_agent, "stop"):
            try:
                await self._bidi_agent.stop()
            except asyncio.CancelledError:
                logger.debug("BidiAgent stop cancelled (expected during teardown)")
            except Exception as e:
                logger.warning(f"Error during BidiAgent stop: {e}")
        logger.info(f"Voice agent stopped. Final accumulated_usage: {self._accumulated_usage}")
