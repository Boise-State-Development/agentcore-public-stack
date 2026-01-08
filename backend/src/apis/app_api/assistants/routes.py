"""Assistants API routes

Provides endpoints for managing AI assistants (CRUD operations).
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from typing import Optional
import logging
import uuid
import asyncio
import json

from .models import (
    CreateAssistantDraftRequest,
    CreateAssistantRequest,
    UpdateAssistantRequest,
    AssistantResponse,
    AssistantsListResponse,
    AssistantTestChatRequest
)
from .services.assistant_service import (
    create_assistant_draft,
    create_assistant,
    get_assistant,
    update_assistant,
    list_user_assistants,
    archive_assistant,
    delete_assistant
)
from .services.rag_service import (
    search_assistant_knowledgebase_with_formatting,
    augment_prompt_with_context
)
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.inference_api.chat.service import get_agent
from apis.app_api.documents.services.document_service import list_assistant_documents
from apis.shared.errors import (
    ErrorCode,
    build_conversational_error_event
)
from apis.inference_api.chat.routes import stream_conversational_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistants", tags=["assistants"])


@router.post("/draft", response_model=AssistantResponse, response_model_exclude_none=True)
async def create_assistant_draft_endpoint(
    request: CreateAssistantDraftRequest,
    current_user: User = Depends(get_current_user)
):
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
        assistant = await create_assistant_draft(
            owner_id=user_id,
            owner_name=current_user.name,
            name=request.name
        )
        
        # Convert to response model (excludes owner_id for privacy)
        assistant_dict = assistant.model_dump(by_alias=True, exclude={'ownerId'})
        return AssistantResponse.model_validate(assistant_dict)
    
    except Exception as e:
        logger.error(f"Error creating draft assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create draft assistant: {str(e)}"
        )


@router.post("", response_model=AssistantResponse, response_model_exclude_none=True)
async def create_assistant_endpoint(
    request: CreateAssistantRequest,
    current_user: User = Depends(get_current_user)
):
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
        # Note: vector_index_id is automatically set from ASSISTANTS_VECTOR_STORE_INDEX_NAME env var
        assistant = await create_assistant(
            owner_id=user_id,
            owner_name=current_user.name,
            name=request.name,
            description=request.description,
            instructions=request.instructions,
            visibility=request.visibility,
            tags=request.tags
        )
        
        # Convert to response model (excludes owner_id for privacy)
        assistant_dict = assistant.model_dump(by_alias=True, exclude={'ownerId'})
        return AssistantResponse.model_validate(assistant_dict)
    
    except Exception as e:
        logger.error(f"Error creating assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create assistant: {str(e)}"
        )


@router.get("", response_model=AssistantsListResponse, response_model_exclude_none=True)
async def list_assistants_endpoint(
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of assistants to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for retrieving the next page"),
    include_archived: bool = Query(False, description="Include archived assistants"),
    include_drafts: bool = Query(False, description="Include draft assistants"),
    current_user: User = Depends(get_current_user)
):
    """
    List assistants for the authenticated user with pagination support.
    
    Requires JWT authentication. Returns only assistants belonging to the
    authenticated user, sorted by created_at descending (most recent first).
    
    By default, excludes draft and archived assistants. Use query parameters
    to include them.
    
    Args:
        limit: Maximum number of assistants to return (optional, 1-1000)
        next_token: Pagination token for retrieving next page (optional)
        include_archived: Whether to include archived assistants (default: False)
        include_drafts: Whether to include draft assistants (default: False)
        current_user: Authenticated user from JWT token (injected by dependency)
    
    Returns:
        AssistantsListResponse with paginated assistants and next_token if more exist
    
    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id
    
    logger.info(f"GET /assistants - User: {user_id}, Limit: {limit}, NextToken: {next_token}, "
                f"IncludeArchived: {include_archived}, IncludeDrafts: {include_drafts}")
    
    try:
        # Retrieve assistants for the user with pagination
        assistants, next_page_token = await list_user_assistants(
            owner_id=user_id,
            limit=limit,
            next_token=next_token,
            include_archived=include_archived,
            include_drafts=include_drafts
        )
        
        # Convert to response models (excludes owner_id for privacy)
        assistant_responses = [
            AssistantResponse.model_validate(
                assistant.model_dump(by_alias=True, exclude={'ownerId'})
            )
            for assistant in assistants
        ]
        
        return AssistantsListResponse(
            assistants=assistant_responses,
            next_token=next_page_token
        )
    
    except Exception as e:
        logger.error(f"Error listing assistants: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list assistants: {str(e)}"
        )


