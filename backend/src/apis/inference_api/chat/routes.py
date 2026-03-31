"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

import json
import logging
import os
from typing import AsyncGenerator, Union

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from agents.main_agent.session.session_factory import SessionFactory
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User
from apis.shared.errors import (
    ConversationalErrorEvent,
    ErrorCode,
    build_conversational_error_event,
)
from apis.shared.files.file_resolver import get_file_resolver
from apis.shared.models.managed_models import list_managed_models
from apis.shared.quota import (
    QuotaExceededEvent,
    build_no_quota_configured_event,
    build_quota_exceeded_event,
    build_quota_warning_event,
    get_quota_checker,
    is_quota_enforcement_enabled,
)

from apis.shared.rbac.service import get_app_role_service

from .models import FileContent, InvocationRequest
from .service import get_agent

logger = logging.getLogger(__name__)

# Router with no prefix - endpoints will be at root level
router = APIRouter(tags=["agentcore-runtime"])

# ============================================================
# Preview Session Detection
# ============================================================

# Preview session prefix - sessions with this prefix skip persistence
PREVIEW_SESSION_PREFIX = "preview-"


def is_preview_session(session_id: str) -> bool:
    """Check if a session ID is a preview session (should skip persistence).

    Preview sessions are used for assistant testing in the form builder.
    They allow full agent functionality but don't save to user's conversation history.
    """
    return session_id.startswith(PREVIEW_SESSION_PREFIX)


async def _resolve_caching_enabled(model_id: str | None, explicit_caching_enabled: bool | None) -> bool | None:
    """
    Resolve whether caching should be enabled for a request.

    Priority:
    1. If explicitly set in request, use that value
    2. If model_id provided, look up the managed model's supports_caching field
    3. Otherwise return None (let agent use default)

    Args:
        model_id: The model ID from the request
        explicit_caching_enabled: Explicit caching setting from request

    Returns:
        bool or None: Whether caching should be enabled
    """
    # If explicitly set in request, use that value
    if explicit_caching_enabled is not None:
        return explicit_caching_enabled

    # If no model_id, let agent use default
    if not model_id:
        return None

    # Look up the managed model to check supports_caching
    try:
        managed_models = await list_managed_models()
        for model in managed_models:
            if model.model_id == model_id:
                logger.debug("Found managed model, checking supports_caching")
                return model.supports_caching

        # Model not found in managed models - use default
        logger.debug("Model not found in managed models, using default caching behavior")
        return None

    except Exception as e:
        logger.warning("Failed to look up managed model for caching")
        return None


# ============================================================
# Helper Functions for Streaming Error/Status Messages
# ============================================================


