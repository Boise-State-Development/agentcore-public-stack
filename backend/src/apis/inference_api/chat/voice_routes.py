"""
WebSocket voice route for bidirectional speech-to-speech interaction.

Exposes VoiceAgent via WebSocket for real-time audio streaming with
AWS Nova Sonic 2. Adapted from the sample-strands-agent-with-agentcore
voice router pattern.

Protocol:
    Client → Server:
        {"type": "config", "session_id": "...", "auth_token": "...", ...}  (first message)
        {"type": "bidi_audio_input", "audio": "<base64>", "sample_rate": 16000}
        {"type": "bidi_text_input", "text": "..."}
        {"type": "ping"}
        {"type": "stop"}

    Server → Client:
        {"type": "bidi_connection_start", "connection_id": "...", "status": "connected"}
        {"type": "bidi_error", "message": "..."}
        Agent stream events (audio, transcripts, tool use, etc.)
"""

import asyncio
import json
import jwt
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from apis.shared.sessions.metadata import get_session_metadata, store_session_metadata
from apis.shared.sessions.models import SessionMetadata

logger = logging.getLogger(__name__)


def _sanitize_log(value: str) -> str:
    """Strip newlines and carriage returns to prevent log injection."""
    return str(value).replace("\n", "").replace("\r", "")

router = APIRouter(tags=["voice"])

# Track active voice sessions for debugging
_active_sessions: Dict[str, Any] = {}

# Lazy import to avoid loading bidi deps at module level
_VoiceAgentClass = None


def _get_voice_agent_class():
    """Lazily import VoiceAgent to avoid import errors when bidi not installed."""
    global _VoiceAgentClass
    if _VoiceAgentClass is None:
        from agents.main_agent.voice_agent import VoiceAgent
        _VoiceAgentClass = VoiceAgent
    return _VoiceAgentClass


def _extract_user_from_token(token: str) -> Optional[Dict[str, str]]:
    """
    Extract user claims from JWT token (trusted — no signature verification).

    Same pattern as get_current_user_trusted in auth/dependencies.py.
    WebSocket connections can't use Depends() so we handle auth manually.
    """
    if not token:
        return None

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        user_id = payload.get("sub")
        if not user_id:
            return None
        return {
            "user_id": str(user_id),
            "email": payload.get("email") or payload.get("preferred_username") or "",
            "raw_token": token,
        }
    except jwt.DecodeError as e:
        logger.warning(f"Failed to decode voice auth token: {e}")
        return None


async def _ensure_session_metadata(session_id: str, user_id: str) -> None:
    """Create session metadata entry if one doesn't already exist.

    This makes the voice session visible in the conversations side nav.
    If the session started as a text chat, existing metadata is preserved.
    """
    try:
        existing = await get_session_metadata(session_id, user_id)
        if existing:
            logger.debug(f"Session metadata already exists for {_sanitize_log(session_id)}")
            return

        now = datetime.now(timezone.utc).isoformat()
        metadata = SessionMetadata(
            session_id=session_id,
            user_id=user_id,
            title="Voice Conversation",
            status="active",
            created_at=now,
            last_message_at=now,
            message_count=0,
            starred=False,
            tags=[],
            preferences=None,
        )
        await store_session_metadata(session_id=session_id, user_id=user_id, session_metadata=metadata)
        logger.info(f"Created session metadata for voice session {_sanitize_log(session_id)}")
    except Exception as e:
        logger.error(f"Failed to create session metadata for {_sanitize_log(session_id)}: {e}", exc_info=True)