@router.get("/{assistant_id}", response_model=AssistantResponse, response_model_exclude_none=True)
async def get_assistant_endpoint(
    assistant_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve a specific assistant by ID.
    
    Requires JWT authentication. Users can only access their own assistants.
    
    Args:
        assistant_id: Assistant identifier from URL path
        current_user: Authenticated user from JWT token (injected by dependency)
    
    Returns:
        AssistantResponse with assistant information
    
    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id
    
    logger.info(f"GET /assistants/{assistant_id} - User: {user_id}")
    
    try:
        # Retrieve assistant
        assistant = await get_assistant(
            assistant_id=assistant_id,
            owner_id=user_id
        )
        
        if not assistant:
            raise HTTPException(
                status_code=404,
                detail=f"Assistant not found: {assistant_id}"
            )
        
        # Convert to response model (excludes owner_id for privacy)
        assistant_dict = assistant.model_dump(by_alias=True, exclude={'ownerId'})
        return AssistantResponse.model_validate(assistant_dict)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve assistant: {str(e)}"
        )


@router.put("/{assistant_id}", response_model=AssistantResponse, response_model_exclude_none=True)
async def update_assistant_endpoint(
    assistant_id: str,
    request: UpdateAssistantRequest,
    current_user: User = Depends(get_current_user)
):
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
    
    logger.info(f"PUT /assistants/{assistant_id} - User: {user_id}")
    
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
            status=request.status
        )
        
        if not updated_assistant:
            raise HTTPException(
                status_code=404,
                detail=f"Assistant not found: {assistant_id}"
            )
        
        # Convert to response model
        return AssistantResponse.model_validate(
            updated_assistant.model_dump(by_alias=True)
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update assistant: {str(e)}"
        )


