"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

import asyncio
import json
import logging
import os
from typing import AsyncGenerator, Union

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from agents.main_agent.core.model_config import KNOWN_CANONICAL_PARAMS
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
from apis.shared.sessions.metadata import ensure_session_metadata_exists
from apis.shared.user_settings.repository import UserSettingsRepository

from .models import FileContent, InvocationRequest
from .service import generate_conversation_title, get_agent

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


def _sanitize_log(value: object) -> str:
    """Return a log-safe representation of untrusted values.

    Remove line breaks and replace other ASCII control characters so user
    input cannot forge additional log entries or inject terminal controls.
    """
    if value is None:
        return "?"
    text = str(value).replace("\r", "").replace("\n", "")
    control_map = {
        i: "?"
        for i in range(32)
        if i not in (9,)  # keep horizontal tab for readability
    }
    control_map[127] = "?"
    return text.translate(control_map)


async def _find_managed_model(model_id: str | None):
    """Best-effort lookup of a managed-model record by external model ID."""
    if not model_id:
        return None
    try:
        managed_models = await list_managed_models()
        for model in managed_models:
            if model.model_id == model_id:
                return model
    except Exception:
        # model_id is request-controlled; sanitize before logging to keep
        # CRLF / control chars from forging extra log lines.
        logger.warning("Failed to look up managed model %s", _sanitize_log(model_id))
    return None


async def _resolve_user_default_model(user_id: str | None) -> tuple[str | None, str | None]:
    """Look up the user's persisted defaultModelId and resolve its provider.

    Returns ``(model_id, provider)``. When the request does not specify
    ``model_id``, callers fall back to the user's saved preference; if that
    is also unset (or the saved id no longer exists in managed models), the
    callers in turn fall back to the agent factory's hardcoded default.

    The lookup is best-effort: any failure (no table, DynamoDB error, or
    deleted model) returns ``(None, None)`` so the chat turn proceeds on
    the system default rather than being blocked.
    """
    if not user_id:
        return None, None
    try:
        repo = UserSettingsRepository()
        if not repo.enabled:
            return None, None
        settings = await repo.get_settings(user_id)
        saved_id = settings.get("defaultModelId")
    except Exception:
        logger.warning("Failed to load user settings for default model lookup", exc_info=True)
        return None, None
    if not saved_id:
        return None, None

    managed = await _find_managed_model(saved_id)
    provider = managed.provider if managed else None
    return saved_id, provider


def _merge_inference_params(
    managed_model,
    request_params: dict,
) -> dict:
    """Merge admin-configured defaults with request-supplied inference params.

    For each canonical param the managed model declares:
      * unsupported -> drop the request value (logged) and don't set a default
      * supported with admin default -> use the default unless the request
        provides a value within bounds; out-of-bounds values are clamped.

    Request keys for params the managed model says nothing about pass through
    untouched — the per-provider translation table will drop unknowns.
    """
    merged: dict = {}
    spec_map = {}
    if managed_model and managed_model.supported_params:
        spec_map = managed_model.supported_params.params or {}

    seen_keys: set[str] = set()
    for name, spec in spec_map.items():
        seen_keys.add(name)
        if not spec.supported:
            if name in request_params:
                # `name` is a registry-defined canonical key; managed_model.model_id
                # comes from DDB but ultimately traces back to a user-supplied
                # value on create. Sanitize defensively so CodeQL's log-injection
                # check is satisfied uniformly across log sites.
                logger.info(
                    "Dropping unsupported inference param '%s' for model %s",
                    _sanitize_log(name),
                    _sanitize_log(getattr(managed_model, "model_id", "?")),
                )
            continue

        # Locked params always use the admin default — user overrides are
        # dropped without error. Lets admins pin e.g. `temperature` for
        # reproducibility while leaving `max_tokens` user-tunable.
        if spec.locked:
            if spec.default is not None:
                merged[name] = spec.default
            continue

        if name in request_params and request_params[name] is not None:
            value = request_params[name]
            if isinstance(value, (int, float)):
                if spec.min is not None and value < spec.min:
                    value = spec.min
                if spec.max is not None and value > spec.max:
                    value = spec.max
            merged[name] = value
        elif spec.default is not None:
            merged[name] = spec.default

    # Pass through request keys the admin spec doesn't mention, but only when
    # they're in the canonical allow-list. Without this gate, a user could
    # submit a future canonical key (or one a future provider mapping starts
    # forwarding) and bypass the admin's per-model bounds entirely. Unknown
    # keys are dropped here; the provider translation table is the second
    # line of defense for ones it doesn't understand.
    for name, value in request_params.items():
        if name in seen_keys or value is None:
            continue
        if name not in KNOWN_CANONICAL_PARAMS:
            logger.info(
                "Dropping unrecognized inference param '%s' for model %s",
                _sanitize_log(name),
                _sanitize_log(getattr(managed_model, "model_id", "?")),
            )
            continue
        merged[name] = value

    # Final cross-param safety check. Anthropic rejects requests where
    # `thinking.budget_tokens >= max_tokens`, and the per-param clamping
    # above can't catch it (each param is bounded independently). When
    # both are set and inconsistent, drop `thinking` so the response still
    # streams instead of erroring out — the user just doesn't get a
    # reasoning trace this turn. Logged so the gap is visible in metrics.
    thinking = merged.get("thinking")
    max_tokens = merged.get("max_tokens")
    if (
        isinstance(thinking, int)
        and not isinstance(thinking, bool)
        and isinstance(max_tokens, int)
        and not isinstance(max_tokens, bool)
        and thinking >= max_tokens
    ):
        logger.warning(
            "Dropping thinking budget %d for model %s — not less than max_tokens %d",
            thinking,
            _sanitize_log(getattr(managed_model, "model_id", "?")),
            max_tokens,
        )
        merged.pop("thinking", None)

    return merged


