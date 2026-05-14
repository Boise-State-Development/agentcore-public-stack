"""Chat feature routes

Application-specific chat endpoints moved from inference_api to keep
the AgentCore Runtime API clean. Currently:
- Conversation title generation (`POST /chat/generate-title`)

The browser-facing streaming chat path is the cookie-authenticated BFF
proxy at `POST /chat/stream` (see `proxy_routes.py`).
"""

import logging

from fastapi import APIRouter, Depends

from apis.inference_api.chat.models import GenerateTitleRequest, GenerateTitleResponse
from apis.inference_api.chat.service import generate_conversation_title
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/generate-title")
async def generate_title(request: GenerateTitleRequest, current_user: User = Depends(get_current_user_from_session)):
    """
    Generate a conversation title for a new session.

    This endpoint uses AWS Bedrock Nova Micro to generate a concise,
    descriptive title based on the user's initial message. It's designed
    to be called in parallel with the first chat request.

    The endpoint:
    - Uses cookie session auth to extract user_id
    - Truncates input to ~500 tokens for speed and cost efficiency
    - Calls Nova Micro with temperature=0.3 for consistent output
    - Updates session metadata both locally and in cloud
    - Returns fallback title "New Conversation" on error

    Args:
        request: GenerateTitleRequest with session_id and user input
        current_user: User from session cookie (injected by dependency)

    Returns:
        GenerateTitleResponse with generated title and session_id
    """
    user_id = current_user.user_id
    logger.info(f"Title generation request - Session: {request.session_id}, User: {user_id}")

    try:
        # Generate title using Nova Micro
        title = await generate_conversation_title(session_id=request.session_id, user_id=user_id, user_input=request.input)

        return GenerateTitleResponse(title=title, session_id=request.session_id)

    except Exception as e:
        logger.error(f"Error in generate_title endpoint: {e}")
        # Return fallback instead of raising exception
        # Title generation failures shouldn't break the user experience
        return GenerateTitleResponse(title="New Conversation", session_id=request.session_id)
