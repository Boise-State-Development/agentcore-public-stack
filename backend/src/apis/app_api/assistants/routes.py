"""Assistants API routes

Provides endpoints for managing AI assistants (CRUD operations).
"""

import asyncio
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from apis.app_api.documents.services.document_service import list_assistant_documents
from apis.inference_api.chat.routes import stream_conversational_message
from apis.inference_api.chat.service import get_agent
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.errors import ErrorCode, build_conversational_error_event
from apis.shared.assistants.models import (
    AssistantResponse,
    AssistantSharesResponse,
    AssistantsListResponse,
    AssistantTestChatRequest,
    CreateAssistantDraftRequest,
    CreateAssistantRequest,
    ShareAssistantRequest,
    UnshareAssistantRequest,
    UpdateAssistantRequest,
)
from apis.shared.assistants.service import (
    assistant_exists,
    create_assistant,
    create_assistant_draft,
    delete_assistant,
    get_assistant,
    get_assistant_with_access_check,
    list_assistant_shares,
    list_shared_with_user,
    list_user_assistants,
    share_assistant,
    unshare_assistant,
    update_assistant,
)
from apis.shared.assistants.rag_service import augment_prompt_with_context, search_assistant_knowledgebase_with_formatting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistants", tags=["assistants"])


@router.post("/draft", response_model=AssistantResponse, response_model_exclude_none=True)
async def create_assistant_draft_endpoint(request: CreateAssistantDraftRequest, current_user: User = Depends(get_current_user)):
    """
    Create a draft assistant with auto-generated ID.

    This endpoint is used when the user clicks "Create New" to immediately
    generate an assistant ID that can be used for tagging documents before
    the assistant is fully configured.

    Requires JWT authentication. The assistant is created with status=DRAFT
    and minimal fields. Use PUT /assistants/{assistant_id} to complete it.

    Args:
        request: CreateAssistantDraftRequest with optional name
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantResponse with generated ID and status=DRAFT

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"POST /assistants/draft - User: {user_id}, Name: {request.name}")

    try:
        # Create draft assistant
        assistant = await create_assistant_draft(owner_id=user_id, owner_name=current_user.name, name=request.name)

        # Convert to response model (excludes owner_id for privacy)
        assistant_dict = assistant.model_dump(by_alias=True, exclude={"ownerId"})
        return AssistantResponse.model_validate(assistant_dict)

    except Exception as e:
        logger.error(f"Error creating draft assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create draft assistant: {str(e)}")


@router.post("", response_model=AssistantResponse, response_model_exclude_none=True)
async def create_assistant_endpoint(request: CreateAssistantRequest, current_user: User = Depends(get_current_user)):
    """
    Create a complete assistant with all required fields.

    This endpoint creates a fully configured assistant with status=COMPLETE.
    Most users will use POST /assistants/draft followed by PUT /assistants/{id}
    instead of this endpoint.

    Requires JWT authentication.

    Args:
        request: CreateAssistantRequest with all required fields
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantResponse with generated ID and status=COMPLETE

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"POST /assistants - User: {user_id}, Name: {request.name}")

    try:
        # Create complete assistant
        # Note: vector_index_id is automatically set from S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME env var
        assistant = await create_assistant(
            owner_id=user_id,
            owner_name=current_user.name,
            name=request.name,
            description=request.description,
            instructions=request.instructions,
            visibility=request.visibility,
            tags=request.tags,
            starters=request.starters,
            emoji=request.emoji,
        )

        # Convert to response model (excludes owner_id for privacy)
        assistant_dict = assistant.model_dump(by_alias=True, exclude={"ownerId"})
        return AssistantResponse.model_validate(assistant_dict)

    except Exception as e:
        logger.error(f"Error creating assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create assistant: {str(e)}")


