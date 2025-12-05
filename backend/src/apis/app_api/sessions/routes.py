"""Sessions API routes

Provides endpoints for managing session metadata.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
import logging
from datetime import datetime

from .models import UpdateSessionMetadataRequest, SessionMetadataResponse
from ..messages.models import SessionMetadata, SessionPreferences
from ..metadata.service import store_session_metadata, get_session_metadata
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/{session_id}/metadata", response_model=SessionMetadataResponse, response_model_exclude_none=True)
async def get_session_metadata_endpoint(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve session metadata for a specific session.

    Requires JWT authentication. Users can only access their own sessions.

    Args:
        session_id: Session identifier from URL path
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        SessionMetadataResponse with session information

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if session not found
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"GET /sessions/{session_id}/metadata - User: {user_id}")

    try:
        # Retrieve session metadata
        metadata = await get_session_metadata(
            session_id=session_id,
            user_id=user_id
        )

        if not metadata:
            raise HTTPException(
                status_code=404,
                detail=f"Session not found: {session_id}"
            )

        # Convert to response model
        return SessionMetadataResponse.model_validate(
            metadata.model_dump(by_alias=True)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving session metadata: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve session metadata: {str(e)}"
        )


@router.put("/{session_id}/metadata", response_model=SessionMetadataResponse, response_model_exclude_none=True)
async def update_session_metadata_endpoint(
    session_id: str,
    request: UpdateSessionMetadataRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Update session metadata for a specific session.

    Requires JWT authentication. Users can only update their own sessions.
    This performs a deep merge - existing fields are preserved unless explicitly updated.

    Args:
        session_id: Session identifier from URL path
        request: Fields to update (only non-null fields are updated)
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        SessionMetadataResponse with updated session information

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if session not found
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"PUT /sessions/{session_id}/metadata - User: {user_id}")

    try:
        # Get existing metadata or create new
        existing_metadata = await get_session_metadata(
            session_id=session_id,
            user_id=user_id
        )

        if not existing_metadata:
            # Create new session metadata with defaults
            now = datetime.utcnow().isoformat() + "Z"

            # Build preferences if any preference fields are provided
            preferences = None
            if any([
                request.last_model,
                request.last_temperature is not None,
                request.enabled_tools,
                request.selected_prompt_id,
                request.custom_prompt_text
            ]):
                preferences = SessionPreferences(
                    last_model=request.last_model,
                    last_temperature=request.last_temperature,
                    enabled_tools=request.enabled_tools,
                    selected_prompt_id=request.selected_prompt_id,
                    custom_prompt_text=request.custom_prompt_text
                )

            metadata = SessionMetadata(
                session_id=session_id,
                user_id=user_id,
                title=request.title or "New Conversation",
                status=request.status or "active",
                created_at=now,
                last_message_at=now,
                message_count=0,
                starred=request.starred or False,
                tags=request.tags or [],
                preferences=preferences
            )
        else:
            # Update existing metadata (deep merge)
            # Build updated preferences if any preference field is provided
            preferences = existing_metadata.preferences
            if any([
                request.last_model,
                request.last_temperature is not None,
                request.enabled_tools,
                request.selected_prompt_id,
                request.custom_prompt_text
            ]):
                # Merge with existing preferences
                existing_prefs = preferences.model_dump(by_alias=False) if preferences else {}
                new_prefs = {}
                if request.last_model:
                    new_prefs['last_model'] = request.last_model
                if request.last_temperature is not None:
                    new_prefs['last_temperature'] = request.last_temperature
                if request.enabled_tools:
                    new_prefs['enabled_tools'] = request.enabled_tools
                if request.selected_prompt_id:
                    new_prefs['selected_prompt_id'] = request.selected_prompt_id
                if request.custom_prompt_text:
                    new_prefs['custom_prompt_text'] = request.custom_prompt_text

                merged_prefs = {**existing_prefs, **new_prefs}
                preferences = SessionPreferences(**merged_prefs)

            # Create updated metadata (only update non-null fields)
            metadata = SessionMetadata(
                session_id=session_id,
                user_id=user_id,
                title=request.title if request.title else existing_metadata.title,
                status=request.status if request.status else existing_metadata.status,
                created_at=existing_metadata.created_at,
                last_message_at=existing_metadata.last_message_at,
                message_count=existing_metadata.message_count,
                starred=request.starred if request.starred is not None else existing_metadata.starred,
                tags=request.tags if request.tags is not None else existing_metadata.tags,
                preferences=preferences
            )

        # Store updated metadata
        await store_session_metadata(
            session_id=session_id,
            user_id=user_id,
            session_metadata=metadata
        )

        # Return updated metadata
        return SessionMetadataResponse.model_validate(
            metadata.model_dump(by_alias=True)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating session metadata: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update session metadata: {str(e)}"
        )
