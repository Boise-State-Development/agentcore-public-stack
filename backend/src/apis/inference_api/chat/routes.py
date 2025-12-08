"""Chat feature routes

Handles agent execution and SSE streaming.
Implements AgentCore Runtime standard endpoints:
- POST /invocations (required)
- GET /ping (required)
"""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from typing import Optional
import logging
import asyncio
import json

from .models import InvocationRequest, ChatRequest, ChatEvent, GenerateTitleRequest, GenerateTitleResponse
from .service import get_agent, generate_conversation_title
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# Stream timeout configuration (in seconds)
# Prevents hanging streams and resource exhaustion
STREAM_TIMEOUT_SECONDS = 600  # 10 minutes


# ============================================================
# AgentCore Runtime Standard Endpoints (REQUIRED)
# ============================================================

@router.get("/ping")
async def ping():
    """Health check endpoint (required by AgentCore Runtime)"""
    return {"status": "healthy"}


@router.post("/generate-title")
async def generate_title(
    request: GenerateTitleRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Generate a conversation title for a new session.

    This endpoint uses AWS Bedrock Nova Micro to generate a concise,
    descriptive title based on the user's initial message. It's designed
    to be called in parallel with the first chat request.

    The endpoint:
    - Uses JWT authentication to extract user_id
    - Truncates input to ~500 tokens for speed and cost efficiency
    - Calls Nova Micro with temperature=0.3 for consistent output
    - Updates session metadata both locally and in cloud
    - Returns fallback title "New Conversation" on error

    Args:
        request: GenerateTitleRequest with session_id and user input
        current_user: User from JWT token (injected by dependency)

    Returns:
        GenerateTitleResponse with generated title and session_id
    """
    user_id = current_user.user_id
    logger.info(f"Title generation request - Session: {request.session_id}, User: {user_id}")

    try:
        # Generate title using Nova Micro
        title = await generate_conversation_title(
            session_id=request.session_id,
            user_id=user_id,
            user_input=request.input
        )

        return GenerateTitleResponse(
            title=title,
            session_id=request.session_id
        )

    except Exception as e:
        logger.error(f"Error in generate_title endpoint: {e}")
        # Return fallback instead of raising exception
        # Title generation failures shouldn't break the user experience
        return GenerateTitleResponse(
            title="New Conversation",
            session_id=request.session_id
        )


@router.post("/invocations")
async def invocations(
    request: InvocationRequest,
    current_user: User = Depends(get_current_user)
):
    """
    AgentCore Runtime standard invocation endpoint (required)

    Supports user-specific tool filtering and SSE streaming.
    Creates/caches agent instance per session + tool configuration.
    Uses the authenticated user's ID from the JWT token.
    """
    input_data = request
    user_id = current_user.user_id
    logger.info(f"Invocation request - Session: {input_data.session_id}, User: {user_id}")
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
            user_id=user_id,
            enabled_tools=input_data.enabled_tools,
            model_id=input_data.model_id,
            temperature=input_data.temperature,
            system_prompt=input_data.system_prompt,
            caching_enabled=input_data.caching_enabled
        )

        # Stream response from agent as SSE (with optional files)
        # Note: Compression is handled by GZipMiddleware if configured in main.py
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

@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Legacy chat stream endpoint (for backward compatibility)
    Uses default tools (all available) if enabled_tools not specified
    Uses the authenticated user's ID from the JWT token.
    """
    user_id = current_user.user_id
    logger.info(f"Legacy chat request - Session: {request.session_id}, User: {user_id}, Message: {request.message[:50]}...")

    try:
        # Get agent instance (with or without tool filtering)
        agent = get_agent(
            session_id=request.session_id,
            user_id=user_id,
            enabled_tools=request.enabled_tools  # May be None (use all tools)
        )

        # Wrap stream to ensure flush on disconnect and prevent further processing
        async def stream_with_cleanup():
            stream_iterator = agent.stream_async(request.message, session_id=request.session_id)

            try:
                # Add timeout to prevent hanging streams
                async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
                    async for event in stream_iterator:
                        yield event

            except asyncio.TimeoutError:
                # Stream exceeded timeout - send error and cleanup
                logger.error(
                    f"⏱️ Stream timeout ({STREAM_TIMEOUT_SECONDS}s) for session {request.session_id}"
                )

                # Send timeout error event to client
                error_data = {
                    "type": "error",
                    "message": f"Stream timeout - request exceeded {STREAM_TIMEOUT_SECONDS // 60} minutes"
                }
                yield f"data: {json.dumps(error_data)}\n\n"

            except asyncio.CancelledError:
                # Client disconnected (e.g., stop button clicked)
                logger.warning(f"⚠️ Client disconnected during streaming for session {request.session_id}")

                # Mark session manager as cancelled to prevent further tool execution
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

                # Re-raise to properly close the connection
                raise

            except Exception as e:
                # Log unexpected errors
                logger.error(f"Error during streaming for session {request.session_id}: {e}")
                raise

            finally:
                # Cleanup: Flush buffered messages and close stream iterator
                # This runs on both success and error paths
                if hasattr(agent.session_manager, 'flush'):
                    try:
                        agent.session_manager.flush()
                        logger.info(f"💾 Flushed buffered messages for session {request.session_id}")
                    except Exception as flush_error:
                        logger.error(f"Failed to flush session {request.session_id}: {flush_error}")

                # Close the stream iterator if possible
                if hasattr(stream_iterator, 'aclose'):
                    try:
                        await stream_iterator.aclose()
                    except Exception as close_error:
                        logger.debug(f"Failed to close stream iterator: {close_error}")

        # Stream response from agent
        # Note: Compression is handled by GZipMiddleware if configured in main.py
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


@router.post("/multimodal")
async def chat_multimodal(
    request: ChatRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Stream chat response with multimodal input (files)

    For now, just echoes the message and mentions files.
    Will be replaced with actual Strands Agent execution.
    Uses the authenticated user's ID from the JWT token.
    """
    user_id = current_user.user_id
    logger.info(f"Multimodal chat request - Session: {request.session_id}, User: {user_id}")
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

