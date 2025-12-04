"""Messages API routes

Provides endpoints to retrieve conversation history with JWT authentication.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
import logging

from .service import get_messages
from .models import GetMessagesResponse
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/{session_id}", response_model=GetMessagesResponse, response_model_exclude_none=True)
async def get_session_messages(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve all messages for a specific session and user.

    Requires JWT authentication. The user_id is extracted from the JWT token.
    Users can only access their own messages.

    Args:
        session_id: Session identifier from URL path
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        GetMessagesResponse with conversation history

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user doesn't have required roles
            - 404 if session not found
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"GET /messages/{session_id} - User: {user_id}")

    try:
        # Retrieve messages from storage (cloud or local)
        response = await get_messages(
            session_id=session_id,
            user_id=user_id
        )

        logger.info(f"Successfully retrieved {response.total_count} messages for session {session_id}")

        return response

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Server configuration error: {str(e)}"
        )
    except FileNotFoundError as e:
        logger.warning(f"Session not found: {session_id}")
        raise HTTPException(
            status_code=404,
            detail=f"Session not found: {session_id}"
        )
    except Exception as e:
        logger.error(f"Error retrieving messages: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve messages: {str(e)}"
        )