async def _resolve_model_settings(
    model_id: str | None,
    explicit_caching_enabled: bool | None,
    request_inference_params: dict | None,
) -> tuple[bool | None, dict]:
    """Resolve runtime model knobs from the managed-model registry.

    Returns ``(caching_enabled, inference_params)``. A single registry lookup
    drives both, replacing the prior per-concern lookups.
    """
    request_params = dict(request_inference_params or {})

    if not model_id:
        return explicit_caching_enabled, request_params

    managed_model = await _find_managed_model(model_id)

    if explicit_caching_enabled is not None:
        caching = explicit_caching_enabled
    elif managed_model is not None:
        caching = managed_model.supports_caching
    else:
        caching = None

    inference_params = _merge_inference_params(managed_model, request_params)
    return caching, inference_params


async def _resolve_caching_enabled(model_id: str | None, explicit_caching_enabled: bool | None) -> bool | None:
    """Backward-compat wrapper around :func:`_resolve_model_settings`."""
    caching, _ = await _resolve_model_settings(model_id, explicit_caching_enabled, None)
    return caching


# ============================================================
# Spreadsheet Analysis Tool Injection
# ============================================================

SPREADSHEET_TOOL_IDS = {"list_spreadsheets", "analyze_spreadsheet"}


def _build_spreadsheet_tools(
    enabled_tools: list | None,
    assistant_id: str | None,
    session_id: str,
    user_id: str,
) -> list:
    """Create context-bound spreadsheet analysis tools if enabled by the user."""
    if not enabled_tools:
        return []

    requested = SPREADSHEET_TOOL_IDS.intersection(enabled_tools)
    if not requested:
        return []

    from agents.builtin_tools.spreadsheet_analysis import make_list_spreadsheets_tool, make_analyze_tool

    tools = []
    if "list_spreadsheets" in requested:
        tools.append(make_list_spreadsheets_tool(assistant_id, session_id, user_id))
    if "analyze_spreadsheet" in requested:
        tools.append(make_analyze_tool(assistant_id, session_id, user_id))

    logger.info(f"Created {len(tools)} spreadsheet analysis tools (assistant={assistant_id})")
    return tools


# ============================================================
# Attachment Partitioning (#206)
# ============================================================

def _estimate_decoded_size(file: "FileContent") -> int:
    """Estimate decoded byte size of a base64-encoded FileContent payload.

    Base64 inflates bytes by ~4/3, so decoded size ≈ len(b64) * 3 / 4.
    This avoids allocating the full bytes just to check a threshold.
    """
    try:
        # Account for base64 padding: strip "=" padding before estimating.
        stripped = (file.bytes or "").rstrip("=")
        return (len(stripped) * 3) // 4
    except Exception:
        return 0


