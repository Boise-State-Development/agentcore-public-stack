"""Sessions API routes

Provides endpoints for managing session metadata.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Response, BackgroundTasks, status
from typing import Optional
import logging
from apis.shared.sessions.models import (
    UpdateSessionMetadataRequest,
    SessionInterruptRequest,
    SessionSteerRequest,
    SessionSteerResponse,
    SessionMetadataResponse,
    SessionMetadata,
    SessionPreferences,
    SessionsListResponse,
    BulkDeleteSessionsRequest,
    BulkDeleteSessionsResponse,
    BulkDeleteSessionResult,
    MessagesListResponse
)
from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.metadata import (
    list_user_sessions,
    get_session_metadata,
    mark_session_read,
    mark_session_unread,
    remove_pending_interrupts,
    session_exists_for_other_user,
    set_interrupted_turn,
    store_session_metadata,
)
from .services.session_service import SessionService
from apis.app_api.shares.service import get_share_service
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.feature_flags import mid_turn_steering_enabled
from apis.shared.auth.models import User
from apis.shared.system_prompts.service import get_system_prompts_service

from apis.shared.security.log_sanitize import scrub_log
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=SessionsListResponse, response_model_exclude_none=True)
async def list_user_sessions_endpoint(
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of sessions to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for retrieving the next page of results"),
    current_user: User = Depends(get_current_user_from_session)
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

    logger.info("GET /sessions - listing user sessions")

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
        logger.error("Error listing user sessions", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list user sessions: {str(e)}"
        )


@router.get("/{session_id}/metadata", response_model=SessionMetadataResponse, response_model_exclude_none=True)
async def get_session_metadata_endpoint(
    session_id: str,
    current_user: User = Depends(get_current_user_from_session)
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

    logger.info("GET /sessions/metadata - retrieving session metadata")

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
        logger.error("Error retrieving session metadata", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve session metadata: {str(e)}"
        )


@router.post("/{session_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_session_read_endpoint(
    session_id: str,
    current_user: User = Depends(get_current_user_from_session)
):
    """
    Mark a session as read, clearing the durable ``unread`` flag.

    Called by the SPA when the user opens a session that a scheduled
    (unattended) run left unread. Idempotent single-attribute write — a
    no-op if the session is already read or doesn't exist. Ownership is
    enforced inside ``mark_session_read`` via the GSI lookup, so a session
    belonging to another user is silently ignored (no state change).

    Requires session-cookie authentication. Returns 204 No Content.
    """
    await mark_session_read(session_id=session_id, user_id=current_user.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/unread", status_code=status.HTTP_204_NO_CONTENT)
async def mark_session_unread_endpoint(
    session_id: str,
    current_user: User = Depends(get_current_user_from_session)
):
    """
    Mark a session as unread, setting the durable ``unread`` flag.

    The manual counterpart to ``POST /{id}/read`` — lets a user re-flag a
    conversation (e.g. "remind me to revisit this") so the sidebar dot
    returns. Idempotent single-attribute write; ownership is enforced inside
    ``mark_session_unread`` via the per-user GSI lookup, so a session
    belonging to another user is silently ignored (no state change).

    Requires session-cookie authentication. Returns 204 No Content.
    """
    await mark_session_unread(session_id=session_id, user_id=current_user.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{session_id}/metadata", response_model=SessionMetadataResponse, response_model_exclude_none=True)
async def update_session_metadata_endpoint(
    session_id: str,
    request: UpdateSessionMetadataRequest,
    current_user: User = Depends(get_current_user_from_session)
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

    # Detect whether the client explicitly included `selectedPromptId` in the
    # payload (vs. omitting it). Pydantic's `model_fields_set` records every
    # alias that was present in the input, regardless of value. This lets us
    # distinguish a real "clear to null" from "leave unchanged".
    fields_set = request.model_fields_set
    selected_prompt_provided = "selected_prompt_id" in fields_set
    clearing_prompt = selected_prompt_provided and request.selected_prompt_id is None

    # Validate selected_prompt_id references an enabled prompt — reject early
    # so we never persist a stale/invalid prompt reference.
    if request.selected_prompt_id:
        service = get_system_prompts_service()
        prompt = await service.get_enabled_prompt(request.selected_prompt_id)
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"System prompt '{request.selected_prompt_id}' not found or not enabled",
            )

    logger.info("PUT /sessions/metadata - updating session metadata")

    try:
        # Get existing metadata or create new
        existing_metadata = await get_session_metadata(
            session_id=session_id,
            user_id=user_id
        )

        if not existing_metadata:
            # The per-user fetch can return None either because the session
            # is genuinely fresh OR because a row exists for this
            # session_id under a different user's partition (the
            # SessionLookupIndex GSI is shared across users). Mirror GET's
            # 404 in the second case so the caller cannot use a write to
            # claim ownership of a session id that's already taken.
            if await session_exists_for_other_user(
                session_id=session_id, current_user_id=user_id
            ):
                logger.warning(
                    "PUT /sessions/%s/metadata: session id is taken under a different user; refusing",
                    scrub_log(session_id),
                )
                raise HTTPException(
                    status_code=404,
                    detail=f"Session not found: {session_id}",
                )

            # Create new session metadata with defaults
            now = utc_now_iso()

            # Build preferences if any preference fields are provided
            preferences = None
            if any([
                request.last_model,
                request.enabled_tools,
                request.selected_prompt_id,
                request.custom_prompt_text,
                request.assistant_id,
                request.agent_type
            ]):
                preferences = SessionPreferences(
                    last_model=request.last_model,
                    enabled_tools=request.enabled_tools,
                    selected_prompt_id=request.selected_prompt_id,
                    custom_prompt_text=request.custom_prompt_text,
                    assistant_id=request.assistant_id,
                    agent_type=request.agent_type
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
                request.enabled_tools,
                request.selected_prompt_id,
                clearing_prompt,
                request.custom_prompt_text,
                request.assistant_id,
                request.agent_type
            ]):
                # Merge with existing preferences
                existing_prefs = preferences.model_dump(by_alias=False) if preferences else {}
                new_prefs = {}
                if request.last_model:
                    new_prefs['last_model'] = request.last_model
                if request.enabled_tools:
                    new_prefs['enabled_tools'] = request.enabled_tools
                if clearing_prompt:
                    new_prefs['selected_prompt_id'] = None
                elif request.selected_prompt_id:
                    new_prefs['selected_prompt_id'] = request.selected_prompt_id
                if request.custom_prompt_text:
                    new_prefs['custom_prompt_text'] = request.custom_prompt_text
                if request.assistant_id:
                    new_prefs['assistant_id'] = request.assistant_id
                if request.agent_type:
                    new_prefs['agent_type'] = request.agent_type

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
        logger.error("Error updating session metadata", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update session metadata: {str(e)}"
        )


@router.delete("/{session_id}", status_code=204)
async def delete_session_endpoint(
    session_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user_from_session)
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

    logger.info("DELETE /sessions - deleting session")

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

        # Queue cleanup tasks as background tasks (fire-and-forget)
        # These don't block the response - cleanup happens after 204 is sent

        # 1. Delete AgentCore Memory content
        background_tasks.add_task(
            service.delete_agentcore_memory,
            session_id,
            user_id
        )

        # 2. Cascade delete associated files (S3 objects + metadata)
        background_tasks.add_task(
            service.delete_session_files,
            session_id
        )

        # 3. Delete share snapshots so share links stop working
        share_service = get_share_service()
        background_tasks.add_task(
            share_service.delete_shares_for_session,
            session_id
        )

        logger.info("Successfully deleted session")

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting session", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete session: {str(e)}"
        )


@router.post("/bulk-delete", response_model=BulkDeleteSessionsResponse)
async def bulk_delete_sessions_endpoint(
    request: BulkDeleteSessionsRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user_from_session)
):
    """
    Bulk delete multiple conversations.

    Deletes up to 20 sessions at once. Each session is soft-deleted (moved from
    S#ACTIVE# to S#DELETED# prefix) and conversation content is scheduled for
    deletion from AgentCore Memory as a background task.

    Cost records are preserved for billing and audit purposes.

    The response includes detailed results for each session, allowing the client
    to handle partial failures gracefully.

    Requires JWT authentication. Users can only delete their own sessions.

    Args:
        request: BulkDeleteSessionsRequest with list of session IDs (max 20)
        background_tasks: FastAPI BackgroundTasks for async cleanup
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        BulkDeleteSessionsResponse with counts and individual results

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 422 if validation fails (empty list, >20 sessions)
            - 500 if server error
    """
    user_id = current_user.user_id
    session_ids = request.session_ids

    logger.info("POST /sessions/bulk-delete - bulk deleting sessions")

    results = []
    deleted_count = 0
    failed_count = 0

    try:
        service = SessionService()
        share_service = get_share_service()

        for session_id in session_ids:
            try:
                deleted = await service.delete_session(
                    user_id=user_id,
                    session_id=session_id
                )

                if deleted:
                    # Queue cleanup tasks as background tasks
                    background_tasks.add_task(
                        service.delete_agentcore_memory,
                        session_id,
                        user_id
                    )
                    background_tasks.add_task(
                        service.delete_session_files,
                        session_id
                    )
                    background_tasks.add_task(
                        share_service.delete_shares_for_session,
                        session_id
                    )
                    results.append(BulkDeleteSessionResult(
                        session_id=session_id,
                        success=True,
                        error=None
                    ))
                    deleted_count += 1
                else:
                    results.append(BulkDeleteSessionResult(
                        session_id=session_id,
                        success=False,
                        error="Session not found"
                    ))
                    failed_count += 1

            except Exception as e:
                logger.warning("Failed to delete session in bulk operation")
                results.append(BulkDeleteSessionResult(
                    session_id=session_id,
                    success=False,
                    error=str(e)
                ))
                failed_count += 1

        logger.info("Bulk delete completed")

        return BulkDeleteSessionsResponse(
            deleted_count=deleted_count,
            failed_count=failed_count,
            results=results
        )

    except Exception as e:
        logger.error("Error in bulk delete sessions", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to bulk delete sessions: {str(e)}"
        )


@router.get("/{session_id}/messages", response_model=MessagesListResponse, response_model_exclude_none=True)
async def get_session_messages_endpoint(
    session_id: str,
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of messages to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for retrieving the next page of results"),
    current_user: User = Depends(get_current_user_from_session)
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

    logger.info("GET /sessions/messages - retrieving session messages")

    try:
        # Retrieve messages from storage (cloud or local) with pagination
        response = await get_messages(
            session_id=session_id,
            user_id=user_id,
            limit=limit,
            next_token=next_token
        )

        logger.info("Successfully retrieved session messages")

        return response

    except ValueError as e:
        logger.error("Configuration error retrieving messages")
        raise HTTPException(
            status_code=500,
            detail=f"Server configuration error: {str(e)}"
        )
    except FileNotFoundError as e:
        logger.warning("Session not found")
        raise HTTPException(
            status_code=404,
            detail=f"Session not found: {session_id}"
        )
    except Exception as e:
        logger.error("Error retrieving messages", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve messages: {str(e)}"
        )


@router.post("/{session_id}/interrupt", status_code=204)
async def signal_turn_interrupted_endpoint(
    session_id: str,
    body: SessionInterruptRequest,
    current_user: User = Depends(get_current_user_from_session),
):
    """Record a client-attested reason for the session's turn being interrupted.

    This is the AUTHORITATIVE carrier of client intent for the
    interrupted-turn flow: the transport cannot distinguish a Stop click from
    a refresh from a dropped socket (all three surface as a cancelled
    stream), so the SPA signals out-of-band — via ``fetch(..., {keepalive:
    true})`` with the ``X-CSRF-Token`` header (NOT ``navigator.sendBeacon``,
    which cannot set headers and would be rejected by CSRFMiddleware).

    Two reasons are accepted, and they mean different things:

      * ``user_stopped``   — the Stop button. Deliberate: the user rejected
        the response in flight, so the turn is cancelled server-side too.
      * ``navigated_away`` — the page was hidden or unloaded mid-turn. The
        user left; they did not reject anything. **Recorded only** — the
        running turn is deliberately left alone, matching today's behaviour
        where a refresh lets the turn finish server-side and the reload
        offers to continue it.

    Lives on app-api, not inference-api: the AgentCore Runtime data plane
    only proxies ``/invocations`` + ``/ping``, so a custom inference-api
    route would 404 in cloud.

    Both take precedence over the ``connection_lost`` fallback that
    inference-api's cancellation backstop may race against this write (see
    ``set_interrupted_turn``). No-op for missing sessions — and the GSI
    lookup inside ``set_interrupted_turn`` is user-scoped, so a session
    owned by someone else is also a no-op. Returns 204 either way (the
    user's intent is recorded best-effort; the client never waits on it).
    """
    user_id = current_user.user_id

    logger.info("POST /sessions/.../interrupt (reason=%s)", body.reason)

    try:
        await set_interrupted_turn(
            session_id,
            user_id,
            reason=body.reason,
            source="client_signal",
        )
        # Distributed turn cancellation: a client abort doesn't propagate
        # through the AgentCore Runtime data plane, so arm a cancel on the
        # session's single-flight lease. The container running the turn
        # observes it on its next heartbeat and unwinds — releasing the lease
        # so the user's resend isn't rejected with 409 and stopping wasted
        # model/tool work. Owner-scoped, so a stale Stop can't kill a later
        # turn. Best-effort: never fail the Stop signal on this.
        #
        # Deliberate Stop ONLY. `navigated_away` is an attribution signal, not
        # an instruction: cancelling on it would make every refresh kill the
        # turn it interrupted, discarding work the reload is about to offer to
        # continue. Leaving the turn running preserves exactly today's
        # behaviour for a departure — this endpoint's reason set widened, the
        # side effects did not.
        if body.reason == "user_stopped":
            try:
                from apis.shared.sessions.session_lease import request_session_cancel
                await request_session_cancel(session_id, user_id)
            except Exception:
                logger.warning("Failed to arm session cancel on stop", exc_info=True)
        return Response(status_code=204)
    except Exception:
        logger.error("Error recording turn interruption", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to record interruption",
        )


@router.post("/{session_id}/steer", response_model=SessionSteerResponse, response_model_by_alias=True)
async def steer_running_turn_endpoint(
    session_id: str,
    body: SessionSteerRequest,
    current_user: User = Depends(get_current_user_from_session),
):
    """Queue a follow-up for injection into the turn that is streaming right now.

    Mid-turn steering (docs/specs/mid-turn-steering.md). PR #916 made Enter
    mean "say this" while a response streams, but the follow-up sat in the
    composer until the turn ended — so a user who saw the agent open the wrong
    file could only wait or Stop-and-resend, and the second discards a partial
    generation and re-establishes the prefix. This endpoint arms the text on
    the session's single-flight lease row; the container running the turn
    peeks it at its next tool boundary and appends it to the tool-result
    message, so the agent reads it before choosing its next action.

    Lives on app-api, not inference-api, for the same reason ``/interrupt``
    does: the AgentCore Runtime data plane proxies only ``/invocations`` and
    ``/ping``, so a steer route on inference-api would 404 in cloud. The lease
    row is the cross-container side channel — exactly the mechanism the Stop
    path already proves — and it is owner-scoped, so a steer armed against a
    turn that has since ended is ignored rather than misdelivered to the next
    one.

    Returns 200 with ``queued=false`` when there is no live turn to steer, or
    when the turn ended between the user typing and this request landing. That
    race resolving to "not queued" is the correct outcome, not an error: the
    SPA leaves the entry in its queue and the existing end-of-turn flush sends
    it as a normal turn. 429 when the inbox is at its cap (same fallback).
    """
    if not mid_turn_steering_enabled():
        raise HTTPException(status_code=404, detail="Mid-turn steering is not enabled")

    user_id = current_user.user_id

    logger.info("POST /sessions/.../steer")

    from apis.shared.sessions.session_lease import (
        SteerQueueFullError,
        request_session_steer,
    )

    try:
        queued = await request_session_steer(
            session_id,
            user_id,
            text=body.text,
            entry_id=body.entry_id,
        )
    except SteerQueueFullError:
        raise HTTPException(
            status_code=429,
            detail="Too many follow-ups are already queued for this turn",
        )
    except Exception:
        logger.error("Error queueing a mid-turn steer", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to queue the follow-up")

    return SessionSteerResponse(queued=queued, entry_id=body.entry_id)


@router.delete("/{session_id}/steer/{entry_id}", status_code=204)
async def withdraw_steer_endpoint(
    session_id: str,
    entry_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """Withdraw a queued follow-up the user removed from the composer.

    Best-effort and idempotent: an unknown id, an already-consumed entry, and
    a turn that has since ended all answer 204, because the user's intent —
    "don't send that" — is satisfied in every one of those cases. Only the
    caller's own session's inbox is reachable, since the lease row is keyed
    under ``USER#{user_id}``.
    """
    if not mid_turn_steering_enabled():
        raise HTTPException(status_code=404, detail="Mid-turn steering is not enabled")

    user_id = current_user.user_id

    logger.info("DELETE /sessions/.../steer/...")

    try:
        from apis.shared.sessions.session_lease import remove_steer_entry

        await remove_steer_entry(session_id, user_id, entry_id)
    except Exception:
        # The entry is either still queued (and will be injected, which the
        # SPA can render) or already gone. Neither is worth a 500 on a
        # withdrawal the user has already seen disappear from their composer.
        logger.warning("Failed to withdraw a queued steer", exc_info=True)

    return Response(status_code=204)


@router.delete("/{session_id}/pending-interrupts/{interrupt_id:path}", status_code=204)
async def dismiss_pending_interrupt_endpoint(
    session_id: str,
    interrupt_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """Dismiss a pending OAuth consent interrupt for the caller's session.

    The frontend calls this when the user clicks the dismiss button on an
    inline consent prompt, so a refresh doesn't redisplay it. The id is
    matched as-is — Strands generates ids like ``oauth:google-calendar``,
    so we accept ``:path`` to keep the colon literal in the URL.

    No-op for unknown ids and missing sessions, returning 204 in both
    cases (the user's intent is satisfied).
    """
    user_id = current_user.user_id

    logger.info("DELETE /sessions/.../pending-interrupts/...")

    try:
        await remove_pending_interrupts(
            session_id=session_id,
            user_id=user_id,
            interrupt_ids=[interrupt_id],
        )
        return Response(status_code=204)
    except Exception as e:
        logger.error("Error dismissing pending interrupt", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to dismiss interrupt: {str(e)}",
        )
