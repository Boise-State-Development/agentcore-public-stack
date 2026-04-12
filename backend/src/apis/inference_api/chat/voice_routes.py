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
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

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


@router.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket):
    """
    Bidirectional voice streaming endpoint.

    Query params:
        session_id: Session identifier (optional, auto-generated if missing)
        token: JWT bearer token for authentication
        enabled_tools: JSON array of tool IDs (optional)
    """
    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    token = websocket.query_params.get("token", "")
    enabled_tools_raw = websocket.query_params.get("enabled_tools", "")
    voice_agent = None

    try:
        # Parse enabled tools
        enabled_tools = None
        if enabled_tools_raw:
            try:
                enabled_tools = json.loads(enabled_tools_raw)
            except json.JSONDecodeError:
                logger.warning(f"Invalid enabled_tools JSON: {enabled_tools_raw}")

        # Authenticate
        user_info = _extract_user_from_token(token)
        if not user_info:
            await websocket.close(code=4001, reason="Authentication required")
            return

        user_id = user_info["user_id"]
        auth_token = user_info["raw_token"]

        # Accept the WebSocket connection
        await websocket.accept()
        logger.info(f"Voice WebSocket connected: session={session_id}, user={user_id}")

        # Wait for initial config message (supplements query params)
        try:
            first_msg = await asyncio.wait_for(
                websocket.receive_json(), timeout=10.0
            )
            if first_msg.get("type") == "config":
                # Config can override session params
                session_id = first_msg.get("session_id", session_id)
                if first_msg.get("auth_token"):
                    auth_token = first_msg["auth_token"]
                if first_msg.get("enabled_tools"):
                    enabled_tools = first_msg["enabled_tools"]
                logger.info(f"Voice config received: session={session_id}")
        except asyncio.TimeoutError:
            logger.warning("No config message received within 10s, using query params")
        except Exception as e:
            logger.warning(f"Error reading config message: {e}")

        # Create VoiceAgent
        VoiceAgent = _get_voice_agent_class()
        voice_agent = VoiceAgent(
            session_id=session_id,
            user_id=user_id,
            auth_token=auth_token,
            enabled_tools=enabled_tools,
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
        logger.info(f"Voice WebSocket disconnected: session={session_id}")
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
        if voice_agent:
            try:
                await voice_agent.stop()
            except BaseException as e:
                logger.debug(f"Voice agent stop during cleanup: {type(e).__name__}: {e}")
        try:
            await websocket.close()
        except BaseException:
            pass
        logger.info(f"Voice session cleaned up: {session_id}")


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
                logger.info(f"Client requested stop: session={session_id}")
                break

            else:
                logger.debug(f"Unknown message type: {msg_type}")

    except WebSocketDisconnect:
        logger.info(f"Client disconnected (receive): session={session_id}")
    except asyncio.CancelledError:
        logger.debug(f"Receive task cancelled: session={session_id}")
        raise


async def _send_to_client(
    websocket: WebSocket, voice_agent: Any, session_id: str
) -> None:
    """Stream events from voice agent to client.

    VoiceAgent.stream_async() yields dicts from BidiAgent.receive() — each dict
    has a 'type' field (e.g. 'bidi_audio_stream', 'bidi_transcript_stream',
    'bidi_response_complete', etc.).
    """
    try:
        async for event in voice_agent.stream_async(""):
            try:
                if isinstance(event, dict):
                    await websocket.send_json(event)
                else:
                    await websocket.send_json({
                        "type": "bidi_event",
                        "data": str(event),
                    })
            except WebSocketDisconnect:
                logger.info(f"Client disconnected during send: session={session_id}")
                return
            except Exception as e:
                logger.warning(f"Error sending event to client: {e}")

    except asyncio.CancelledError:
        logger.debug(f"Send task cancelled: session={session_id}")
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
        logger.error(f"Error force-stopping session {session_id}: {e}")

    _active_sessions.pop(session_id, None)
    return {"status": "stopped", "session_id": session_id}
