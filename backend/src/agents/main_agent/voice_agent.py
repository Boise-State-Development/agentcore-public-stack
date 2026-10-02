"""
Voice Agent — Bidirectional speech-to-speech agent using Nova Sonic.

Extends BaseAgent with BidiAgent (Strands bidirectional agent) for
real-time voice interaction. Shares session history with text ChatAgent
for voice-text continuity.

Requires: strands-agents[bidi] extra for BidiAgent and BedrockNovaSonicModel.

Based on the voice agent pattern from:
https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import asyncio
import base64
import logging
import os
import sys
import types
from typing import Any, AsyncGenerator, Dict, List, Optional

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.config.constants import EnvVars, Defaults
from apis.shared.voice_catalog import NovaSonicVoice, get_voice, resolve_voice_id

logger = logging.getLogger(__name__)

# Optional imports — BidiAgent requires the strands bidi extra.
#
# strands-agents 1.55.0 renamed the provider: the module went
# ``models.nova_sonic`` -> ``models.bedrock`` and the class
# ``BidiNovaSonicModel`` -> ``BedrockNovaSonicModel``. That is an ImportError,
# which this block swallows into BIDI_AVAILABLE=False — so a stale import would
# not crash, it would silently turn voice off everywhere with one INFO line.
# Import the name explicitly rather than leaning on the package's lazy
# ``__getattr__``, so a future rename fails loudly here too.
#
# 1.57.2 graduated the API from ``strands.experimental.bidi`` to
# ``strands.bidi``. The experimental path survives as a shim that resolves to
# the same module objects (so ``isinstance`` holds across both) but warns on
# import; import the stable path.
try:
    from strands.bidi import BidiAgent
    from strands.bidi.models.bedrock import BedrockNovaSonicModel
    from strands.bidi.types.events import BidiUsageEvent, ModalityUsage
    BIDI_AVAILABLE = True
except ImportError:
    BIDI_AVAILABLE = False


def usage_modality_details(usage_event: dict) -> list:
    """Split a Nova Sonic ``usageEvent`` into Strands ``ModalityUsage`` rows.

    Nova reports four buckets under ``details.total`` — speech and text, in and
    out — and bills them on two rate cards ten-fold apart. Strands 1.55's
    provider keeps only the grand totals, which is what made every voice
    session price as if it were all speech (or, with no catalog row, $0).

    Returns ``[]`` when the event carries no ``details`` so the caller can
    tell "no split" from "a split of zeros". The rows are plain dicts (the
    ``ModalityUsage`` TypedDict shape) so this stays importable without the
    bidi extra installed.
    """
    total = (usage_event.get("details") or {}).get("total") or {}
    if not total:
        return []
    inp = total.get("input") or {}
    out = total.get("output") or {}
    return [
        {
            "modality": "audio",
            "input_tokens": int(inp.get("speechTokens") or 0),
            "output_tokens": int(out.get("speechTokens") or 0),
        },
        {
            "modality": "text",
            "input_tokens": int(inp.get("textTokens") or 0),
            "output_tokens": int(out.get("textTokens") or 0),
        },
    ]


if BIDI_AVAILABLE:

    class NovaSonicModelWithUsageDetails(BedrockNovaSonicModel):
        """``BedrockNovaSonicModel`` that keeps the speech/text usage split.

        Only ``usageEvent`` is intercepted; every other event goes to the
        provider untouched. The split rides the ``modality_details`` slot the
        SDK already reserves on ``BidiUsageEvent`` (and serialises through
        ``as_dict``), so nothing downstream has to learn a new event shape.

        1.57 changed this hook's contract: it now takes the provider's
        per-response state and returns a *list* of events. The provider calls
        it positionally, so an override with the old one-argument signature
        raises on every Nova event — which is why the signature is asserted
        against the provider's in the tests rather than trusted.
        """

        def _convert_nova_event(self, nova_event: dict, response_state: Any) -> list:
            usage = nova_event.get("usageEvent")
            if not usage:
                return super()._convert_nova_event(nova_event, response_state)
            details = usage_modality_details(usage)
            total_input = usage.get("totalInputTokens", 0)
            total_output = usage.get("totalOutputTokens", 0)
            return [
                BidiUsageEvent(
                    input_tokens=total_input,
                    output_tokens=total_output,
                    total_tokens=usage.get("totalTokens", total_input + total_output),
                    modality_details=[ModalityUsage(**row) for row in details] or None,
                )
            ]
else:
    logger.info("BidiAgent not available — install strands-agents[bidi] for voice support")


# Mock PyAudio to avoid dependency — browser uses Web Audio API
if "pyaudio" not in sys.modules:
    _fake_pyaudio = types.ModuleType("pyaudio")
    _fake_pyaudio.PyAudio = type("PyAudio", (), {})
    sys.modules["pyaudio"] = _fake_pyaudio


class VoiceWireAdapter:
    """Translate Strands >=1.57 Bidi output events into our voice WebSocket contract.

    strands-agents 1.57 renamed and re-shaped every Bidi output event (1.57.0
    #4444 "adopt production component names", 1.57.1 #4604 "remove response
    stop reasons"). The SPA (``voice-chat.service.ts``) and our own turn and
    usage accounting still speak the 1.55 wire names, so the translation lives
    here, at the one seam we own, rather than in two packages at once.

    What changed, and how each is mapped:

    * ``bidi_audio_delta`` -> ``bidi_audio_stream`` (same fields).
    * ``bidi_transcript_delta`` -> ``bidi_transcript_stream``. Nova used to
      stream an assistant transcript twice (SPECULATIVE, then FINAL) and the
      SPA commits text only when ``is_final`` is set. 1.57 streams it once,
      from the SPECULATIVE stage, and drops FINAL entirely, so every delta is
      now the only pass and is forwarded with ``is_final=True``.
    * ``bidi_response_start`` now fires at the *user's* first content, before
      any user speech has been transcribed. The SPA flushes the user's
      transcript into a message when it sees a response start, so forwarding
      that event as-is would file each user message one turn late. The legacy
      ``bidi_response_start`` is therefore emitted when the *assistant* begins
      (its transcript or its audio), which is when 1.55 emitted it too.
    * ``bidi_barge_in`` -> ``bidi_interruption``.
    * ``bidi_response_stop`` -> ``bidi_response_complete``; ``stop_reason`` is
      no longer on the event, so it is derived from whether a barge-in was
      seen in this response.
    * ``bidi_connection_stop`` -> ``bidi_connection_close``.
    * Start/stop bracketing events with no 1.55 equivalent
      (``bidi_transcript_start``/``_stop``, ``bidi_audio_start``/``_stop``)
      are dropped. So are the completed-block events 1.57.2 added
      (``bidi_transcript_block`` and its text/reasoning siblings): each repeats
      text the deltas already carried. Everything else passes through unchanged.
    """

    _DROPPED = frozenset(
        {
            "bidi_transcript_start",
            "bidi_transcript_stop",
            "bidi_audio_start",
            "bidi_audio_stop",
            "bidi_transcript_block",
            "bidi_text_block",
            "bidi_reasoning_block",
        }
    )

    def __init__(self) -> None:
        self._response_id: Optional[str] = None
        self._assistant_started = False
        self._interrupted = False

    def translate(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return the wire events for one Strands Bidi event (zero or more)."""
        event_type = event.get("type", "")

        if event_type in self._DROPPED:
            if event_type == "bidi_transcript_start" and event.get("role") == "assistant":
                return self._start_assistant()
            if event_type == "bidi_audio_start":
                return self._start_assistant()
            return []

        if event_type == "bidi_response_start":
            self._response_id = event.get("response_id")
            self._assistant_started = False
            self._interrupted = False
            return []

        if event_type == "bidi_transcript_delta":
            delta = event.get("delta", "")
            return [
                {
                    "type": "bidi_transcript_stream",
                    "role": event.get("role"),
                    "delta": {"text": delta},
                    "text": delta,
                    "is_final": True,
                    "current_transcript": None,
                }
            ]

        if event_type == "bidi_audio_delta":
            return [
                {
                    "type": "bidi_audio_stream",
                    "audio": event.get("audio"),
                    "format": event.get("format"),
                    "sample_rate": event.get("sample_rate"),
                    "channels": event.get("channels"),
                }
            ]

        if event_type == "bidi_barge_in":
            self._interrupted = True
            return [{"type": "bidi_interruption", "reason": event.get("reason")}]

        if event_type == "bidi_response_stop":
            # A response that ended before the assistant produced anything
            # (tool-only, or barged in immediately) still owes the SPA a start,
            # or the user's words stay buffered and the SPA re-files the
            # previous turn's text as this turn's transcript entry.
            events = self._start_assistant()
            events.append(
                {
                    "type": "bidi_response_complete",
                    "response_id": event.get("response_id") or self._response_id,
                    "stop_reason": "interrupted" if self._interrupted else "complete",
                }
            )
            self._response_id = None
            self._assistant_started = False
            self._interrupted = False
            return events

        if event_type == "bidi_connection_stop":
            return [
                {
                    "type": "bidi_connection_close",
                    "connection_id": event.get("connection_id"),
                    "reason": event.get("reason"),
                }
            ]

        return [event]

    def _start_assistant(self) -> List[Dict[str, Any]]:
        if self._assistant_started:
            return []
        self._assistant_started = True
        return [{"type": "bidi_response_start", "response_id": self._response_id}]


class VoiceAgent(BaseAgent):
    """
    Bidirectional voice agent using AWS Nova Sonic 2.

    Provides:
    - Real-time speech-to-speech via BedrockNovaSonicModel
    - Voice-text continuity (loads previous text chat history)
    - Separate agent_id ("voice") to avoid session state conflicts
    - Configurable voice, sample rate, and model via environment variables

    Usage:
        agent = VoiceAgent(session_id="sess-123", enabled_tools=[...])
        await agent.start()
        await agent.send_audio(audio_base64, sample_rate=16000)
        async for event in agent.receive_events():
            # wire-contract dicts: bidi_audio_stream, bidi_transcript_stream, etc.
    """

    def __init__(self, voice: Optional[str] = None, **kwargs):
        """
        Initialize voice agent.

        Args:
            voice: Nova 2 Sonic voice id (see ``apis.shared.voice_catalog``).
                   Unknown or blank falls back to the NOVA_SONIC_VOICE env var,
                   then "tiffany" — never an id Bedrock would refuse.
            **kwargs: All BaseAgent constructor args
        """
        self._voice = resolve_voice_id(
            voice,
            os.environ.get(EnvVars.NOVA_SONIC_VOICE, Defaults.NOVA_SONIC_VOICE),
        )
        self._bidi_agent: Any = None
        # Nova Sonic bidi_usage events report CUMULATIVE token counts (not deltas).
        # _accumulated_usage stores the latest cumulative snapshot from the stream.
        self._accumulated_usage: dict = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        self._per_turn_usage: List[dict] = []  # Snapshot of usage per completed turn
        self._turn_count: int = 0  # Completed turns (bidi_response_complete)
        self._response_start_count: int = 0  # Started turns (bidi_response_start)
        self._wire = VoiceWireAdapter()
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

            # 1.57 split the provider's audio config: the voice is its own
            # `voice` kwarg, and `audio` carries per-direction sample rates
            # only (channels and format are fixed at mono PCM). Unknown keys
            # are rejected, so the 1.55 five-key dict no longer fits.
            model = NovaSonicModelWithUsageDetails(
                model_id=model_id,
                voice=self._voice,
                audio={
                    "input": {"sample_rate": Defaults.NOVA_SONIC_INPUT_RATE},
                    "output": {"sample_rate": Defaults.NOVA_SONIC_OUTPUT_RATE},
                },
                region=os.environ.get(EnvVars.AWS_REGION, Defaults.AWS_REGION),
            )

            # Build voice-specific system prompt
            voice_prompt = self._build_voice_system_prompt()

            # Load text history for voice-text continuity
            initial_messages = self._load_text_history()

            # Create BidiAgent with separate agent_id.
            #
            # 1.57.2 removed BidiAgent's `session_manager` kwarg (#4698). All
            # it did was register the manager as a hook provider, and BidiAgent
            # still fires the events the manager listens on
            # (AgentInitializedEvent, MessageAddedEvent, BidiAgentStopEvent),
            # so passing it as a hook keeps voice transcripts persisting.
            # Dropping it would not error — voice would just stop saving.
            self._bidi_agent = BidiAgent(
                model=model,
                tools=tools,
                system_prompt=voice_prompt,
                agent_id=Defaults.VOICE_AGENT_ID,
                hooks=[self.session_manager] if self.session_manager else None,
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
        memory_context = getattr(self, "memory_context", None)
        if memory_context:
            base = f"{base}\n\n{memory_context}" if base else memory_context
        voice_addendum = (
            "\n\n## Voice Interaction Guidelines\n"
            "- Keep responses concise and conversational\n"
            "- Avoid long lists or complex formatting (the user is listening)\n"
            "- Use natural speech patterns\n"
            "- Confirm understanding before taking actions\n"
        )
        return base + voice_addendum + self._voice_presentation_line()

    def _voice_presentation_line(self) -> str:
        """One line telling the model which voice it speaks with.

        Amazon's Nova 2 Sonic prompt guidance: languages that conjugate the
        speaker's own gender (Hindi, Portuguese, French, Italian, Spanish) need
        the system prompt to say which form matches the voice, or "I am tired"
        comes out as the wrong one. English never conjugates it, so the line is
        inert for the default voice and costs a few tokens — but it is part of
        the cached prefix, so it is written once per voice, not per turn.
        """
        voice: Optional[NovaSonicVoice] = get_voice(getattr(self, "_voice", None))
        if voice is None:
            return ""
        form = "feminine" if voice.gender == "feminine" else "masculine"
        return (
            f"- You speak with the voice \"{voice.name}\", which sounds {form}; "
            f"in languages that mark the speaker's gender, use the {form} form for yourself\n"
        )

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

        # 1.57 takes raw bytes in an `audio_delta` block; the sample rate is
        # fixed by the model's audio config rather than sent per chunk.
        if sample_rate != Defaults.NOVA_SONIC_INPUT_RATE:
            logger.debug(
                "Voice audio sample_rate=%s differs from the configured input rate %s",
                sample_rate,
                Defaults.NOVA_SONIC_INPUT_RATE,
            )
        await self._bidi_agent.send({
            "audio_delta": {"format": "pcm", "source": {"bytes": base64.b64decode(audio_base64)}},
        })

    async def send_text(self, text: str) -> None:
        """
        Send text input to the voice agent via BidiAgent.send().

        Args:
            text: Text message to send
        """
        if not self._bidi_agent:
            raise RuntimeError("Voice agent not started")

        await self._bidi_agent.send({"text": text})

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

    @property
    def voice_id(self) -> str:
        """The Nova 2 Sonic voice this agent speaks with (always a catalog id)."""
        return self._voice

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

        # Lazy-loaded pricing for real-time cost calculation, fetched once
        # and cached for the session lifetime.
        pricing_state: dict = {"loaded": False, "pricing": None}

        async for event in self._bidi_agent.receive():
            if hasattr(event, "as_dict"):
                raw_dict = event.as_dict()
            else:
                raw_dict = {"type": "unknown", "data": str(event)}

            for event_dict in self._wire.translate(raw_dict):
                yield await self._account_and_enrich(event_dict, pricing_state)

    async def _account_and_enrich(self, event_dict: dict, pricing_state: dict) -> dict:
        """Apply turn and usage accounting to one wire event and return it."""
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

        if event_type == "bidi_usage":
            usage = event_dict.get("usage", event_dict)
            self._apply_usage_snapshot(usage)
            logger.info(f"Voice bidi_usage snapshot: {usage}")
            logger.info(f"Voice usage current: {self._accumulated_usage}")

            # Calculate real-time cost and enrich the event for the client.
            if not pricing_state["loaded"]:
                pricing_state["loaded"] = True
                try:
                    from apis.shared.costs.pricing_config import get_model_pricing
                    pricing_state["pricing"] = await get_model_pricing(self.voice_model_id)
                except Exception as e:
                    logger.debug(f"Voice pricing unavailable: {e}")

            pricing_dict = pricing_state["pricing"]
            if pricing_dict and self._accumulated_usage.get("totalTokens", 0) > 0:
                try:
                    from apis.shared.costs.calculator import CostCalculator
                    total_cost, breakdown = CostCalculator.calculate_voice_cost(
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

        return event_dict

    def _apply_usage_snapshot(self, usage: dict) -> None:
        """Fold one ``bidi_usage`` payload into ``_accumulated_usage``.

        Nova Sonic reports CUMULATIVE totals in each event (not deltas), so we
        replace rather than sum — the speech/text split too, when the provider
        kept it (see NovaSonicModelWithUsageDetails). Shared by the live stream
        and the post-stop drain so the two can never price a session differently.
        """
        for key in ("inputTokens", "outputTokens", "totalTokens"):
            self._accumulated_usage[key] = usage.get(key, self._accumulated_usage[key])
        for row in usage.get("modality_details") or []:
            bucket = {"audio": "speech", "text": "text"}.get(row.get("modality"))
            if not bucket:
                continue
            self._accumulated_usage[f"{bucket}InputTokens"] = int(row.get("input_tokens") or 0)
            self._accumulated_usage[f"{bucket}OutputTokens"] = int(row.get("output_tokens") or 0)

    async def stream_async(
        self,
        message: str,
        session_id: Optional[str] = None,
        files: Optional[List] = None,
        citations: Optional[List] = None,
        original_message: Optional[str] = None,
        interrupt_responses: Optional[List] = None,
        # Accepted only to satisfy the base signature. Voice has no `@`-mention
        # surface, so there is no turn-scoped Agent to record (#756), and no
        # composer to steer from mid-turn.
        turn_agent_id: Optional[str] = None,
        turn_lease: Any = None,
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
                    if not hasattr(event, "as_dict"):
                        continue
                    for event_dict in self._wire.translate(event.as_dict()):
                        event_type = event_dict.get("type", "")
                        if event_type == "bidi_usage":
                            usage = event_dict.get("usage", event_dict)
                            self._apply_usage_snapshot(usage)
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
