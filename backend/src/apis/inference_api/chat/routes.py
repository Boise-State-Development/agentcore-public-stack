"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import StreamingResponse
import logging
import json
from typing import AsyncGenerator, Union
from datetime import datetime, timezone

from .models import InvocationRequest
from .service import get_agent
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.errors import (
    ErrorCode,
    create_error_response,
    ConversationalErrorEvent,
    build_conversational_error_event,
)
from apis.shared.quota import (
    get_quota_checker,
    is_quota_enforcement_enabled,
    build_quota_exceeded_event,
    build_quota_warning_event,
    QuotaExceededEvent,
)
from agents.strands_agent.session.session_factory import SessionFactory

logger = logging.getLogger(__name__)

# Router with no prefix - endpoints will be at root level
router = APIRouter(tags=["agentcore-runtime"])


# ============================================================
# Helper Functions for Streaming Error/Status Messages
# ============================================================

async def stream_conversational_message(
    message: str,
    stop_reason: str,
    metadata_event: Union[QuotaExceededEvent, ConversationalErrorEvent, None],
    session_id: str,
    user_id: str,
    user_input: str
) -> AsyncGenerator[str, None]:
    """Stream a message as an assistant response with optional metadata event.

    This helper function creates a proper SSE stream that appears as an
    assistant message in the chat UI and persists to session history.

    Args:
        message: The markdown message to display
        stop_reason: Reason for stopping (e.g., 'quota_exceeded', 'error')
        metadata_event: Optional event with additional metadata for UI
        session_id: Session ID for persistence
        user_id: User ID for persistence
        user_input: The user's original message to save
    """
    # Emit message_start event (assistant response)
    yield f"event: message_start\ndata: {json.dumps({'role': 'assistant'})}\n\n"

    # Emit content_block_start for text
    yield f"event: content_block_start\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text'})}\n\n"

    # Emit the message as text delta
    yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': message})}\n\n"

    # Emit content_block_stop
    yield f"event: content_block_stop\ndata: {json.dumps({'contentBlockIndex': 0})}\n\n"

    # Emit message_stop
    yield f"event: message_stop\ndata: {json.dumps({'stopReason': stop_reason})}\n\n"

    # Emit the metadata event with full details for UI handling
    if metadata_event:
        yield metadata_event.to_sse_format()

    # Emit done event
    yield "event: done\ndata: {}\n\n"

    # Save messages to session for persistence
    try:
        from strands.types.session import SessionMessage

        session_manager = SessionFactory.create_session_manager(
            session_id=session_id,
            user_id=user_id,
            caching_enabled=False
        )

        # Save user message
        user_message = {
            "role": "user",
            "content": [{"text": user_input}]
        }

        # Save assistant message
        assistant_message = {
            "role": "assistant",
            "content": [{"text": message}]
        }

        # Use base_manager's create_message for persistence (AgentCore Memory)
        if hasattr(session_manager, 'base_manager') and hasattr(session_manager.base_manager, 'create_message'):
            user_session_msg = SessionMessage.from_message(user_message, 0)
            assistant_session_msg = SessionMessage.from_message(assistant_message, 1)

            session_manager.base_manager.create_message(session_id, "default", user_session_msg)
            session_manager.base_manager.create_message(session_id, "default", assistant_session_msg)
            logger.info(f"💾 Saved {stop_reason} messages to session {session_id}")

    except Exception as e:
        logger.error(f"Failed to save {stop_reason} messages to session: {e}", exc_info=True)


# ============================================================
# AgentCore Runtime Standard Endpoints (REQUIRED)
# ============================================================

@router.get("/ping")
async def ping():
    """Health check endpoint (required by AgentCore Runtime)"""
    return {"status": "healthy"}


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

    Quota enforcement (when enabled via ENABLE_QUOTA_ENFORCEMENT=true):
    - Checks user quota before processing
    - Streams quota_exceeded as assistant message if quota exceeded (better UX)
    - Injects quota_warning event into stream if approaching limit
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

    # Check quota if enforcement is enabled
    quota_warning_event = None
    quota_exceeded_event = None
    if is_quota_enforcement_enabled():
        try:
            quota_checker = get_quota_checker()
            quota_result = await quota_checker.check_quota(
                user=current_user,
                session_id=input_data.session_id
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
                session_id=input_data.session_id,
                user_id=user_id,
                user_input=input_data.message
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": input_data.session_id
            }
        )

    try:
        # Get agent instance with user-specific configuration
        # AgentCore Memory tracks preferences across sessions per user_id
        # Supports multiple LLM providers: AWS Bedrock, OpenAI, and Google Gemini
        agent = get_agent(
            session_id=input_data.session_id,
            user_id=user_id,
            enabled_tools=input_data.enabled_tools,
            model_id=input_data.model_id,
            temperature=input_data.temperature,
            system_prompt=input_data.system_prompt,
            caching_enabled=input_data.caching_enabled,
            provider=input_data.provider,
            max_tokens=input_data.max_tokens
        )

        # Create stream with optional quota warning injection
        async def stream_with_quota_warning() -> AsyncGenerator[str, None]:
            """Wrap agent stream to inject quota warning at start if needed"""
            # Yield quota warning event first if applicable
            if quota_warning_event:
                yield quota_warning_event.to_sse_format()

            # Then yield all agent stream events
            async for event in agent.stream_async(
                input_data.message,
                session_id=input_data.session_id,
                files=input_data.files
            ):
                yield event

        # Stream response from agent as SSE (with optional files)
        # Note: Compression is handled by GZipMiddleware if configured in main.py
        return StreamingResponse(
            stream_with_quota_warning(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": input_data.session_id
            }
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is (e.g., from auth)
        raise
    except Exception as e:
        # Stream error as a conversational assistant message for better UX
        logger.error(f"Error in invocations: {e}", exc_info=True)

        error_event = build_conversational_error_event(
            code=ErrorCode.AGENT_ERROR,
            error=e,
            session_id=input_data.session_id,
            recoverable=True
        )

        return StreamingResponse(
            stream_conversational_message(
                message=error_event.message,
                stop_reason="error",
                metadata_event=error_event,
                session_id=input_data.session_id,
                user_id=user_id,
                user_input=input_data.message
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": input_data.session_id
            }
        )