@router.get("", response_model=AssistantsListResponse, response_model_exclude_none=True)
async def list_assistants_endpoint(
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of assistants to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for retrieving the next page"),
    include_drafts: bool = Query(False, description="Include draft assistants"),
    include_public: bool = Query(False, description="Include public assistants (in addition to user's own)"),
    current_user: User = Depends(get_current_user),
):
    """
    List assistants for the authenticated user with pagination support.

    Requires JWT authentication. By default, returns only assistants belonging to the
    authenticated user, sorted by created_at descending (most recent first).

    When include_public=True, returns both the user's own assistants AND all public
    assistants (excluding those owned by the user to avoid duplicates).

    By default, excludes draft assistants. Use query parameters to include them.

    Args:
        limit: Maximum number of assistants to return (optional, 1-1000)
        next_token: Pagination token for retrieving next page (optional)
        include_drafts: Whether to include draft assistants (default: False)
        include_public: Whether to include public assistants (default: False)
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantsListResponse with paginated assistants and next_token if more exist

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("GET /assistants")

    try:
        # Retrieve assistants for the user with pagination
        assistants, next_page_token = await list_user_assistants(
            owner_id=user_id,
            limit=limit,
            next_token=next_token,
            include_drafts=include_drafts,
            include_public=include_public,
        )

        # Mark owned assistants with share metadata
        for assistant in assistants:
            assistant.is_shared_with_me = False
            assistant.first_interacted = None  # Not applicable for owned assistants

        # Also get assistants shared with this user
        shared_assistants = await list_shared_with_user(current_user.email)

        # Filter out duplicates (assistants the user already owns)
        owned_assistant_ids = {a.assistant_id for a in assistants}
        unique_shared = [a for a in shared_assistants if a.assistant_id not in owned_assistant_ids]

        # Mark shared assistants with share metadata
        for assistant in unique_shared:
            assistant.is_shared_with_me = True
            # first_interacted is already set by list_shared_with_user

        # Combine lists (user's own assistants first, then shared)
        all_assistants = assistants + unique_shared

        # Sort by created_at descending (most recent first)
        all_assistants.sort(key=lambda x: x.created_at, reverse=True)

        # Apply limit if specified (simple truncation for now - proper pagination would be more complex)
        if limit and limit > 0:
            all_assistants = all_assistants[:limit]
            # Note: next_token handling becomes complex with merged lists, so we'll set it to None
            # A more sophisticated implementation would handle pagination properly
            next_page_token = None if len(all_assistants) < limit else next_page_token

        # Convert to response models (excludes owner_id for privacy)
        assistant_responses = [
            AssistantResponse.model_validate(assistant.model_dump(by_alias=True, exclude={"ownerId"})) for assistant in all_assistants
        ]

        return AssistantsListResponse(assistants=assistant_responses, next_token=next_page_token)

    except Exception as e:
        logger.error(f"Error listing assistants: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list assistants: {str(e)}")


@router.get("/{assistant_id}", response_model=AssistantResponse, response_model_exclude_none=True)
async def get_assistant_endpoint(assistant_id: str, current_user: User = Depends(get_current_user)):
    """
    Retrieve a specific assistant by ID with visibility-based access control.

    Requires JWT authentication. Access is controlled by assistant visibility:
    - PRIVATE: Only owner can access
    - PUBLIC: Anyone can access
    - SHARED: Owner or users with share records can access

    Args:
        assistant_id: Assistant identifier from URL path
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantResponse with assistant information

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if access denied (assistant exists but user lacks permission)
            - 404 if assistant not found
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("GET /assistants/{assistant_id}")

    try:
        # First check if assistant exists (without access check)
        exists = await assistant_exists(assistant_id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # Assistant exists, now check access
        assistant = await get_assistant_with_access_check(assistant_id=assistant_id, user_id=user_id, user_email=current_user.email)

        if not assistant:
            # Assistant exists but access is denied
            raise HTTPException(status_code=403, detail=f"Access denied: You do not have permission to access this assistant")

        # Convert to response model (excludes owner_id for privacy)
        assistant_dict = assistant.model_dump(by_alias=True, exclude={"ownerId"})
        return AssistantResponse.model_validate(assistant_dict)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve assistant: {str(e)}")


@router.put("/{assistant_id}", response_model=AssistantResponse, response_model_exclude_none=True)
async def update_assistant_endpoint(assistant_id: str, request: UpdateAssistantRequest, current_user: User = Depends(get_current_user)):
    """
    Update an assistant (deep merge).

    Requires JWT authentication. Users can only update their own assistants.
    Only provided fields are updated; existing fields are preserved.

    This can be used to:
    - Complete a draft assistant (set status=COMPLETE with full fields)
    - Update any assistant fields
    - Transition status from DRAFT to COMPLETE

    Args:
        assistant_id: Assistant identifier from URL path
        request: UpdateAssistantRequest with fields to update (all optional)
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantResponse with updated assistant information

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("PUT /assistants/{assistant_id}")

    try:
        # Update assistant
        # Note: vector_index_id is not user-configurable - it's set automatically from environment
        updated_assistant = await update_assistant(
            assistant_id=assistant_id,
            owner_id=user_id,
            name=request.name,
            description=request.description,
            instructions=request.instructions,
            visibility=request.visibility,
            tags=request.tags,
            starters=request.starters,
            emoji=request.emoji,
            status=request.status,
            image_url=request.image_url,
        )

        if not updated_assistant:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # Convert to response model
        return AssistantResponse.model_validate(updated_assistant.model_dump(by_alias=True))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update assistant: {str(e)}")


@router.delete("/{assistant_id}", status_code=204)
async def delete_assistant_endpoint(assistant_id: str, current_user: User = Depends(get_current_user)):
    """Delete an assistant and all associated documents using soft-delete + background cleanup."""
    user_id = current_user.user_id
    logger.info("DELETE /assistants/{assistant_id}")

    try:
        # 1. List all documents for the assistant
        docs, _ = await list_assistant_documents(
            assistant_id=assistant_id,
            owner_id=user_id,
            limit=1000,
        )

        # 2. Batch soft-delete all documents with TTL
        if docs:
            from apis.app_api.documents.services.document_service import batch_soft_delete_documents
            await batch_soft_delete_documents(
                assistant_id=assistant_id,
                document_ids=[doc.document_id for doc in docs],
            )

        # 3. Hard-delete assistant record
        success = await delete_assistant(assistant_id=assistant_id, owner_id=user_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # 4. Fire-and-forget background cleanup for all documents
        if docs:
            from apis.app_api.documents.services.cleanup_service import cleanup_assistant_documents
            asyncio.ensure_future(cleanup_assistant_documents(assistant_id, docs))

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete assistant: {str(e)}")


@router.post("/{assistant_id}/test-chat")
async def test_chat_endpoint(assistant_id: str, request: AssistantTestChatRequest, current_user: User = Depends(get_current_user)):
    """
    Test chat endpoint for assistants with RAG functionality.

    This endpoint allows users to test their assistant's RAG capabilities
    by querying the vector store and getting responses augmented with
    retrieved context. Messages are ephemeral and not persisted.

    Requires:
    - Assistant must exist and be owned by the user
    - Assistant must have at least one processed document (status='complete')

    Args:
        assistant_id: Assistant identifier from URL path
        request: AssistantTestChatRequest with message and optional session_id
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        StreamingResponse with SSE events (same format as main chat)

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 400 if assistant has no processed documents
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("POST /assistants/{assistant_id}/test-chat")

    try:
        # 1. Get assistant and verify ownership
        assistant = await get_assistant(assistant_id=assistant_id, owner_id=user_id)

        if not assistant:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # 2. Check if assistant has processed documents
        documents, _ = await list_assistant_documents(
            assistant_id=assistant_id,
            owner_id=user_id,
            limit=100,  # Check up to 100 documents
        )

        processed_documents = [doc for doc in documents if doc.status == "complete"]
        if not processed_documents:
            raise HTTPException(status_code=400, detail="Assistant has no processed documents. Please upload and process documents before testing.")

        # 3. Generate session_id if not provided (for ephemeral chat)
        session_id = request.session_id or f"test-{uuid.uuid4().hex[:12]}"

        # 4. Search vector store for relevant context
        context_chunks = await search_assistant_knowledgebase_with_formatting(assistant_id=assistant_id, query=request.message, top_k=5)

        # 5. Augment user message with retrieved context
        augmented_message = augment_prompt_with_context(user_message=request.message, context_chunks=context_chunks)

        # 6. Create agent with assistant's instructions as system prompt
        agent = get_agent(
            session_id=session_id,
            user_id=user_id,
            enabled_tools=None,  # No tools for test chat
            system_prompt=assistant.instructions,  # Use assistant's custom instructions
            caching_enabled=False,  # Disable caching for test chat
        )

        # 7. Stream response using existing infrastructure
        async def stream_response():
            # Send debug event with RAG context information
            debug_data = {
                "type": "rag_debug",
                "chunk_count": len(context_chunks),
                "chunks": [
                    {
                        "index": i + 1,
                        "text": chunk.get("text", "")[:500] + ("..." if len(chunk.get("text", "")) > 500 else ""),  # Truncate to 500 chars
                        "distance": chunk.get("distance"),
                        "key": chunk.get("key", ""),
                        "source": chunk.get("metadata", {}).get("source", "unknown"),
                    }
                    for i, chunk in enumerate(context_chunks)
                ],
            }
            yield f"event: debug\ndata: {json.dumps(debug_data)}\n\n"

            try:
                stream_iterator = agent.stream_async(augmented_message, session_id=session_id, files=None)

                # Add timeout to prevent hanging streams
                async with asyncio.timeout(600):  # 10 minutes
                    async for event in stream_iterator:
                        yield event

            except asyncio.TimeoutError:
                logger.error(f"⏱️ Stream timeout for test chat session {session_id}")
                error_event = build_conversational_error_event(
                    code=ErrorCode.TIMEOUT, error=Exception("Stream processing time exceeded 600 seconds"), session_id=session_id, recoverable=True
                )
                async for event in stream_conversational_message(
                    message=error_event.message,
                    stop_reason="error",
                    metadata_event=error_event,
                    session_id=session_id,
                    user_id=user_id,
                    user_input=request.message,
                ):
                    yield event

            except Exception as e:
                logger.error(f"Error during test chat streaming: {e}", exc_info=True)
                error_event = build_conversational_error_event(code=ErrorCode.STREAM_ERROR, error=e, session_id=session_id, recoverable=True)
                async for event in stream_conversational_message(
                    message=error_event.message,
                    stop_reason="error",
                    metadata_event=error_event,
                    session_id=session_id,
                    user_id=user_id,
                    user_input=request.message,
                ):
                    yield event

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": session_id},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in test_chat_endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to process test chat: {str(e)}")