def _partition_attachments(
    all_files: list,
) -> tuple[list, list, list]:
    """Split attachments into (inline_for_bedrock, tabular, oversized_non_tabular).

    - Tabular files (csv/xlsx) are never sent inline — they route through
      the spreadsheet analysis tools. Keeps Bedrock's 4.5MB document limit
      from exploding on XLSX files that expand during internal parsing.
    - Non-tabular files larger than INLINE_DOCUMENT_MAX_BYTES are dropped
      from the inline set with a user-facing note, to prevent mid-stream
      ValidationException on the raw AWS error path.
    - Everything else rides along as a regular document/image content block.
    """
    from apis.shared.files.models import INLINE_DOCUMENT_MAX_BYTES, is_tabular_file

    inline: list = []
    tabular: list = []
    oversized: list = []

    for file in all_files:
        if is_tabular_file(file.filename, file.content_type):
            tabular.append(file)
            continue
        # Only size-gate non-image documents. Images have their own Bedrock
        # limits (much larger) and the prompt builder reroutes them as
        # image blocks, which are not affected by the document-size cap.
        content_type = (file.content_type or "").lower()
        is_image = content_type.startswith("image/")
        if not is_image and _estimate_decoded_size(file) > INLINE_DOCUMENT_MAX_BYTES:
            oversized.append(file)
            continue
        inline.append(file)

    return inline, tabular, oversized


def _build_attachment_guidance(
    diverted_tabular: list,
    oversized_inline: list,
    enabled_tools: list | None,
) -> str:
    """Return a short markdown addendum describing how attachments will be
    handled, to append to the user's message so the agent (and the user)
    both understand why a file isn't inline.
    """
    parts: list[str] = []

    if diverted_tabular:
        names = ", ".join(f"`{f.filename}`" for f in diverted_tabular)
        tool_is_enabled = bool(enabled_tools) and (
            "analyze_spreadsheet" in enabled_tools or "list_spreadsheets" in enabled_tools
        )
        if tool_is_enabled:
            parts.append(
                f"_Attached spreadsheet(s) {names} are available through the "
                f"Spreadsheet Analysis tool rather than inline — use "
                f"`list_spreadsheets` to see them and `analyze_spreadsheet` "
                f"to run aggregations or lookups._"
            )
        else:
            parts.append(
                f"_Attached spreadsheet(s) {names} can't be read inline at "
                f"this size. To analyze them, enable **Spreadsheet Analysis** "
                f"in the Tools section of the settings panel (gear icon next "
                f"to the message input), then re-send your message._"
            )

    if oversized_inline:
        names = ", ".join(f"`{f.filename}`" for f in oversized_inline)
        parts.append(
            f"_Attached file(s) {names} exceed the inline document size limit "
            f"and were skipped. Try a smaller file, or convert to CSV/XLSX "
            f"and use the Spreadsheet Analysis tool._"
        )

    return "\n\n".join(parts)