async def _finalize_voice_session(session_id: str, user_id: str, voice_agent: Any) -> None:
    """Update session metadata and store cost/token data after voice session ends.

    Called in the finally block of voice_stream to persist usage metrics.
    """
    # Use response_start_count as fallback when turns are interrupted before completion
    completed_turns = getattr(voice_agent, "turn_count", 0)
    started_turns = getattr(voice_agent, "response_start_count", 0)
    effective_turns = max(completed_turns, started_turns)

    logger.info(
        f"Finalizing voice session {_sanitize_log(session_id)}: "
        f"completed_turns={completed_turns}, started_turns={started_turns}, "
        f"effective_turns={effective_turns}, "
        f"accumulated_usage={getattr(voice_agent, 'accumulated_usage', 'N/A')}, "
        f"per_turn_usage_count={len(getattr(voice_agent, 'per_turn_usage', []))}"
    )
    try:
        # Update session metadata with final turn count
        existing = await get_session_metadata(session_id, user_id)
        if existing:
            now = datetime.now(timezone.utc).isoformat()
            updated = SessionMetadata(
                session_id=session_id,
                user_id=user_id,
                title=existing.title,
                status=existing.status,
                created_at=existing.created_at,
                last_message_at=now,
                message_count=existing.message_count + effective_turns,
                starred=existing.starred,
                tags=existing.tags,
                preferences=existing.preferences,
            )
            await store_session_metadata(session_id=session_id, user_id=user_id, session_metadata=updated)
            logger.info(f"Updated voice session metadata: turns={effective_turns}, session={_sanitize_log(session_id)}")
    except Exception as e:
        logger.error(f"Failed to update session metadata for {_sanitize_log(session_id)}: {e}", exc_info=True)

    # Store metadata for each assistant message in the voice session.
    # BidiAgent may split responses into multiple messages, so we can't assume
    # a strict user/assistant alternating pattern. Instead, read the actual messages
    # from AgentCore Memory and find which indices are assistant messages.
    try:
        import asyncio
        from agents.main_agent.config.constants import Defaults

        accumulated_usage = getattr(voice_agent, "accumulated_usage", {})
        has_usage = (accumulated_usage.get("inputTokens", 0) + accumulated_usage.get("outputTokens", 0)) > 0

        if not has_usage:
            logger.info(f"No voice usage to record metadata for session {_sanitize_log(session_id)}")
            return

        from apis.app_api.messages.models import Attribution, MessageMetadata, ModelInfo, TokenUsage
        from apis.app_api.sessions.services.metadata import store_message_metadata

        model_id = getattr(voice_agent, "voice_model_id", "amazon.nova-2-sonic-v1:0")
        model_info = ModelInfo(
            model_id=model_id,
            model_name="Nova Sonic 2",
            provider="bedrock",
        )

        # Read actual messages from AgentCore Memory to find assistant indices
        assistant_indices = []
        try:
            session_manager = voice_agent.session_manager
            if hasattr(session_manager, "list_messages"):
                messages = await asyncio.to_thread(
                    session_manager.list_messages,
                    session_id,
                    Defaults.VOICE_AGENT_ID,
                )
                for idx, msg in enumerate(messages or []):
                    inner = getattr(msg, "message", msg)
                    role = inner.get("role") if isinstance(inner, dict) else getattr(inner, "role", None)
                    if role == "assistant":
                        assistant_indices.append(idx)
                logger.info(f"Voice session messages: {len(messages or [])} total, assistant at indices {assistant_indices}")
        except Exception as msg_err:
            logger.warning(f"Could not read voice messages for metadata: {msg_err}")

        if not assistant_indices:
            # Fallback: store a single record at index 1 (most common position)
            assistant_indices = [1]
            logger.info("No assistant messages found, using fallback index [1]")

        # Get pricing
        pricing = None
        try:
            from apis.app_api.costs.calculator import CostCalculator
            from apis.app_api.costs.pricing_config import get_model_pricing

            pricing = await get_model_pricing(model_id)
        except Exception as cost_err:
            logger.debug(f"Cost calculation unavailable for voice: {cost_err}")

        # Store cumulative session usage on the LAST assistant message only.
        # Nova Sonic reports cumulative totals for the whole connection, so
        # splitting across messages would be misleading. The last message
        # carries the full session cost; earlier messages get no badge.
        last_idx = assistant_indices[-1]
        input_tokens = accumulated_usage.get("inputTokens", 0)
        output_tokens = accumulated_usage.get("outputTokens", 0)
        total_tokens = input_tokens + output_tokens

        token_usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

        attribution = Attribution(
            user_id=user_id,
            session_id=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        cost = None
        if pricing and total_tokens > 0:
            try:
                total_cost, breakdown = CostCalculator.calculate_message_cost(accumulated_usage, pricing)
                cost = {
                    "total": total_cost,
                    "inputCost": breakdown.input_cost,
                    "outputCost": breakdown.output_cost,
                    "cacheReadCost": breakdown.cache_read_cost,
                    "cacheWriteCost": breakdown.cache_write_cost,
                }
            except Exception:
                pass

        message_metadata = MessageMetadata(
            token_usage=token_usage,
            model_info=model_info,
            attribution=attribution,
            cost=cost,
        )

        await store_message_metadata(
            session_id=session_id,
            user_id=user_id,
            message_id=f"voice:{last_idx}",
            message_metadata=message_metadata,
        )

        logger.info(
            f"Stored voice metadata on last assistant message (index {last_idx}), "
            f"usage={accumulated_usage}, session={_sanitize_log(session_id)}"
        )
    except Exception as e:
        logger.error(f"Failed to store voice cost metadata for {_sanitize_log(session_id)}: {e}", exc_info=True)


def _get_param_from_request(websocket: WebSocket, header_suffix: str, query_param: Optional[str]) -> Optional[str]:
    """Extract param from AgentCore custom header (cloud) or query param (local)."""
    header_name = f"x-amzn-bedrock-agentcore-runtime-custom-{header_suffix}"
    custom_header = websocket.headers.get(header_name)
    if custom_header:
        return custom_header
    return query_param


def _get_enabled_tools_from_request(websocket: WebSocket, query_param: Optional[str]) -> Optional[list]:
    """Extract enabled_tools from AgentCore custom header or query param."""
    tools_json = _get_param_from_request(websocket, "enabled-tools", query_param)
    if not tools_json:
        return None
    try:
        return json.loads(tools_json)
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid enabled_tools JSON: {_sanitize_log(str(e))}")
        return None


@router.websocket("/voice/stream")
async def voice_stream(
    websocket: WebSocket,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    enabled_tools: Optional[str] = None,
    token: Optional[str] = None,
):
    """
    Bidirectional voice streaming endpoint.

    Supports two connection modes:

    **AgentCore (deployed):** Browser connects via
    ``wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<ARN>/ws``.
    Auth is handled by AgentCore's JWT Authorizer at the proxy layer.
    The bearer token for user-claim extraction arrives in the first
    ``config`` message sent by the client after connection opens.

    **Local dev:** Browser connects directly to
    ``ws://localhost:8001/voice/stream``. Session ID and token are
    plain query params; the config message supplements them.
    """
    # Accept immediately — AgentCore validates auth at the proxy layer;
    # user claims are extracted from the config message after accept.
    await websocket.accept()

    # Resolve params: AgentCore custom header → query param → default
    session_id = _get_param_from_request(websocket, "session-id", session_id)
    user_id = _get_param_from_request(websocket, "user-id", user_id)
    enabled_tools_list = _get_enabled_tools_from_request(websocket, enabled_tools)
    auth_token = _get_param_from_request(websocket, "auth-token", token) or ""

    # Always read config message from client (sent on WebSocket open).
    # Required for auth_token in AgentCore mode and supplements any
    # missing params (AgentCore proxy may not forward all query params).
    try:
        first_msg = await asyncio.wait_for(
            websocket.receive_json(), timeout=10.0
        )
        if first_msg.get("type") == "config":
            session_id = first_msg.get("session_id") or session_id
            user_id = first_msg.get("user_id") or user_id
            enabled_tools_list = first_msg.get("enabled_tools") or enabled_tools_list
            auth_token = first_msg.get("auth_token") or auth_token
            logger.info(f"Voice config received from client message")
    except asyncio.TimeoutError:
        logger.warning("No config message received within 10s, using query params")
    except Exception as e:
        logger.warning(f"Error reading config message: {e}")

    # Generate session_id if not provided by any source
    if not session_id:
        session_id = str(uuid.uuid4())
        logger.info(f"Generated new voice session ID: {_sanitize_log(session_id)}")

    # Extract user from token (query param or config message)
    if not user_id and auth_token:
        user_info = _extract_user_from_token(auth_token)
        if user_info:
            user_id = user_info["user_id"]

    if not user_id:
        await websocket.send_json({"type": "bidi_error", "message": "Authentication required"})
        await websocket.close(code=4001, reason="Authentication required")
        return

    logger.info(
        f"Voice WebSocket connected: session={_sanitize_log(session_id)}, "
        f"user={_sanitize_log(user_id)}, tools={len(enabled_tools_list or [])}, "
        f"auth_token={'present' if auth_token else 'missing'}"
    )

    voice_agent = None

    try:
        # Create VoiceAgent
        VoiceAgent = _get_voice_agent_class()
        voice_agent = VoiceAgent(
            session_id=session_id,
            user_id=user_id,
            auth_token=auth_token,
            enabled_tools=enabled_tools_list,
        )

        _active_sessions[session_id] = voice_agent

        # Send connection confirmation
        await websocket.send_json({
            "type": "bidi_connection_start",
            "connection_id": session_id,
            "status": "connected",
        })

        # Start the voice agent
        await voice_agent.start()

        # Create session metadata so voice sessions appear in the side nav
        await _ensure_session_metadata(session_id, user_id)

        # Run bidirectional communication
        receive_task = asyncio.create_task(
            _receive_from_client(websocket, voice_agent, session_id)
        )
        send_task = asyncio.create_task(
            _send_to_client(websocket, voice_agent, session_id)
        )

        done, pending = await asyncio.wait(
            [receive_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Check for task exceptions
        for task in done:
            if task.exception():
                logger.error(f"Voice task error: {task.exception()}")

    except WebSocketDisconnect:
        logger.info(f"Voice WebSocket disconnected: session={_sanitize_log(session_id)}")
    except Exception as e:
        logger.error(f"Voice stream error: {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "bidi_error",
                "message": str(e),
            })
        except Exception:
            pass
    finally:
        # Cleanup — catch BaseException since CancelledError escapes Exception in 3.12
        _active_sessions.pop(session_id, None)
        if voice_agent and user_id:
            # 1. Stop BidiAgent first — flushes stream, emits final events including usage
            try:
                await voice_agent.stop()
            except BaseException as e:
                logger.debug(f"Voice agent stop: {type(e).__name__}: {e}")
            # 2. Drain any remaining queued events (captures final bidi_usage
            #    that weren't consumed because _send_to_client was cancelled)
            try:
                await voice_agent.drain_remaining_events(timeout=3.0)
            except BaseException as e:
                logger.debug(f"Voice event drain: {type(e).__name__}: {e}")
            # 3. NOW finalize with complete accumulated usage data
            try:
                await _finalize_voice_session(session_id, user_id, voice_agent)
            except BaseException as e:
                logger.debug(f"Voice session finalization error: {type(e).__name__}: {e}")
        try:
            await websocket.close()
        except BaseException:
            pass
        logger.info(f"Voice session cleaned up: {_sanitize_log(session_id)}")


async def _receive_from_client(
    websocket: WebSocket, voice_agent: Any, session_id: str
) -> None:
    """Receive messages from client and dispatch to voice agent."""
    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type", "")

            if msg_type == "bidi_audio_input":
                audio = msg.get("audio", "")
                sample_rate = msg.get("sample_rate", 16000)
                await voice_agent.send_audio(audio, sample_rate)

            elif msg_type == "bidi_text_input":
                text = msg.get("text", "")
                if text:
                    await voice_agent.send_text(text)

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif msg_type == "stop":
                logger.info(f"Client requested stop: session={_sanitize_log(session_id)}")
                break

            else:
                logger.debug(f"Unknown message type: {_sanitize_log(msg_type)}")

    except WebSocketDisconnect:
        logger.info(f"Client disconnected (receive): session={_sanitize_log(session_id)}")
    except asyncio.CancelledError:
        logger.debug(f"Receive task cancelled: session={_sanitize_log(session_id)}")
        raise


async def _send_to_client(
    websocket: WebSocket, voice_agent: Any, session_id: str
) -> None:
    """Stream events from voice agent to client.

    VoiceAgent.receive_events() yields dicts from BidiAgent.receive() — each dict
    has a 'type' field (e.g. 'bidi_audio_stream', 'bidi_transcript_stream',
    'bidi_response_complete', etc.).
    """
    try:
        async for event in voice_agent.receive_events():
            try:
                if isinstance(event, dict):
                    await websocket.send_json(event)
                else:
                    await websocket.send_json({
                        "type": "bidi_event",
                        "data": str(event),
                    })
            except WebSocketDisconnect:
                logger.info(f"Client disconnected during send: session={_sanitize_log(session_id)}")
                return
            except Exception as e:
                logger.warning(f"Error sending event to client: {e}")

    except asyncio.CancelledError:
        logger.debug(f"Send task cancelled: session={_sanitize_log(session_id)}")
        raise
    except Exception as e:
        logger.error(f"Error in send_to_client: {e}")


# --- Debug endpoints ---

@router.get("/voice/sessions")
async def list_voice_sessions():
    """List active voice sessions (for debugging)."""
    return {
        "active_sessions": list(_active_sessions.keys()),
        "count": len(_active_sessions),
    }


@router.delete("/voice/sessions/{session_id}")
async def stop_voice_session(session_id: str):
    """Force-stop a voice session (for debugging)."""
    agent = _active_sessions.get(session_id)
    if not agent:
        return {"status": "not_found", "session_id": session_id}

    try:
        await agent.stop()
    except Exception as e:
        logger.error(f"Error force-stopping session {_sanitize_log(session_id)}: {e}")

    _active_sessions.pop(session_id, None)
    return {"status": "stopped", "session_id": session_id}


# =============================================================================
# /ws alias for AgentCore Runtime
# AgentCore Runtime routes WebSocket requests to /ws on the container.
# This delegates to /voice/stream for cloud deployment compatibility.
# =============================================================================

@router.websocket("/ws")
async def ws_stream(
    websocket: WebSocket,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    enabled_tools: Optional[str] = None,
    token: Optional[str] = None,
):
    """WebSocket endpoint for AgentCore Runtime (cloud mode).

    AgentCore Runtime expects containers to implement WebSocket at /ws
    path on port 8080. This endpoint delegates to voice_stream.
    """
    await voice_stream(
        websocket=websocket,
        session_id=session_id,
        user_id=user_id,
        enabled_tools=enabled_tools,
        token=token,
    )
