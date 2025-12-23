"""Chat feature routes

Application-specific chat endpoints moved from inference_api to keep
AgentCore Runtime API clean. These endpoints handle:
- Conversation title generation
- Legacy chat streaming
- Multimodal chat input
"""

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import StreamingResponse
import logging
import asyncio
import json
from typing import AsyncGenerator

# Import models and services from inference_api (shared code)
from apis.inference_api.chat.models import (
    ChatRequest,
    ChatEvent,
    GenerateTitleRequest,
    GenerateTitleResponse
)
from apis.inference_api.chat.service import get_agent, generate_conversation_title
from apis.inference_api.chat.routes import stream_conversational_message
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.errors import (
    StreamErrorEvent,
    ErrorCode,
    ConversationalErrorEvent,
    build_conversational_error_event,
)
from apis.shared.quota import (
    get_quota_checker,
    is_quota_enforcement_enabled,
    build_quota_exceeded_event,
    build_quota_warning_event,
)
from agents.strands_agent.session.session_factory import SessionFactory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# Stream timeout configuration (in seconds)
# Prevents hanging streams and resource exhaustion
STREAM_TIMEOUT_SECONDS = 600  # 10 minutes


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


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Legacy chat stream endpoint (for backward compatibility)
    Uses default tools (all available) if enabled_tools not specified
    Uses the authenticated user's ID from the JWT token.

    Quota enforcement (when enabled via ENABLE_QUOTA_ENFORCEMENT=true):
    - Checks user quota before processing
    - Streams quota_exceeded as assistant message if quota exceeded (better UX)
    - Injects quota_warning event into stream if approaching limit
    """
    user_id = current_user.user_id
    logger.info(f"Legacy chat request - Session: {request.session_id}, User: {user_id}, Message: {request.message[:50]}...")

    # Check quota if enforcement is enabled
    quota_warning_event = None
    quota_exceeded_event = None
    if is_quota_enforcement_enabled():
        try:
            quota_checker = get_quota_checker()
            quota_result = await quota_checker.check_quota(
                user=current_user,
                session_id=request.session_id
            )

            if not quota_result.allowed:
                # Quota exceeded - stream as SSE instead of 429 for better UX
                logger.warning(f"Quota exceeded for user {user_id}: {quota_result.message}")
                quota_exceeded_event = build_quota_exceeded_event(quota_result)
            else:
                # Check for warning level
                quota_warning_event = build_quota_warning_event(quota_result)
                if quota_warning_event:
                    logger.info(f"Quota warning for user {user_id}: {quota_result.warning_level}")

        except Exception as e:
            # Log error but don't block request - fail open for quota errors
            logger.error(f"Error checking quota for user {user_id}: {e}", exc_info=True)

    # If quota exceeded, stream the quota exceeded message instead of agent response
    if quota_exceeded_event:
        return StreamingResponse(
            stream_conversational_message(
                message=quota_exceeded_event.message,
                stop_reason="quota_exceeded",
                metadata_event=quota_exceeded_event,
                session_id=request.session_id,
                user_id=user_id,
                user_input=request.message
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": request.session_id
            }
        )

    try:
        # Get agent instance (with or without tool filtering)
        agent = get_agent(
            session_id=request.session_id,
            user_id=user_id,
            enabled_tools=request.enabled_tools  # May be None (use all tools)
        )

        # Wrap stream to ensure flush on disconnect and prevent further processing
        async def stream_with_cleanup():
            # Yield quota warning event first if applicable
            if quota_warning_event:
                yield quota_warning_event.to_sse_format()
            stream_iterator = agent.stream_async(request.message, session_id=request.session_id)

            try:
                # Add timeout to prevent hanging streams
                async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
                    async for event in stream_iterator:
                        yield event

            except asyncio.TimeoutError:
                # Stream exceeded timeout - send as conversational message
                logger.error(
                    f"⏱️ Stream timeout ({STREAM_TIMEOUT_SECONDS}s) for session {request.session_id}"
                )

                # Build conversational timeout error
                timeout_error = Exception(f"Stream processing time exceeded {STREAM_TIMEOUT_SECONDS} seconds")
                error_event = build_conversational_error_event(
                    code=ErrorCode.TIMEOUT,
                    error=timeout_error,
                    session_id=request.session_id,
                    recoverable=True
                )

                # Stream timeout error as assistant message
                yield f"event: message_start\ndata: {{\"role\": \"assistant\"}}\n\n"
                yield f"event: content_block_start\ndata: {{\"contentBlockIndex\": 0, \"type\": \"text\"}}\n\n"
                yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': error_event.message})}\n\n"

                yield f"event: content_block_stop\ndata: {{\"contentBlockIndex\": 0}}\n\n"
                yield f"event: message_stop\ndata: {{\"stopReason\": \"error\"}}\n\n"
                yield error_event.to_sse_format()
                yield "event: done\ndata: {}\n\n"

                # Persist timeout error to session
                try:
                    from strands.types.session import SessionMessage
                    session_manager = SessionFactory.create_session_manager(
                        session_id=request.session_id,
                        user_id=user_id,
                        caching_enabled=False
                    )

                    user_msg = {"role": "user", "content": [{"text": request.message}]}
                    assistant_msg = {"role": "assistant", "content": [{"text": error_event.message}]}

                    if hasattr(session_manager, 'base_manager') and hasattr(session_manager.base_manager, 'create_message'):
                        user_session_msg = SessionMessage.from_message(user_msg, 0)
                        assistant_session_msg = SessionMessage.from_message(assistant_msg, 1)
                        session_manager.base_manager.create_message(request.session_id, "default", user_session_msg)
                        session_manager.base_manager.create_message(request.session_id, "default", assistant_session_msg)
                        logger.info(f"💾 Saved timeout error messages to session {request.session_id}")
                except Exception as persist_error:
                    logger.error(f"Failed to persist timeout error to session: {persist_error}")

                return

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
                # Log unexpected errors and send to client as conversational message
                logger.error(f"Error during streaming for session {request.session_id}: {e}", exc_info=True)

                # Build conversational error for better UX
                error_event = build_conversational_error_event(
                    code=ErrorCode.STREAM_ERROR,
                    error=e,
                    session_id=request.session_id,
                    recoverable=True
                )

                # Stream error as assistant message
                yield f"event: message_start\ndata: {{\"role\": \"assistant\"}}\n\n"
                yield f"event: content_block_start\ndata: {{\"contentBlockIndex\": 0, \"type\": \"text\"}}\n\n"
                yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': error_event.message})}\n\n"

                yield f"event: content_block_stop\ndata: {{\"contentBlockIndex\": 0}}\n\n"
                yield f"event: message_stop\ndata: {{\"stopReason\": \"error\"}}\n\n"
                yield error_event.to_sse_format()
                yield "event: done\ndata: {}\n\n"

                # Persist error messages to session
                try:
                    from strands.types.session import SessionMessage
                    session_manager = SessionFactory.create_session_manager(
                        session_id=request.session_id,
                        user_id=user_id,
                        caching_enabled=False
                    )

                    user_msg = {"role": "user", "content": [{"text": request.message}]}
                    assistant_msg = {"role": "assistant", "content": [{"text": error_event.message}]}

                    if hasattr(session_manager, 'base_manager') and hasattr(session_manager.base_manager, 'create_message'):
                        user_session_msg = SessionMessage.from_message(user_msg, 0)
                        assistant_session_msg = SessionMessage.from_message(assistant_msg, 1)
                        session_manager.base_manager.create_message(request.session_id, "default", user_session_msg)
                        session_manager.base_manager.create_message(request.session_id, "default", assistant_session_msg)
                        logger.info(f"💾 Saved stream error messages to session {request.session_id}")
                except Exception as persist_error:
                    logger.error(f"Failed to persist stream error to session: {persist_error}")

                # Don't re-raise - we've handled the error gracefully
                return

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
        return StreamingResponse(
            stream_with_cleanup(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": request.session_id
            }
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is (e.g., from auth)
        raise
    except Exception as e:
        # Stream error as a conversational assistant message for better UX
        logger.error(f"Error in chat_stream: {e}", exc_info=True)

        error_event = build_conversational_error_event(
            code=ErrorCode.STREAM_ERROR,
            error=e,
            session_id=request.session_id,
            recoverable=True
        )

        return StreamingResponse(
            stream_conversational_message(
                message=error_event.message,
                stop_reason="error",
                metadata_event=error_event,
                session_id=request.session_id,
                user_id=user_id,
                user_input=request.message
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": request.session_id
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

