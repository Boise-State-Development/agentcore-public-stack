"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import StreamingResponse
import logging
from typing import AsyncGenerator

from .models import InvocationRequest
from .service import get_agent
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.errors import ErrorCode, create_error_response
from apis.shared.quota import (
    get_quota_checker,
    is_quota_enforcement_enabled,
    build_quota_exceeded_response,
    build_quota_warning_event,
)

logger = logging.getLogger(__name__)

# Router with no prefix - endpoints will be at root level
router = APIRouter(tags=["agentcore-runtime"])


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
    - Returns 429 if quota exceeded
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
    if is_quota_enforcement_enabled():
        try:
            quota_checker = get_quota_checker()
            quota_result = await quota_checker.check_quota(
                user=current_user,
                session_id=input_data.session_id
            )

            if not quota_result.allowed:
                # Quota exceeded - return 429
                logger.warning(f"Quota exceeded for user {user_id}: {quota_result.message}")
                response = build_quota_exceeded_response(quota_result)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=response.model_dump(by_alias=True)
                )

            # Check for warning level
            quota_warning_event = build_quota_warning_event(quota_result)
            if quota_warning_event:
                logger.info(f"Quota warning for user {user_id}: {quota_result.warning_level}")

        except HTTPException:
            raise
        except Exception as e:
            # Log error but don't block request - fail open for quota errors
            logger.error(f"Error checking quota for user {user_id}: {e}", exc_info=True)

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
        logger.error(f"Error in invocations: {e}", exc_info=True)
        error_detail = create_error_response(
            code=ErrorCode.AGENT_ERROR,
            message="Agent processing failed",
            detail=str(e),
            status_code=500,
            metadata={"session_id": input_data.session_id}
        )
        raise HTTPException(
            status_code=500,
            detail=error_detail
        )