async def stream_conversational_message(
    message: str,
    stop_reason: str,
    metadata_event: Union[QuotaExceededEvent, ConversationalErrorEvent, None],
    session_id: str,
    user_id: str,
    user_input: str,
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

    # Skip persistence for preview sessions
    if is_preview_session(session_id):
        logger.info("Preview session - skipping message persistence")
        return

    # Save messages to session for persistence
    try:
        from strands.types.content import Message
        from strands.types.session import SessionMessage

        session_manager = SessionFactory.create_session_manager(session_id=session_id, user_id=user_id, caching_enabled=False)

        # Save user message
        user_message: Message = {"role": "user", "content": [{"text": user_input}]}

        # Save assistant message
        assistant_message: Message = {"role": "assistant", "content": [{"text": message}]}

        # Use base_manager's create_message for persistence (AgentCore Memory)
        if hasattr(session_manager, "base_manager") and hasattr(session_manager.base_manager, "create_message"):
            user_session_msg = SessionMessage.from_message(user_message, index=0)
            assistant_session_msg = SessionMessage.from_message(assistant_message, index=1)

            session_manager.base_manager.create_message(session_id, "default", user_session_msg)
            session_manager.base_manager.create_message(session_id, "default", assistant_session_msg)
            logger.info("Saved messages to session")

    except Exception as e:
        logger.error("Failed to save messages to session", exc_info=True)


# ============================================================
# AgentCore Runtime Standard Endpoints (REQUIRED)
# ============================================================


@router.get("/ping")
async def ping():
    """Health check endpoint (required by AgentCore Runtime)"""
    return {"status": "healthy", "version": os.environ.get("APP_VERSION", "unknown")}


@router.post("/invocations")
async def invocations(request: InvocationRequest, current_user: User = Depends(get_current_user_trusted)):
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
    auth_token = current_user.raw_token
    logger.info("Invocation request received")
    logger.info("Message received")

    if input_data.enabled_tools:
        logger.info(f"Enabled tools ({len(input_data.enabled_tools)})")

    if input_data.files:
        logger.info(f"Files attached: {len(input_data.files)} files")
        for file in input_data.files:
            logger.info("  - File attached")

    if input_data.file_upload_ids:
        logger.info(f"File upload IDs: {len(input_data.file_upload_ids)} IDs to resolve")

    # Resolve file upload IDs to FileContent objects
    files_to_send = list(input_data.files) if input_data.files else []

    if input_data.file_upload_ids:
        try:
            file_resolver = get_file_resolver()
            resolved_files = await file_resolver.resolve_files(
                user_id=user_id,
                upload_ids=input_data.file_upload_ids,
                max_files=5,  # Bedrock document limit
            )
            # Convert ResolvedFileContent to FileContent
            for rf in resolved_files:
                files_to_send.append(FileContent(filename=rf.filename, content_type=rf.content_type, bytes=rf.bytes))
            logger.info(f"Resolved {len(resolved_files)} files from upload IDs")
        except Exception as e:
            logger.warning("Failed to resolve file upload IDs")
            # Continue without files rather than failing the request

    # Check quota if enforcement is enabled
    quota_warning_event = None
    quota_exceeded_event = None
    if is_quota_enforcement_enabled():
        try:
            quota_checker = get_quota_checker()
            quota_result = await quota_checker.check_quota(user=current_user, session_id=input_data.session_id)

            if not quota_result.allowed:
                # Quota blocked - stream as SSE instead of 429 for better UX
                logger.warning("Quota blocked for user")
                if quota_result.tier is None:
                    # No quota tier configured for this user
                    quota_exceeded_event = build_no_quota_configured_event(quota_result)
                else:
                    # Quota limit exceeded
                    quota_exceeded_event = build_quota_exceeded_event(quota_result)
            else:
                # Check for warning level
                quota_warning_event = build_quota_warning_event(quota_result)
                if quota_warning_event:
                    logger.info("Quota warning for user")

        except Exception as e:
            # Log error but don't block request - fail open for quota errors
            logger.error("Error checking quota for user", exc_info=True)

    # If quota exceeded, stream the quota exceeded message instead of agent response
    if quota_exceeded_event:
        return StreamingResponse(
            stream_conversational_message(
                message=quota_exceeded_event.message,
                stop_reason="quota_exceeded",
                metadata_event=quota_exceeded_event,
                session_id=input_data.session_id,
                user_id=user_id,
                user_input=input_data.message,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": input_data.session_id},
        )

    # Check model access if a specific model_id is requested
    if input_data.model_id:
        app_role_service = get_app_role_service()
        if not await app_role_service.can_access_model(current_user, input_data.model_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to model: {input_data.model_id}",
            )

    # Handle assistant RAG integration if assistant_id is provided
    # Import here to avoid circular import (app_api.assistants imports from inference_api.chat.routes)
    assistant = None
    context_chunks = None
    augmented_message = input_data.message
    system_prompt = input_data.system_prompt  # Start with provided system prompt

    logger.info(
        "Invocation request - processing with assistant context"
    )

    if input_data.rag_assistant_id:
        # Local imports to avoid circular dependency
        from apis.shared.assistants.rag_service import (
            augment_prompt_with_context,
            search_assistant_knowledgebase_with_formatting,
        )
        from apis.shared.assistants.service import (
            get_assistant_with_access_check,
            mark_share_as_interacted,
        )
        from apis.shared.sessions.messages import get_messages
        from apis.shared.sessions.metadata import (
            get_session_metadata,
            store_session_metadata,
        )
        from apis.shared.sessions.models import (
            SessionMetadata,
            SessionPreferences,
        )

        logger.info("Assistant RAG requested")
        logger.info("Processing for authenticated user")

        # 1. Check if session already has an assistant attached
        # If it does, verify it's the same assistant (can't change assistants mid-session)
        # If it doesn't, verify session has no messages (can only attach to new sessions)
        # Skip validation for preview sessions (they don't persist state)
        if not is_preview_session(input_data.session_id):
            try:
                existing_metadata = await get_session_metadata(input_data.session_id, user_id)
                existing_assistant_id = existing_metadata.preferences.assistant_id if existing_metadata and existing_metadata.preferences else None

                if existing_assistant_id:
                    # Session already has an assistant - verify it's the same one
                    if existing_assistant_id != input_data.rag_assistant_id:
                        logger.warning(
                            "Attempted to change assistant mid-session"
                        )
                        raise HTTPException(
                            status_code=400, detail="Cannot change assistants mid-session. Start a new session to use a different assistant."
                        )
                    # Same assistant - allow it to continue
                    logger.info("Continuing with existing assistant in session")
                else:
                    # No assistant attached - verify session has no messages (can only attach to new sessions)
                    messages_response = await get_messages(
                        session_id=input_data.session_id,
                        user_id=user_id,
                        limit=1,  # Only need to check if any messages exist
                    )
                    if messages_response.messages and len(messages_response.messages) > 0:
                        logger.warning(
                            "Attempted to attach assistant to session with existing messages"
                        )
                        raise HTTPException(
                            status_code=400, detail="Assistants can only be attached to new sessions, start a new session to chat with this assistant"
                        )
            except HTTPException:
                raise
            except Exception as e:
                logger.error("Error checking session state", exc_info=True)
                # Continue anyway - better to allow than block on error
        else:
            logger.info("Preview session - skipping session state validation")

        # 2. Load assistant with access check
        logger.info("Loading assistant with access check...")
        assistant = await get_assistant_with_access_check(assistant_id=input_data.rag_assistant_id, user_id=user_id, user_email=current_user.email)

        if not assistant:
            logger.warning("get_assistant_with_access_check returned None")
            # Check if assistant exists at all to provide better error message
            from apis.shared.assistants.service import assistant_exists

            exists = await assistant_exists(input_data.rag_assistant_id)

            if not exists:
                logger.warning("Assistant does not exist (404)")
                raise HTTPException(status_code=404, detail=f"Assistant not found: {input_data.rag_assistant_id}")
            else:
                logger.warning("Access denied to assistant (403)")
                raise HTTPException(status_code=403, detail=f"Access denied: You do not have permission to access this assistant")

        # Log assistant details for debugging
        logger.info("Assistant loaded successfully!")
        logger.info("Assistant details retrieved")
        logger.info("Assistant name retrieved")
        logger.info("Assistant owner retrieved")
        logger.info("Assistant visibility retrieved")
        logger.info("Assistant instructions retrieved")
        logger.info("Assistant instructions length retrieved")
        logger.info("Assistant vector index retrieved")

        # Mark as viewed if this is a shared assistant (not owned)
        if assistant.owner_id != user_id:
            await mark_share_as_interacted(assistant_id=input_data.rag_assistant_id, user_email=current_user.email)

        # 3. Search assistant knowledge base
        logger.info("Starting knowledge base search for assistant...")
        try:
            logger.info("Searching knowledge base for assistant...")
            context_chunks = await search_assistant_knowledgebase_with_formatting(
                assistant_id=input_data.rag_assistant_id, query=input_data.message, top_k=5
            )
            logger.info(f"Knowledge base search returned {len(context_chunks) if context_chunks else 0} chunks")
            if context_chunks:
                for i, chunk in enumerate(context_chunks):
                    logger.info(f"Chunk {i + 1} retrieved")
                    logger.info(f"Chunk {i + 1} metadata retrieved")

            # 4. Augment message with context
            if context_chunks:
                augmented_message = augment_prompt_with_context(user_message=input_data.message, context_chunks=context_chunks)
                logger.info(
                    f"Augmented message with {len(context_chunks)} context chunks"
                )
                logger.info("Augmented message preview available")
            else:
                logger.info("No context chunks found for assistant - using original message without augmentation")
        except Exception as e:
            logger.error("Error searching assistant knowledge base", exc_info=True)
            logger.error(f"Exception type: {type(e).__name__}")
            # Continue without RAG context rather than failing

        # 5. Append assistant's instructions to the base system prompt (don't replace)
        # For preview sessions, prefer the system_prompt from the request (live form edits)
        # over the saved assistant instructions, so users can test changes before saving.
        logger.info("Checking assistant instructions...")
        preview_instructions_override = input_data.system_prompt if is_preview_session(input_data.session_id) and input_data.system_prompt else None
        effective_instructions = preview_instructions_override or assistant.instructions

        if effective_instructions:
            # Import here to avoid circular dependency
            from agents.main_agent.core.system_prompt_builder import SystemPromptBuilder

            # Build the base prompt with date
            base_prompt_builder = SystemPromptBuilder()
            base_prompt = base_prompt_builder.build(include_date=True)

            # Append assistant instructions to the base prompt
            system_prompt = f"{base_prompt}\n\n## Assistant-Specific Instructions\n\n{effective_instructions}"
            if preview_instructions_override:
                logger.info(
                    "Using live preview instructions override"
                )
            else:
                logger.info(
                    "Appended assistant instructions to base system prompt"
                )
            logger.info("Final system prompt built")
        else:
            # No assistant instructions - use base prompt if no system_prompt provided
            logger.warning("No instructions found on assistant!")
            if not system_prompt:
                from agents.main_agent.core.system_prompt_builder import SystemPromptBuilder

                base_prompt_builder = SystemPromptBuilder()
                system_prompt = base_prompt_builder.build(include_date=True)
            logger.info(
                "Assistant has no instructions - using fallback system prompt"
            )

        # 6. Save assistant_id to session preferences (persist for future loads)
        # Skip persistence for preview sessions
        if not is_preview_session(input_data.session_id):
            try:
                existing_metadata = await get_session_metadata(input_data.session_id, user_id)
                if existing_metadata:
                    # Update existing metadata with assistant_id in preferences
                    prefs_dict = existing_metadata.preferences.model_dump(by_alias=False) if existing_metadata.preferences else {}
                    prefs_dict["assistant_id"] = input_data.rag_assistant_id

                    updated_metadata = existing_metadata.model_copy(update={"assistant_id": input_data.rag_assistant_id})

                else:
                    # Create new metadata with assistant_id in preferences
                    from datetime import datetime, timezone

                    now = datetime.now(timezone.utc).isoformat()
                    preferences = SessionPreferences(assistantId=input_data.rag_assistant_id)

                    updated_metadata = SessionMetadata(
                        sessionId=input_data.session_id,
                        userId=user_id,
                        title="",
                        status="active",
                        createdAt=now,
                        lastMessageAt=now,
                        messageCount=0,
                        starred=False,
                        tags=[],
                        preferences=preferences,
                        deleted=None,
                        deletedAt=None,
                    )

                await store_session_metadata(session_id=input_data.session_id, user_id=user_id, session_metadata=updated_metadata)
                logger.info("Saved assistant_id to session preferences")
            except Exception as e:
                logger.error("Failed to save assistant_id to session preferences", exc_info=True)
                # Continue - not critical if metadata save fails
        else:
            logger.info("Preview session - skipping assistant_id persistence")

    try:
        # Resolve caching_enabled based on managed model configuration
        # This allows admins to disable caching for models that don't support it
        caching_enabled = await _resolve_caching_enabled(model_id=input_data.model_id, explicit_caching_enabled=input_data.caching_enabled)

        if caching_enabled is False:
            logger.info("Prompt caching disabled for model")

        # Get agent instance with user-specific configuration
        # AgentCore Memory tracks preferences across sessions per user_id
        # Supports multiple LLM providers: AWS Bedrock, OpenAI, and Google Gemini
        # Use augmented message and assistant system prompt if assistant RAG was applied
        agent = get_agent(
            session_id=input_data.session_id,
            user_id=user_id,
            auth_token=auth_token,
            enabled_tools=input_data.enabled_tools,
            model_id=input_data.model_id,
            temperature=input_data.temperature,
            system_prompt=system_prompt,  # Use assistant's instructions if available
            caching_enabled=caching_enabled,
            provider=input_data.provider,
            max_tokens=input_data.max_tokens,
        )

        # Build citations list for persistence (convert context chunks to citation format)
        citations_for_storage = []
        if context_chunks:
            for chunk in context_chunks:
                citations_for_storage.append(
                    {
                        "assistantId": input_data.rag_assistant_id,
                        "documentId": chunk.get("metadata", {}).get("document_id", ""),
                        "fileName": chunk.get("metadata", {}).get("source", "Unknown Source"),
                        "text": chunk.get("text", "")[:500],  # Limit excerpt length
                    }
                )

        # Create stream with optional quota warning injection
        async def stream_with_quota_warning() -> AsyncGenerator[str, None]:
            """Wrap agent stream to inject quota warning at start if needed"""
            # Yield quota warning event first if applicable
            if quota_warning_event:
                yield quota_warning_event.to_sse_format()

            # Yield citation events BEFORE the agent stream starts
            # This allows the UI to display sources immediately
            if citations_for_storage:
                for citation in citations_for_storage:
                    yield f"event: citation\ndata: {json.dumps(citation)}\n\n"

            # Then yield all agent stream events
            # Use augmented message if assistant RAG was applied
            # Use resolved files (from S3) merged with any direct file content
            #
            # Always store the original user message as displayText when the prompt
            # will be modified before reaching the model. This happens when:
            #   1. RAG augmentation prepends context chunks to the message
            #   2. File attachments cause PromptBuilder to rewrite into ContentBlocks
            # The original text becomes the single source of truth for UI display,
            # while the full augmented prompt stays in AgentCore Memory for the LLM.
            message_will_be_modified = (
                augmented_message != input_data.message  # RAG augmentation
                or bool(files_to_send)                   # File attachments
            )
            async for event in agent.stream_async(
                augmented_message,
                session_id=input_data.session_id,
                files=files_to_send if files_to_send else None,
                citations=citations_for_storage if citations_for_storage else None,
                original_message=input_data.message if message_will_be_modified else None,
            ):
                yield event

        # Stream response from agent as SSE (with optional files)
        # Note: Compression is handled by GZipMiddleware if configured in main.py
        return StreamingResponse(
            stream_with_quota_warning(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": input_data.session_id},
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is (e.g., from auth)
        raise
    except Exception as e:
        # Stream error as a conversational assistant message for better UX
        logger.error("Error in invocations", exc_info=True)

        error_event = build_conversational_error_event(code=ErrorCode.AGENT_ERROR, error=e, session_id=input_data.session_id, recoverable=True)

        return StreamingResponse(
            stream_conversational_message(
                message=error_event.message,
                stop_reason="error",
                metadata_event=error_event,
                session_id=input_data.session_id,
                user_id=user_id,
                user_input=input_data.message,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": input_data.session_id},
        )
