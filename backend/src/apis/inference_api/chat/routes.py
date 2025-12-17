"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
import logging

from .models import InvocationRequest
from .service import get_agent
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.errors import ErrorCode, create_error_response

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