async def _build_tabular_inventory(
    session_id: str,
    assistant_id: str | None,
    enabled_tools: list | None,
) -> str:
    """Inventory every tabular file visible to the agent this turn, and
    prepend it to the user message when more than one exists.

    Motivation: when the vector search returns chunks from multiple source
    files with identical schemas (e.g. two monthly FY ledgers), the model
    has no way to tell there's more than one spreadsheet at all — RAG
    surfaces chunk content but not a full file inventory. The model picks
    whichever file yielded the first high-ranked chunk and silently runs
    analyze_spreadsheet against just that one. The user's "total" is
    wrong by exactly the other file(s).

    We ship the file list inline so the agent sees the full set at turn
    start and can call list_spreadsheets / pick deliberately / ask the
    user / aggregate across files. Only emitted when the analysis tools
    are enabled (otherwise the agent can't act on it anyway) and when at
    least two tabular files exist (one file isn't ambiguous).
    """
    if not enabled_tools:
        return ""
    tool_is_enabled = (
        "analyze_spreadsheet" in enabled_tools
        or "list_spreadsheets" in enabled_tools
    )
    if not tool_is_enabled:
        return ""

    # Lazy imports to avoid pulling the agent layer into module-load time
    # on cold starts where this code path isn't exercised.
    try:
        from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
            _get_kb_files,
            _get_session_files,
        )
    except Exception:
        return ""

    files: list[dict] = []
    try:
        if assistant_id:
            files.extend(await _get_kb_files(assistant_id))
        files.extend(await _get_session_files(session_id))
    except Exception:
        logger.warning("Failed to enumerate tabular files for inventory", exc_info=True)
        return ""

    # De-duplicate by (filename, source) — a single file shouldn't be
    # listed twice if our lookups overlap.
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for f in files:
        key = (f.get("filename", ""), f.get("source", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    if len(unique) < 2:
        # Single file: no ambiguity, and list_spreadsheets covers discovery
        # for the agent if it ever needs it.
        return ""

    def _fmt_size(n: int) -> str:
        if n >= 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        if n >= 1024:
            return f"{n // 1024} KB"
        return f"{n} B"

    lines = []
    for f in unique:
        name = f.get("filename", "")
        source = "knowledge base" if f.get("source") == "knowledge_base" else "chat attachment"
        size = _fmt_size(int(f.get("size_bytes") or 0))
        lines.append(f"- `{name}` ({source}, {size})")

    listing = "\n".join(lines)
    return (
        f"_Multiple spreadsheet files are attached. Before running "
        f"`analyze_spreadsheet`, decide which file(s) the user's request "
        f"refers to — if it's ambiguous or spans multiple files, call "
        f"`list_spreadsheets` and/or ask the user rather than picking one "
        f"silently. State which file(s) you analyzed in your response._\n\n"
        f"**Available spreadsheets:**\n{listing}"
    )



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
    # Resume requests reuse the cached agent and its paused interrupt state;
    # they bypass quota, file resolution, and RAG augmentation because those
    # already ran on the original turn that got paused.
    is_resume = bool(input_data.interrupt_responses)
    logger.info(
        "Invocation request received (resume=%s)" % is_resume
    )
    logger.info("Message received")

    if input_data.enabled_tools:
        logger.info(f"Enabled tools ({len(input_data.enabled_tools)})")

    if input_data.files:
        logger.info(f"Files attached: {len(input_data.files)} files")
        for file in input_data.files:
            logger.info("  - File attached")

    if input_data.file_upload_ids:
        logger.info(f"File upload IDs: {len(input_data.file_upload_ids)} IDs to resolve")

    # Resolve file upload IDs to FileContent objects, then partition:
    #   - inline_files: images + non-tabular documents that Bedrock can
    #     ingest directly as document content blocks
    #   - tabular_files: csv/xlsx, which we intentionally NEVER send inline
    #     because XLSX in particular inflates dramatically inside Bedrock
    #     (1.4MB zipped → >4.5MB internal, triggering ValidationException).
    #     They remain available to the agent via list_spreadsheets /
    #     analyze_spreadsheet, which run pandas on the real file. See #206.
    #   - oversized_files: non-tabular docs that exceed our inline size
    #     budget; we skip them inline and surface a note instead of
    #     letting Bedrock reject the turn.
    all_files = list(input_data.files) if input_data.files else []

    if input_data.file_upload_ids:
        try:
            file_resolver = get_file_resolver()
            resolved_files = await file_resolver.resolve_files(
                user_id=user_id,
                upload_ids=input_data.file_upload_ids,
                max_files=5,  # Bedrock document limit
            )
            for rf in resolved_files:
                all_files.append(
                    FileContent(filename=rf.filename, content_type=rf.content_type, bytes=rf.bytes)
                )
            logger.info(f"Resolved {len(resolved_files)} files from upload IDs")
        except Exception:
            logger.warning("Failed to resolve file upload IDs", exc_info=True)
            # Continue without files rather than failing the request

    files_to_send, diverted_tabular, oversized_inline = _partition_attachments(all_files)
    if diverted_tabular:
        logger.info(
            f"Diverted {len(diverted_tabular)} tabular file(s) from inline document blocks; "
            f"available via spreadsheet tools: {[f.filename for f in diverted_tabular]}"
        )
    if oversized_inline:
        logger.warning(
            f"Skipped {len(oversized_inline)} oversized file(s) (> inline limit): "
            f"{[(f.filename, _estimate_decoded_size(f)) for f in oversized_inline]}"
        )

    # Pre-create session metadata so OAuth interrupts and other state can
    # attach to the session row from turn one. Best-effort; on failure the
    # post-stream lazy-create in StreamCoordinator still covers it.
    #
    # Also clear any stale paused_turn snapshot at the start of a fresh turn.
    # If the user abandoned a paused turn and started a new one, the prior
    # snapshot is no longer authorized — letting it survive would let a
    # later (mistaken) resume request pick up against a turn the user
    # already moved past.
    is_new_session = False
    if not is_resume:
        is_new_session = await ensure_session_metadata_exists(input_data.session_id, user_id)
        try:
            from apis.shared.sessions.metadata import clear_paused_turn
            await clear_paused_turn(input_data.session_id, user_id)
        except Exception as e:
            logger.error("Failed to clear stale paused_turn on new turn: %s", e, exc_info=True)

    # First turn → kick off title generation concurrently with the stream.
    # Runs as a background task so it doesn't add latency to TTFT. The
    # targeted UpdateExpression in update_session_title is race-safe with
    # the post-stream _update_session_metadata write.
    if is_new_session and input_data.message:
        asyncio.create_task(
            generate_conversation_title(
                session_id=input_data.session_id,
                user_id=user_id,
                user_input=input_data.message,
            )
        )

    # Check quota if enforcement is enabled
    quota_warning_event = None
    quota_exceeded_event = None
    if is_quota_enforcement_enabled() and not is_resume:
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

    if input_data.rag_assistant_id and not is_resume:
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
                    # Update existing metadata: merge assistant_id into the
                    # preferences sub-model. The top-level SessionMetadata has
                    # no assistant_id field, so applying the update there
                    # (previous behavior) silently did nothing under
                    # extra="allow" and left preferences.assistant_id=None.
                    # That broke the mid-session validation above on turn 2+
                    # because the check relies on preferences.assistant_id to
                    # recognize an already-attached assistant (#205).
                    prefs_dict = (
                        existing_metadata.preferences.model_dump(by_alias=False)
                        if existing_metadata.preferences
                        else {}
                    )
                    prefs_dict["assistant_id"] = input_data.rag_assistant_id
                    merged_preferences = SessionPreferences(**prefs_dict)

                    updated_metadata = existing_metadata.model_copy(
                        update={"preferences": merged_preferences}
                    )

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
        # Resume requests rebuild the agent from the persisted PausedTurnSnapshot
        # so a refresh / cache eviction / pod restart between pause and resume
        # still lands on the same MainAgent shape (matching tool registry,
        # model, prompt). Strands' SessionManager separately restores
        # `_interrupt_state` from AgentCore Memory, so the paused tool call
        # picks up where it left off. Non-resume requests use the request
        # body as before.
        if is_resume:
            from datetime import datetime, timezone
            from apis.shared.sessions.metadata import clear_paused_turn, get_paused_turn

            snapshot = await get_paused_turn(input_data.session_id, user_id)
            if not snapshot:
                logger.warning("Resume rejected: no paused_turn snapshot found")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No paused turn for this session; restart the turn.",
                )
            try:
                expires_at = datetime.fromisoformat(snapshot.expires_at)
            except ValueError:
                expires_at = None
            if expires_at and datetime.now(timezone.utc) > expires_at:
                logger.warning("Resume rejected: paused_turn snapshot expired")
                await clear_paused_turn(input_data.session_id, user_id)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Paused turn expired; restart the turn.",
                )

            caching_enabled = snapshot.caching_enabled
            # Snapshot wins on resume so an authorized turn finishes against the
            # exact param shape it was authorized for, even if admin defaults
            # have since changed. Fall back to the legacy fields for snapshots
            # written before inference_params was added.
            resume_inference_params = snapshot.inference_params or {}
            if not resume_inference_params:
                if snapshot.temperature is not None:
                    resume_inference_params["temperature"] = snapshot.temperature
                if snapshot.max_tokens is not None:
                    resume_inference_params["max_tokens"] = snapshot.max_tokens
            agent = await get_agent(
                session_id=input_data.session_id,
                user_id=user_id,
                auth_token=auth_token,
                enabled_tools=snapshot.enabled_tools,
                model_id=snapshot.model_id,
                system_prompt=snapshot.system_prompt,
                caching_enabled=snapshot.caching_enabled,
                provider=snapshot.provider,
                inference_params=resume_inference_params,
                agent_type=snapshot.agent_type,
                is_resume=True,
            )
        else:
            # Build the canonical request inference-params dict. The frontend
            # sends ``inference_params`` directly; legacy ``temperature`` /
            # ``max_tokens`` fields are folded in for older clients and
            # treated as defaults that lose to anything in ``inference_params``.
            request_inference_params: dict = dict(input_data.inference_params or {})
            if input_data.temperature is not None:
                request_inference_params.setdefault("temperature", input_data.temperature)
            if input_data.max_tokens is not None:
                request_inference_params.setdefault("max_tokens", input_data.max_tokens)

            # Resolve the user's persisted default when the request does
            # not pin a model. Without this, a "no default selected" client
            # always lands on the hardcoded factory default and the user's
            # saved preference is silently ignored at chat time (#161).
            effective_model_id = input_data.model_id
            effective_provider = input_data.provider
            if not effective_model_id:
                user_default_id, user_default_provider = await _resolve_user_default_model(user_id)
                if user_default_id:
                    # Re-check model access against the resolved id. The
                    # earlier guard only ran on `input_data.model_id`, so a
                    # stale saved default the user no longer has rights to
                    # would otherwise sneak past RBAC here.
                    app_role_service = get_app_role_service()
                    if await app_role_service.can_access_model(current_user, user_default_id):
                        effective_model_id = user_default_id
                        if not effective_provider and user_default_provider:
                            effective_provider = user_default_provider
                        logger.info("Applied user default model from settings")
                    else:
                        logger.info(
                            "User default model exists but RBAC denies access; falling back to system default"
                        )

            # Single registry lookup resolves caching + inference params,
            # merging admin defaults with request overrides.
            caching_enabled, inference_params = await _resolve_model_settings(
                model_id=effective_model_id,
                explicit_caching_enabled=input_data.caching_enabled,
                request_inference_params=request_inference_params,
            )

            if caching_enabled is False:
                logger.info("Prompt caching disabled for model")

            # Get agent instance with user-specific configuration
            # AgentCore Memory tracks preferences across sessions per user_id
            # Supports multiple LLM providers: AWS Bedrock, OpenAI, and Google Gemini
            # Use augmented message and assistant system prompt if assistant RAG was applied

            # Spreadsheet tools scoped to the assistant's document corpus,
            # when an assistant is attached to this request. The frontend
            # keeps the assistant id in the URL for the whole session's
            # lifetime, so we can trust `input_data.rag_assistant_id`
            # directly; no preferences fallback needed.
            extra_tools = _build_spreadsheet_tools(
                enabled_tools=input_data.enabled_tools,
                assistant_id=input_data.rag_assistant_id,
                session_id=input_data.session_id,
                user_id=user_id,
            )

            agent = await get_agent(
                session_id=input_data.session_id,
                user_id=user_id,
                auth_token=auth_token,
                enabled_tools=input_data.enabled_tools,
                model_id=effective_model_id,
                system_prompt=system_prompt,  # Use assistant's instructions if available
                caching_enabled=caching_enabled,
                provider=effective_provider,
                inference_params=inference_params,
                agent_type=input_data.agent_type,
                extra_tools=extra_tools,
                is_resume=False,
            )

        # Resume requests must target interrupts that the cached agent
        # actually has paused. Cache eviction, a process restart, or a
        # forged request will otherwise be silently accepted by Strands
        # and drop the client's response. Reject up front so the client
        # sees a 400 and can restart the turn cleanly.
        if is_resume:
            strands_agent = getattr(agent, "agent", None)
            interrupt_state = getattr(strands_agent, "_interrupt_state", None) if strands_agent else None
            known_ids: set[str] = set()
            if interrupt_state and getattr(interrupt_state, "activated", False):
                interrupts = getattr(interrupt_state, "interrupts", None) or {}
                known_ids = set(interrupts.keys())
            submitted_ids = [entry.interruptId for entry in (input_data.interrupt_responses or [])]
            unknown_ids = [iid for iid in submitted_ids if iid not in known_ids]
            if unknown_ids:
                logger.warning(
                    "Resume rejected: submitted interrupt ids not in paused state"
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Unknown or expired interrupt ids; restart the turn.",
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
            #   3. Attachment guidance is appended (tabular routed to tools, etc.)
            # The original text becomes the single source of truth for UI display,
            # while the full augmented prompt stays in AgentCore Memory for the LLM.
            attachment_guidance = _build_attachment_guidance(
                diverted_tabular, oversized_inline, input_data.enabled_tools
            )
            # When multiple spreadsheets are visible, ship the full inventory
            # up front so the agent can disambiguate intentionally instead of
            # silently picking whichever file the vector search ranked first.
            tabular_inventory = await _build_tabular_inventory(
                session_id=input_data.session_id,
                assistant_id=input_data.rag_assistant_id,
                enabled_tools=input_data.enabled_tools,
            )
            # Bind to a new local so we don't trip Python's local-scope rules
            # inside this generator closure (augmented_message is defined in
            # the outer function; reassigning it here would make the whole
            # name local and UnboundLocalError before the assignment runs).
            final_message = augmented_message
            if attachment_guidance:
                final_message = f"{final_message}\n\n{attachment_guidance}"
            if tabular_inventory:
                final_message = f"{final_message}\n\n{tabular_inventory}"

            message_will_be_modified = (
                final_message != input_data.message  # RAG augmentation / attachment guidance / inventory
                or bool(files_to_send)               # File attachments
            )
            # Strands' resume protocol wants each entry wrapped as
            # {"interruptResponse": {...}}. The InvocationRequest schema
            # accepts the inner shape so callers don't have to think about
            # the SDK's content-block convention.
            interrupt_responses_payload = (
                [{"interruptResponse": entry.model_dump()} for entry in input_data.interrupt_responses]
                if input_data.interrupt_responses
                else None
            )

            async for event in agent.stream_async(
                final_message,
                session_id=input_data.session_id,
                files=files_to_send if files_to_send else None,
                citations=citations_for_storage if citations_for_storage else None,
                original_message=input_data.message if message_will_be_modified else None,
                interrupt_responses=interrupt_responses_payload,
            ):
                yield event

            # Resume bookkeeping: any interrupt that was submitted in this
            # request and is no longer present in the agent's interrupt state
            # has been resolved — drop the persisted breadcrumb so a refresh
            # doesn't redisplay a stale prompt. Interrupts that re-paused
            # (same provider, new url) are left in place; the next event
            # extractor will refresh them.
            #
            # When the agent's interrupt state is no longer activated after
            # streaming, the turn fully completed — clear ``paused_turn`` too
            # so a stale snapshot doesn't authorize a phantom resume against
            # an already-finished turn. If interrupts re-paused, the snapshot
            # was overwritten by ``_extract_oauth_required_events`` for the
            # next pause, so leave it alone.
            if is_resume and input_data.interrupt_responses:
                try:
                    strands_agent = getattr(agent, "agent", None)
                    interrupt_state = getattr(strands_agent, "_interrupt_state", None) if strands_agent else None
                    still_paused: set[str] = set()
                    state_activated = bool(
                        interrupt_state and getattr(interrupt_state, "activated", False)
                    )
                    if state_activated:
                        still_paused = set((getattr(interrupt_state, "interrupts", None) or {}).keys())
                    resolved_ids = [
                        entry.interruptId
                        for entry in input_data.interrupt_responses
                        if entry.interruptId not in still_paused
                    ]
                    if resolved_ids:
                        from apis.shared.sessions.metadata import remove_pending_interrupts
                        await remove_pending_interrupts(
                            session_id=input_data.session_id,
                            user_id=user_id,
                            interrupt_ids=resolved_ids,
                        )
                    if not state_activated:
                        from apis.shared.sessions.metadata import clear_paused_turn
                        await clear_paused_turn(
                            session_id=input_data.session_id,
                            user_id=user_id,
                        )
                except Exception as cleanup_err:
                    logger.error("Failed to clear resolved pending_interrupts: %s", cleanup_err, exc_info=True)

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
