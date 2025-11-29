"""Chat feature routes

Handles agent execution and SSE streaming.
Implements AgentCore Runtime standard endpoints:
- POST /invocations (required)
- GET /ping (required)
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
import logging
import asyncio
import json

from models.schemas import ChatRequest, ChatEvent
from .models import InvocationRequest
from .service import get_agent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# ============================================================
# AgentCore Runtime Standard Endpoints (REQUIRED)
# ============================================================

@router.get("/ping")
async def ping():
    """Health check endpoint (required by AgentCore Runtime)"""
    return {"status": "healthy"}


@router.post("/invocations")
async def invocations(request: InvocationRequest):
    """
    AgentCore Runtime standard invocation endpoint (required)

    Supports user-specific tool filtering and SSE streaming.
    Creates/caches agent instance per session + tool configuration.
    """
    input_data = request.input
    logger.info(f"Invocation request - Session: {input_data.session_id}, User: {input_data.user_id}")
    logger.info(f"Message: {input_data.message[:50]}...")

    if input_data.enabled_tools:
        logger.info(f"Enabled tools ({len(input_data.enabled_tools)}): {input_data.enabled_tools}")

    if input_data.files:
        logger.info(f"Files attached: {len(input_data.files)} files")
        for file in input_data.files:
            logger.info(f"  - {file.filename} ({file.content_type})")

    try:
        # Get agent instance with user-specific configuration
        # AgentCore Memory tracks preferences across sessions per user_id
        agent = get_agent(
            session_id=input_data.session_id,
            user_id=input_data.user_id,
            enabled_tools=input_data.enabled_tools,
            model_id=input_data.model_id,
            temperature=input_data.temperature,
            system_prompt=input_data.system_prompt,
            caching_enabled=input_data.caching_enabled
        )

        # Stream response from agent as SSE (with optional files)
        return StreamingResponse(
            agent.stream_async(
                input_data.message,
                session_id=input_data.session_id,
                files=input_data.files
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": input_data.session_id
            }
        )

    except Exception as e:
        logger.error(f"Error in invocations: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Agent processing failed: {str(e)}"
        )


# ============================================================
# Legacy Endpoints (for backward compatibility)
# ============================================================

@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    Legacy chat stream endpoint (for backward compatibility)
    Uses default tools (all available) if enabled_tools not specified
    """
    logger.info(f"Legacy chat request - Session: {request.session_id}, Message: {request.message[:50]}...")

    try:
        # Get agent instance (with or without tool filtering)
        agent = get_agent(
            session_id=request.session_id,
            enabled_tools=request.enabled_tools  # May be None (use all tools)
        )

        # Wrap stream to ensure flush on disconnect and prevent further processing
        async def stream_with_cleanup():
            client_disconnected = False
            stream_iterator = agent.stream_async(request.message, session_id=request.session_id)

            try:
                async for event in stream_iterator:
                    if client_disconnected:
                        # Client already disconnected, don't yield more events
                        break
                    yield event
            except asyncio.CancelledError:
                # Client disconnected (e.g., stop button clicked)
                client_disconnected = True
                logger.warning(f"⚠️ Client disconnected during streaming for session {request.session_id}")

                # Mark session manager as cancelled to prevent further message buffering
                if hasattr(agent.session_manager, 'cancelled'):
                    agent.session_manager.cancelled = True
                    logger.info(f"🚫 Session manager marked as cancelled - will ignore further messages")

                # Add final assistant message with stop reason
                stop_message = {
                    "role": "assistant",
                    "content": [{"text": "Session stopped by user"}]
                }
                if hasattr(agent.session_manager, 'pending_messages'):
                    agent.session_manager.pending_messages.append(stop_message)
                    logger.info(f"📝 Added stop message to pending buffer")

                # Flush buffered messages including stop message
                if hasattr(agent.session_manager, 'flush'):
                    try:
                        agent.session_manager.flush()
                        logger.info(f"💾 Flushed buffered messages with stop message after client disconnect")
                    except Exception as flush_error:
                        logger.error(f"Failed to flush on disconnect: {flush_error}")

                raise  # Re-raise to properly close the connection
            except Exception as e:
                logger.error(f"Error during streaming: {e}")
                # Flush on error as well
                if hasattr(agent.session_manager, 'flush'):
                    try:
                        agent.session_manager.flush()
                        logger.info(f"💾 Flushed buffered messages after error")
                    except Exception as flush_error:
                        logger.error(f"Failed to flush on error: {flush_error}")
                raise
            finally:
                # Cleanup: close the stream iterator if possible
                if hasattr(stream_iterator, 'aclose'):
                    try:
                        await stream_iterator.aclose()
                    except Exception:
                        pass

        # Stream response from agent
        return StreamingResponse(
            stream_with_cleanup(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": request.session_id
            }
        )

    except Exception as e:
        logger.error(f"Error in chat_stream: {e}")

        async def error_generator():
            error_data = {
                "type": "error",
                "message": str(e)
            }
            yield f"data: {json.dumps(error_data)}\n\n"

        return StreamingResponse(
            error_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no"
            }
        )


@router.post("/chat/multimodal")
async def chat_multimodal(request: ChatRequest):
    """
    Stream chat response with multimodal input (files)

    For now, just echoes the message and mentions files.
    Will be replaced with actual Strands Agent execution.
    """
    logger.info(f"Multimodal chat request - Session: {request.session_id}")
    logger.info(f"Message: {request.message[:50]}...")
    if request.files:
        logger.info(f"Files: {len(request.files)} uploaded")
        for file in request.files:
            logger.info(f"  - {file.filename} ({file.content_type})")

    async def event_generator():
        try:
            # Send init event
            event = ChatEvent(
                type="init",
                content="Processing multimodal input",
                metadata={"session_id": request.session_id, "file_count": len(request.files or [])}
            )
            yield f"data: {event.to_json()}\n\n"
            await asyncio.sleep(0.2)

            # Echo message
            response_text = f"Received message: '{request.message}'"
            if request.files:
                response_text += f" and {len(request.files)} file(s): "
                response_text += ", ".join([f.filename for f in request.files])

            for word in response_text.split():
                event = ChatEvent(
                    type="text",
                    content=word + " "
                )
                yield f"data: {event.to_json()}\n\n"
                await asyncio.sleep(0.05)

            # Complete
            event = ChatEvent(
                type="complete",
                content="Multimodal processing complete"
            )
            yield f"data: {event.to_json()}\n\n"

        except Exception as e:
            logger.error(f"Error in multimodal event_generator: {e}")
            error_event = ChatEvent(
                type="error",
                content=str(e)
            )
            yield f"data: {error_event.to_json()}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

