"""Sessions API routes

Provides endpoints for managing session metadata.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Response, BackgroundTasks
from typing import Optional
import logging
from datetime import datetime
from .models import UpdateSessionMetadataRequest, SessionMetadataResponse, SessionMetadata, SessionPreferences, SessionsListResponse
from apis.app_api.messages.models import MessagesListResponse
from .services.messages import get_messages
from .services.metadata import store_session_metadata, get_session_metadata, list_user_sessions
from .services.session_service import SessionService
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=SessionsListResponse, response_model_exclude_none=True)
async def list_user_sessions_endpoint(
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of sessions to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for retrieving the next page of results"),
    current_user: User = Depends(get_current_user)
):
    """
    List sessions for the authenticated user with pagination support.

    Requires JWT authentication. Returns only sessions belonging to the authenticated user,
    sorted by last_message_at descending (most recent first).

    Args:
        limit: Maximum number of sessions to return (optional, 1-1000)
        next_token: Pagination token for retrieving next page (optional)
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        SessionsListResponse with paginated sessions and next_token if more results exist

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"GET /sessions - User: {user_id}, Limit: {limit}, NextToken: {next_token}")

    try:
        # Retrieve sessions for the user with pagination
        sessions, next_page_token = await list_user_sessions(
            user_id=user_id,
            limit=limit,
            next_token=next_token
        )

        # Convert to response models
        session_responses = [
            SessionMetadataResponse.model_validate(
                session.model_dump(by_alias=True)
            )
            for session in sessions
        ]

        return SessionsListResponse(
            sessions=session_responses,
            next_token=next_page_token
        )

    except Exception as e:
        logger.error(f"Error listing user sessions: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list user sessions: {str(e)}"
        )


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

            # IMPORTANT: Do NOT set message_count here - it should only be managed by
            # the streaming coordinator (_update_session_metadata in stream_coordinator.py)
            # Setting it here causes a race condition where the PUT endpoint writes 0,
            # then the streaming coordinator writes the correct count, but the deep merge
            # preserves the incorrect 0 value.
            metadata = SessionMetadata(
                session_id=session_id,
                user_id=user_id,
                title=request.title or "New Conversation",
                status=request.status or "active",
                created_at=now,
                last_message_at=now,
                # message_count will be set by streaming coordinator on first message
                message_count=0,  # Safe default - will be overwritten by first message
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


@router.delete("/{session_id}", status_code=204)
async def delete_session_endpoint(
    session_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a conversation.

    This soft-deletes the session metadata (moves from S#ACTIVE# to S#DELETED#
    prefix) and schedules deletion of conversation content from AgentCore Memory
    as a background task (fire-and-forget).

    Cost records are preserved for billing and audit purposes - they are stored
    separately with C# SK prefix and are not affected by session deletion.

    Requires JWT authentication. Users can only delete their own sessions.

    Args:
        session_id: Session identifier from URL path
        background_tasks: FastAPI BackgroundTasks for async cleanup
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        204 No Content on success

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if session not found
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"DELETE /sessions/{session_id} - User: {user_id}")

    try:
        service = SessionService()
        deleted = await service.delete_session(
            user_id=user_id,
            session_id=session_id
        )

        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Session not found: {session_id}"
            )

        # Queue AgentCore Memory cleanup as background task (fire-and-forget)
        # This doesn't block the response - cleanup happens after 204 is sent
        background_tasks.add_task(
            service.delete_agentcore_memory,
            session_id,
            user_id
        )

        logger.info(f"Successfully deleted session {session_id} for user {user_id}")

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting session: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete session: {str(e)}"
        )


@router.get("/{session_id}/messages", response_model=MessagesListResponse, response_model_exclude_none=True)
async def get_session_messages_endpoint(
    session_id: str,
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of messages to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for retrieving the next page of results"),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve messages for a specific session with pagination support.

    Requires JWT authentication. The user_id is extracted from the JWT token.
    Users can only access their own messages.

    Args:
        session_id: Session identifier from URL path
        limit: Maximum number of messages to return (optional, max: 1000)
        next_token: Pagination token for retrieving next page (optional)
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        MessagesListResponse with paginated conversation history

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user doesn't have required roles
            - 404 if session not found
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"GET /sessions/{session_id}/messages - User: {user_id}, Limit: {limit}, NextToken: {next_token}")

    try:
        # Retrieve messages from storage (cloud or local) with pagination
        response = await get_messages(
            session_id=session_id,
            user_id=user_id,
            limit=limit,
            next_token=next_token
        )

        logger.info(f"Successfully retrieved {len(response.messages)} messages for session {session_id}")

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