@router.post("/{assistant_id}/archive", response_model=AssistantResponse, response_model_exclude_none=True)
async def archive_assistant_endpoint(
    assistant_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Archive an assistant (soft delete).
    
    Sets the assistant status to ARCHIVED. The assistant will not appear
    in default listings but can still be retrieved by ID and can be
    un-archived by setting status back to COMPLETE.
    
    Requires JWT authentication. Users can only archive their own assistants.
    
    Args:
        assistant_id: Assistant identifier from URL path
        current_user: Authenticated user from JWT token (injected by dependency)
    
    Returns:
        AssistantResponse with status=ARCHIVED
    
    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id
    
    logger.info(f"POST /assistants/{assistant_id}/archive - User: {user_id}")
    
    try:
        # Archive assistant (soft delete)
        archived_assistant = await archive_assistant(
            assistant_id=assistant_id,
            owner_id=user_id
        )
        
        if not archived_assistant:
            raise HTTPException(
                status_code=404,
                detail=f"Assistant not found: {assistant_id}"
            )
        
        # Convert to response model
        return AssistantResponse.model_validate(
            archived_assistant.model_dump(by_alias=True)
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error archiving assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to archive assistant: {str(e)}"
        )


@router.delete("/{assistant_id}", status_code=204)
async def delete_assistant_endpoint(
    assistant_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete an assistant permanently (hard delete).
    
    This is irreversible. The assistant and all associated data will be
    permanently removed. Consider using POST /assistants/{id}/archive for
    soft deletion instead.
    
    Requires JWT authentication. Users can only delete their own assistants.
    
    Args:
        assistant_id: Assistant identifier from URL path
        current_user: Authenticated user from JWT token (injected by dependency)
    
    Returns:
        204 No Content on successful deletion
    
    Raises:
        HTTPException:
            - 401 if not authenticated
            - 404 if assistant not found or not owned by user
            - 500 if server error
    """
    user_id = current_user.user_id
    
    logger.info(f"DELETE /assistants/{assistant_id} - User: {user_id}")
    
    try:
        # Delete assistant permanently (hard delete)
        success = await delete_assistant(
            assistant_id=assistant_id,
            owner_id=user_id
        )
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Assistant not found: {assistant_id}"
            )
        
        # Return 204 No Content (no response body)
        return None
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete assistant: {str(e)}"
        )


@router.post("/{assistant_id}/test-chat")
async def test_chat_endpoint(
    assistant_id: str,
    request: AssistantTestChatRequest,
    current_user: User = Depends(get_current_user)
):
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
    
    logger.info(f"POST /assistants/{assistant_id}/test-chat - User: {user_id}, Message: {request.message[:50]}...")
    
    try:
        # 1. Get assistant and verify ownership
        assistant = await get_assistant(
            assistant_id=assistant_id,
            owner_id=user_id
        )
        
        if not assistant:
            raise HTTPException(
                status_code=404,
                detail=f"Assistant not found: {assistant_id}"
            )
        
        # 2. Check if assistant has processed documents
        documents, _ = await list_assistant_documents(
            assistant_id=assistant_id,
            owner_id=user_id,
            limit=100  # Check up to 100 documents
        )
        
        processed_documents = [doc for doc in documents if doc.status == 'complete']
        if not processed_documents:
            raise HTTPException(
                status_code=400,
                detail="Assistant has no processed documents. Please upload and process documents before testing."
            )
        
        # 3. Generate session_id if not provided (for ephemeral chat)
        session_id = request.session_id or f"test-{uuid.uuid4().hex[:12]}"
        
        # 4. Search vector store for relevant context
        context_chunks = await search_assistant_knowledgebase_with_formatting(
            assistant_id=assistant_id,
            query=request.message,
            top_k=5
        )
        
        
        
        # 5. Augment user message with retrieved context
        augmented_message = augment_prompt_with_context(
            user_message=request.message,
            context_chunks=context_chunks
        )
        
        # 6. Create agent with assistant's instructions as system prompt
        agent = get_agent(
            session_id=session_id,
            user_id=user_id,
            enabled_tools=None,  # No tools for test chat
            system_prompt=assistant.instructions,  # Use assistant's custom instructions
            caching_enabled=False  # Disable caching for test chat
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
                        "text": chunk.get('text', '')[:500] + ('...' if len(chunk.get('text', '')) > 500 else ''),  # Truncate to 500 chars
                        "distance": chunk.get('distance'),
                        "key": chunk.get('key', ''),
                        "source": chunk.get('metadata', {}).get('source', 'unknown')
                    }
                    for i, chunk in enumerate(context_chunks)
                ]
            }
            yield f"event: debug\ndata: {json.dumps(debug_data)}\n\n"
            
            try:
                stream_iterator = agent.stream_async(
                    augmented_message,
                    session_id=session_id,
                    files=None
                )
                
                # Add timeout to prevent hanging streams
                async with asyncio.timeout(600):  # 10 minutes
                    async for event in stream_iterator:
                        yield event
                        
            except asyncio.TimeoutError:
                logger.error(f"⏱️ Stream timeout for test chat session {session_id}")
                error_event = build_conversational_error_event(
                    code=ErrorCode.TIMEOUT,
                    error=Exception("Stream processing time exceeded 600 seconds"),
                    session_id=session_id,
                    recoverable=True
                )
                async for event in stream_conversational_message(
                    message=error_event.message,
                    stop_reason="error",
                    metadata_event=error_event,
                    session_id=session_id,
                    user_id=user_id,
                    user_input=request.message
                ):
                    yield event
                    
            except Exception as e:
                logger.error(f"Error during test chat streaming: {e}", exc_info=True)
                error_event = build_conversational_error_event(
                    code=ErrorCode.STREAM_ERROR,
                    error=e,
                    session_id=session_id,
                    recoverable=True
                )
                async for event in stream_conversational_message(
                    message=error_event.message,
                    stop_reason="error",
                    metadata_event=error_event,
                    session_id=session_id,
                    user_id=user_id,
                    user_input=request.message
                ):
                    yield event
        
        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-ID": session_id
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in test_chat_endpoint: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process test chat: {str(e)}"
        )