@router.post("/{assistant_id}/shares", response_model=AssistantSharesResponse)
async def share_assistant_endpoint(assistant_id: str, request: ShareAssistantRequest, current_user: User = Depends(get_current_user)):
    """
    Share an assistant with specified email addresses.

    Requires JWT authentication. Only the owner can share their assistant.
    Creates share records for each email address. Emails are normalized to lowercase.

    Args:
        assistant_id: Assistant identifier
        request: ShareAssistantRequest with list of email addresses
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantSharesResponse with updated list of shared emails

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("POST /assistants/{assistant_id}/shares")

    try:
        # Share assistant with emails
        success = await share_assistant(assistant_id=assistant_id, owner_id=user_id, emails=request.emails)

        if not success:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # Get updated share list
        shared_emails = await list_assistant_shares(assistant_id, user_id)

        return AssistantSharesResponse(assistant_id=assistant_id, shared_with=shared_emails)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sharing assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to share assistant: {str(e)}")


@router.delete("/{assistant_id}/shares", response_model=AssistantSharesResponse)
async def unshare_assistant_endpoint(assistant_id: str, request: UnshareAssistantRequest, current_user: User = Depends(get_current_user)):
    """
    Remove shares from an assistant for specified email addresses.

    Requires JWT authentication. Only the owner can unshare their assistant.

    Args:
        assistant_id: Assistant identifier
        request: UnshareAssistantRequest with list of email addresses to remove
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantSharesResponse with updated list of shared emails

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("DELETE /assistants/{assistant_id}/shares")

    try:
        # Unshare assistant from emails
        success = await unshare_assistant(assistant_id=assistant_id, owner_id=user_id, emails=request.emails)

        if not success:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # Get updated share list
        shared_emails = await list_assistant_shares(assistant_id, user_id)

        return AssistantSharesResponse(assistant_id=assistant_id, shared_with=shared_emails)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unsharing assistant: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to unshare assistant: {str(e)}")


@router.get("/{assistant_id}/shares", response_model=AssistantSharesResponse)
async def get_assistant_shares_endpoint(assistant_id: str, current_user: User = Depends(get_current_user)):
    """
    Get list of email addresses an assistant is shared with.

    Requires JWT authentication. Only the owner can view the share list.

    Args:
        assistant_id: Assistant identifier
        current_user: Authenticated user from JWT token (injected by dependency)

    Returns:
        AssistantSharesResponse with list of shared emails

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info("GET /assistants/{assistant_id}/shares")

    try:
        # Get share list
        shared_emails = await list_assistant_shares(assistant_id, user_id)

        # Verify ownership (list_assistant_shares checks this, but we want to return 404 if not found)
        assistant = await get_assistant(assistant_id, user_id)
        if not assistant:
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        return AssistantSharesResponse(assistant_id=assistant_id, shared_with=shared_emails)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting assistant shares: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get assistant shares: {str(e)}")
