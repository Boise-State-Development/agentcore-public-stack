"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import AsyncGenerator, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse

from agents.main_agent.core.model_config import KNOWN_CANONICAL_PARAMS
from agents.main_agent.session.session_factory import SessionFactory
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User
from apis.shared.errors import (
    ConversationalErrorEvent,
    ErrorCode,
    build_conversational_error_event,
)
from apis.shared.feature_flags import agents_enabled, skills_enabled
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
from apis.inference_api.chat.agent_binding_resolver import (
    AgentBindingBlockedError,
    resolve_agent_invocation,
)
from apis.shared.sessions.metadata import ensure_session_metadata_exists
from apis.shared.tools.injected import (
    ARTIFACT_TOOL_IDS,
    EXCEL_SPREADSHEET_TOOL_IDS,
    POWERPOINT_PRESENTATION_TOOL_IDS,
    SPREADSHEET_TOOL_IDS,
    WORD_DOCUMENT_TOOL_IDS,
    WORKSPACE_TOOL_IDS,
)
from apis.shared.user_settings.repository import UserSettingsRepository

from .app_context_dispatch import (
    AppContextUpdateError,
    dispatch_app_context_update,
    merge_and_clear_pending_context,
)
from .app_tool_dispatch import AppToolCallError, dispatch_app_tool_call
from .agent_binding_policy import binds_conversation
from .models import FileContent, InvocationRequest
from .service import generate_conversation_title, get_agent
from .system_prompt_resolver import (
    append_active_prompt,
    resolve_active_prompt_text,
    should_resolve_custom_prompt,
)

from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

# Router with no prefix - endpoints will be at root level
router = APIRouter(tags=["agentcore-runtime"])

# ============================================================
# Preview Session Detection
# ============================================================

# Preview session prefix - sessions with this prefix skip persistence
PREVIEW_SESSION_PREFIX = "preview-"

# Default agent factory variant for a user turn when the client doesn't pin one.
# Skills v2: plain chat is the default; skills are opt-in (selected per-turn or
# bound on an Agent). A client can still pin agent_type explicitly (e.g. an
# Agent that binds skills resolves to "skill" via the agent-binding resolver).
DEFAULT_AGENT_TYPE = "chat"


def _mark_session_cancelled(agent) -> None:
    """Flip the agent's session-manager ``cancelled`` flag (cooperative stop).

    Both StopHook (tool boundaries) and the stream coordinator (mid-generation)
    read this flag to unwind the turn. Defensive: a nonstandard agent without a
    session manager is simply a no-op.
    """
    session_manager = getattr(agent, "session_manager", None)
    if session_manager is not None:
        session_manager.cancelled = True
        logger.info("Cooperative stop: cancel observed for the running turn")


async def _lease_heartbeat_loop(lease, agent) -> None:
    """Renew the single-flight session lease and observe cancel requests.

    Runs as a background task for the life of the SSE stream. Renewing on a wall
    clock (rather than piggybacking on SSE-event cadence) keeps the lease alive
    across a long silent tool call — code-interpreter / browser can run past the
    lease window between yielded events — and bounds Stop→resend latency to one
    interval. Each renew also reports whether a cancel has been armed for this
    lease owner; on the first such observation we flip the agent's ``cancelled``
    flag and stop renewing (the turn is unwinding). Best-effort and owner-scoped;
    cancelled in the stream generator's ``finally``.
    """
    from apis.shared.sessions.session_lease import (
        LEASE_HEARTBEAT_SECONDS,
        renew_session_lease,
    )

    while True:
        await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
        cancel_requested = await renew_session_lease(lease)
        if cancel_requested:
            _mark_session_cancelled(agent)
            return


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


def _as_int_or_none(value: object) -> int | None:
    """Coerce a numeric inference-param value to int for safety comparisons.

    Inference params arrive untyped (``Dict[str, Any]`` from JSON), so an
    integer bound can show up as a float (e.g. ``8192.0``). Returns ``None``
    for bool / non-numeric values (including a ``thinking`` value an admin
    pasted as a raw SDK dict) so callers skip the check rather than crash.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


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

        # Enum params (e.g. `effort`): the override must be a member of the
        # admin-declared `allowed` set; an out-of-domain value falls back to
        # the default rather than erroring mid-stream. Mirrors the numeric
        # clamp below, and the per-model `allowed` differences (Sonnet 4.6
        # vs Opus 4.7) stay data, not code.
        if spec.allowed is not None:
            req = request_params.get(name)
            if req is not None and req in spec.allowed:
                merged[name] = req
            elif spec.default is not None:
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
    # Coerce before comparing: both values can arrive as floats (untyped
    # Dict[str, Any] from JSON), and an `isinstance(..., int)` gate would
    # silently skip the check on float input and let the bad request through.
    thinking = _as_int_or_none(merged.get("thinking"))
    max_tokens = _as_int_or_none(merged.get("max_tokens"))
    if thinking is not None and max_tokens is not None and thinking >= max_tokens:
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
) -> tuple[bool | None, dict, str | None, str | None, str | None]:
    """Resolve runtime model knobs from the managed-model registry.

    Returns ``(caching_enabled, inference_params, mantle_api_mode,
    mantle_region, provider)``. A single registry lookup drives all of them.
    The Mantle fields are server-authoritative (recorded on the model):
    ``mantle_api_mode`` selects Chat Completions vs the Responses API and
    ``mantle_region`` optionally pins inference to a specific region; both
    ``None`` for non-Mantle models. ``provider`` is the model's registered
    provider (e.g. ``"mantle"``), returned so callers can recover it when the
    request/binding didn't carry one — without it a Mantle model like
    ``openai.gpt-5.4`` misroutes to Bedrock ConverseStream and fails with an
    invalid-model-identifier error. Resolving these here keeps them off the
    client request — the SPA can't override.
    """
    request_params = dict(request_inference_params or {})

    if not model_id:
        return explicit_caching_enabled, request_params, None, None, None

    managed_model = await _find_managed_model(model_id)

    if explicit_caching_enabled is not None:
        caching = explicit_caching_enabled
    elif managed_model is not None:
        caching = managed_model.supports_caching
    else:
        caching = None

    mantle_api_mode = (
        getattr(managed_model, "mantle_api_mode", None)
        if managed_model is not None
        else None
    )
    mantle_region = (
        getattr(managed_model, "mantle_region", None)
        if managed_model is not None
        else None
    )
    provider = (
        getattr(managed_model, "provider", None)
        if managed_model is not None
        else None
    )

    inference_params = _merge_inference_params(managed_model, request_params)
    return caching, inference_params, mantle_api_mode, mantle_region, provider


async def _resolve_caching_enabled(model_id: str | None, explicit_caching_enabled: bool | None) -> bool | None:
    """Backward-compat wrapper around :func:`_resolve_model_settings`."""
    caching, _, _, _, _ = await _resolve_model_settings(model_id, explicit_caching_enabled, None)
    return caching


# ============================================================
# Spreadsheet Analysis Tool Injection
# ============================================================

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

    logger.info(f"Created {len(tools)} spreadsheet analysis tools (assistant={scrub_log(assistant_id)})")
    return tools


# ============================================================
# Artifact Authoring Tool Injection
# ============================================================

def _build_artifact_tools(
    enabled_tools: list | None,
    session_id: str,
    user_id: str,
) -> list:
    """Create context-bound artifact authoring tools if enabled by the user."""
    if not enabled_tools or not ARTIFACT_TOOL_IDS.intersection(enabled_tools):
        return []

    # Artifacts are a single toggle: enabling create_artifact provisions the
    # full authoring toolset (create + update) so the model can iterate on a
    # document without a second admin catalog entry. The legacy
    # "update_artifact" catalog row is retired — see
    # backend/scripts/backfill_artifact_tool_merge.py.
    from agents.builtin_tools.artifacts import (
        make_create_artifact_tool,
        make_update_artifact_tool,
    )

    tools = [
        make_create_artifact_tool(session_id, user_id),
        make_update_artifact_tool(session_id, user_id),
    ]

    logger.info(f"Created {len(tools)} artifact authoring tools")
    return tools


# ============================================================
# Word Document Tool Injection
# ============================================================

def _build_word_document_tools(
    enabled_tools: list | None,
    session_id: str,
    user_id: str,
) -> list:
    """Create context-bound Word document tools if enabled by the user.

    Identity is captured by closure (same pattern as the artifact and
    spreadsheet tools) since the runtime does not populate ToolContext.
    """
    if not enabled_tools or not WORD_DOCUMENT_TOOL_IDS.intersection(enabled_tools):
        return []

    # The Word capability is a single toggle: enabling create_word_document
    # provisions the full document toolset (create/modify/list/read) so the
    # model can round-trip on a document without extra admin catalog entries.
    from agents.builtin_tools.word_document_tool import (
        make_create_word_document_tool,
        make_list_word_documents_tool,
        make_modify_word_document_tool,
        make_read_word_document_tool,
    )

    tools = [
        make_create_word_document_tool(session_id, user_id),
        make_modify_word_document_tool(session_id, user_id),
        make_list_word_documents_tool(session_id, user_id),
        make_read_word_document_tool(session_id, user_id),
    ]

    logger.info(f"Created {len(tools)} word document tools")
    return tools


# ============================================================
# Workspace Tool Injection
# ============================================================

def _build_workspace_tools(
    enabled_tools: list | None,
    session_id: str,
    user_id: str,
) -> list:
    """Create context-bound workspace file tools if enabled by the user.

    Identity is captured by closure (same pattern as the artifact and word
    document tools). The "workspace_files" catalog entry is a single toggle
    that provisions the full toolset (list/read/write).
    """
    from apis.shared.feature_flags import workspace_tools_enabled

    if not workspace_tools_enabled():
        return []
    if not enabled_tools or not WORKSPACE_TOOL_IDS.intersection(enabled_tools):
        return []

    from agents.builtin_tools.workspace_tools import (
        make_workspace_list_tool,
        make_workspace_read_tool,
        make_workspace_write_tool,
    )

    tools = [
        make_workspace_list_tool(session_id, user_id),
        make_workspace_read_tool(session_id, user_id),
        make_workspace_write_tool(session_id, user_id),
    ]

    logger.info(f"Created {len(tools)} workspace tools")
    return tools


# ============================================================
# Excel Spreadsheet Tool Injection
# ============================================================

def _build_excel_spreadsheet_tools(
    enabled_tools: list | None,
    session_id: str,
    user_id: str,
) -> list:
    """Create context-bound Excel spreadsheet tools if enabled by the user.

    Identity is captured by closure (same pattern as the Word document and
    spreadsheet analysis tools) since the runtime does not populate ToolContext.
    Distinct from the spreadsheet *analysis* tools (list_spreadsheets /
    analyze_spreadsheet): this toolset creates/modifies/reads/lists generated
    .xlsx files, it doesn't analyze uploaded ones.
    """
    if not enabled_tools or not EXCEL_SPREADSHEET_TOOL_IDS.intersection(enabled_tools):
        return []

    # The Excel capability is a single toggle: enabling create_excel_spreadsheet
    # provisions the full workbook toolset (create/modify/list/read) so the
    # model can round-trip on a spreadsheet without extra admin catalog entries.
    from agents.builtin_tools.excel_spreadsheet_tool import (
        make_create_excel_spreadsheet_tool,
        make_list_excel_spreadsheets_tool,
        make_modify_excel_spreadsheet_tool,
        make_read_excel_spreadsheet_tool,
    )

    tools = [
        make_create_excel_spreadsheet_tool(session_id, user_id),
        make_modify_excel_spreadsheet_tool(session_id, user_id),
        make_list_excel_spreadsheets_tool(session_id, user_id),
        make_read_excel_spreadsheet_tool(session_id, user_id),
    ]

    logger.info(f"Created {len(tools)} excel spreadsheet tools")
    return tools


# ============================================================
# PowerPoint Presentation Tool Injection
# ============================================================

def _build_powerpoint_presentation_tools(
    enabled_tools: list | None,
    session_id: str,
    user_id: str,
) -> list:
    """Create context-bound PowerPoint presentation tools if enabled by the user.

    Identity is captured by closure (same pattern as the Word document and Excel
    spreadsheet tools) since the runtime does not populate ToolContext.
    """
    if not enabled_tools or not POWERPOINT_PRESENTATION_TOOL_IDS.intersection(enabled_tools):
        return []

    # The PowerPoint capability is a single toggle: enabling
    # create_powerpoint_presentation provisions the full deck toolset
    # (create/modify/list/read) so the model can round-trip on a presentation
    # without extra admin catalog entries.
    from agents.builtin_tools.powerpoint_presentation_tool import (
        make_create_powerpoint_presentation_tool,
        make_list_powerpoint_layouts_tool,
        make_list_powerpoint_presentations_tool,
        make_modify_powerpoint_presentation_tool,
        make_read_powerpoint_presentation_tool,
    )

    tools = [
        make_create_powerpoint_presentation_tool(session_id, user_id),
        make_modify_powerpoint_presentation_tool(session_id, user_id),
        make_list_powerpoint_presentations_tool(session_id, user_id),
        make_read_powerpoint_presentation_tool(session_id, user_id),
        make_list_powerpoint_layouts_tool(session_id, user_id),
    ]

    logger.info(f"Created {len(tools)} powerpoint presentation tools")
    return tools


def _build_memory_tools(agent_memory, user_id: str, user_email: str) -> list:
    """Context-bound Memory-Space tools for an Agent's resolved memory binding.

    ``agent_memory`` is the resolver's ``ResolvedMemoryBinding`` (or ``None``). No binding
    → no tools. Read tools (list + read) are always exposed; the write tool only when the
    binding grants ``readwrite`` — and the service re-checks ``editor+`` on every call, so
    this is a UX gate, not the security boundary. Not gated on ``enabled_tools``: the
    governing capability is the Agent's binding, not the user's tool picker.
    """
    if agent_memory is None:
        return []

    from agents.builtin_tools.memory_spaces import (
        make_memory_list_tool,
        make_memory_read_tool,
        make_memory_write_tool,
    )

    space_id, space_name = agent_memory.space_id, agent_memory.space_name
    tools = [
        make_memory_list_tool(space_id, space_name, user_id, user_email),
        make_memory_read_tool(space_id, space_name, user_id, user_email),
    ]
    if agent_memory.access == "readwrite":
        tools.append(make_memory_write_tool(space_id, space_name, user_id, user_email))

    logger.info(f"Created {len(tools)} memory-space tools for bound space")
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


def _build_interruption_note(reason: str) -> str:
    """Reason-driven note prepended to the next turn's prompt when the prior
    turn was interrupted (see `clear_interrupted_turn`, whose popped reason
    feeds this).

    Why the note lives HERE and not on the interrupted turn itself: the
    reason is not knowable at cancellation time — the client's `user_stopped`
    signal (app-api) races the server-side cancellation backstop
    (inference-api), and precedence only settles in the session record. By
    the next turn the marker is authoritative.

    The two reasons carry opposite signal, so the guidance differs:
    `user_stopped` is deliberate feedback (don't barrel onward);
    `connection_lost` (or unclassified) is a technical drop (the user likely
    still wants the answer). The note is prepended to the persisted user
    message (the `original_message`/displayText split keeps it out of the
    UI), so it remains an honest in-history record that ages out via
    compaction rather than a permanent synthetic system turn.
    """
    if reason == "user_stopped":
        guidance = (
            "The user deliberately stopped your previous response before it "
            "finished (the last assistant message above is the partial that "
            "was delivered). Treat that as meaningful feedback — do not "
            "resume or repeat it on your own; let the user's message below "
            "set the direction."
        )
    else:  # connection_lost / unknown — technical drop, no user intent
        guidance = (
            "Your previous response was cut off by a connection interruption "
            "— the user did not stop it deliberately (the last assistant "
            "message above is the partial that was delivered). If the user "
            "asks you to continue, pick up where it left off instead of "
            "starting over."
        )
    return f"<interruption_note>\n{guidance}\n</interruption_note>"


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

    # Persist user + assistant turns. Unlike the streaming error paths in
    # stream_coordinator (which persist assistant-only because the agent
    # loop's MessageAddedEvent hook already wrote the user turn), this
    # path fires BEFORE any agent run — quota-exceeded short-circuits,
    # etc. — so no hook has persisted the user turn yet.
    try:
        from agents.main_agent.session.persistence import persist_synthetic_messages

        session_manager = SessionFactory.create_session_manager(session_id=session_id, user_id=user_id, caching_enabled=False)
        persist_synthetic_messages(
            session_manager,
            session_id,
            [("user", user_input), ("assistant", message)],
        )

    except Exception:
        logger.error("Failed to save messages to session", exc_info=True)


# ============================================================
# AgentCore Runtime Standard Endpoints (REQUIRED)
# ============================================================


@router.get("/ping")
async def ping():
    """Health check endpoint (required by AgentCore Runtime).

    AgentCore's idle reaper requires ``time_of_last_update`` (int epoch
    seconds) alongside ``status``. When the field is absent the platform
    reaps the microVM at ``idleRuntimeSessionTimeout`` even mid-stream,
    regardless of the reported status (bedrock-agentcore-sdk-python#471).

    We do not run the SDK's async-task busy tracking here (that's the
    deferred ``async_mode`` work), so we cannot report ``HealthyBusy``.
    Returning a fresh timestamp on every ping keeps the session alive
    while the runtime data plane is polling us, which is the documented
    mitigation for the silent mid-generation reap.
    """
    return {
        "status": "Healthy",
        "time_of_last_update": int(time.time()),
        "version": os.environ.get("APP_VERSION", "unknown"),
    }


async def _resolve_accessible_skill_ids(current_user: User) -> list[str]:
    """Resolve every skill a user can reach: RBAC-granted catalog ∪ own.

    Thin delegate to the shared resolver (``apis.shared.skills.access``) used
    by both this path and the user-facing skills API, so the picker and the
    runtime can never drift. Kept as a module-level seam for tests. Never
    raises — on any failure the user simply gets no skills and the turn runs
    without the disclosure plugin.
    """
    from apis.shared.skills.access import resolve_accessible_skill_ids

    return await resolve_accessible_skill_ids(current_user)


def _apply_enabled_skills_filter(
    accessible_skill_ids: list[str], enabled_skills: Optional[list[str]]
) -> list[str]:
    """Narrow the accessible skill set by the client's per-turn selection.

    Intersection only: client input can narrow the set, never grant.

    ``None`` (or an empty list) means **no skills** — Skills v2 D6 flips plain
    chat to opt-in, unlike tools. This is the reverse of v1, where absent meant
    "every accessible skill". Opt-in is what keeps prompt bloat and instruction
    conflicts bounded as the catalog grows across two authorship tiers; a client
    that predates the picker now simply gets a plain chat turn, which is the
    safe direction to fail.
    """
    if not enabled_skills:
        return []
    requested = set(enabled_skills)
    return [sid for sid in accessible_skill_ids if sid in requested]


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
    # Resolve the effective agent type: the client's explicit choice, else the
    # compiled-in default ("chat"). Used for the skill resolution below and the
    # non-resume get_agent calls (resume reuses the snapshot's type). An Agent
    # that binds skills is coerced to "skill" later by the agent-binding
    # resolver.
    effective_agent_type = input_data.agent_type or DEFAULT_AGENT_TYPE
    # Skills feature deferred for this environment: neutralize the legacy
    # "skill" agent type, which is a ChatAgent alias since v2 PR-2. Voice and
    # other agent types pass through untouched.
    if not skills_enabled() and effective_agent_type == "skill":
        effective_agent_type = "chat"
    # Resolve the user's *effective* skills once for the whole request: the
    # accessible set (catalog ∪ own), narrowed by the client's per-turn
    # enabled_skills selection. Threaded into every get_agent call below so they
    # share one skills_hash cache key (otherwise the app-tool-call / resume paths
    # would miss the main turn's cached agent).
    #
    # Skills v2: this is no longer gated on agent_type == "skill". Skills are a
    # plain-chat capability now — the picker in model settings sends
    # enabled_skills on an ordinary turn and ChatAgent mounts the AgentSkills
    # plugin. The opt-in default (D6) is what keeps this cheap: an absent or
    # empty selection short-circuits to [] without touching RBAC or the skill
    # table, so every turn that doesn't ask for skills costs exactly what it did
    # before. An Agent's skill bindings override this further down.
    effective_skill_ids = None
    if skills_enabled() and input_data.enabled_skills:
        effective_skill_ids = _apply_enabled_skills_filter(
            await _resolve_accessible_skill_ids(current_user),
            input_data.enabled_skills,
        )
    # A "Continue" after a max_tokens truncation. Like resume, it bypasses
    # quota / RAG / file resolution and does NOT clear the turn state; unlike
    # resume there is no interrupt to validate — the agent is rebuilt from the
    # resent params and re-entered with an empty prompt (assistant-prefill).
    is_continuation = bool(input_data.continue_truncated)
    # Marketplace D11: the Agent was `@`-mentioned in the composer, so it runs
    # this turn only — it does not bind the conversation. Only meaningful
    # alongside `rag_assistant_id`; on its own it does nothing.
    is_agent_mention = bool(input_data.agent_mention) and bool(input_data.rag_assistant_id)
    logger.info(
        "Invocation request received (resume=%s, continue_truncated=%s, agent_mention=%s)"
        % (is_resume, is_continuation, is_agent_mention)
    )
    logger.info("Message received")

    # App-initiated tools/call (MCP Apps PR #5). Like resume/continuation it
    # bypasses quota / RAG / file resolution / title — there is no model
    # turn. We rebuild the conversation agent (so the MCP client session +
    # auth are wired exactly as for a model-driven call), dispatch the one
    # named tool, publish synthesized tool_use/tool_result into the thread
    # via the per-session broker, and return the CallToolResult as JSON for
    # app-api to relay back to the iframe. Inert behind the host flag (the
    # UIToolCatalog is empty, so dispatch rejects every call as not
    # app-visible).
    if input_data.app_tool_call is not None:
        atc = input_data.app_tool_call
        try:
            request_inference_params = dict(input_data.inference_params or {})
            caching_enabled, inference_params, mantle_api_mode, mantle_region, registry_provider = await _resolve_model_settings(
                model_id=input_data.model_id,
                explicit_caching_enabled=input_data.caching_enabled,
                request_inference_params=request_inference_params,
            )
            agent = await get_agent(
                session_id=input_data.session_id,
                user_id=user_id,
                auth_token=auth_token,
                enabled_tools=input_data.enabled_tools,
                model_id=input_data.model_id,
                system_prompt=input_data.system_prompt,
                caching_enabled=caching_enabled,
                provider=input_data.provider or registry_provider,
                inference_params=inference_params,
                mantle_api_mode=mantle_api_mode,
                mantle_region=mantle_region,
                agent_type=effective_agent_type,
                is_resume=False,
                accessible_skill_ids=effective_skill_ids,
            )
            payload = await dispatch_app_tool_call(
                agent,
                session_id=input_data.session_id,
                user_id=user_id,
                tool_use_id=atc.tool_use_id,
                tool_name=atc.tool_name,
                arguments=atc.arguments,
            )
            return JSONResponse(payload)
        except AppToolCallError as e:
            return JSONResponse({"error": e.message}, status_code=e.code)
        except HTTPException:
            raise
        except Exception:
            logger.error("app tools/call invocation failed", exc_info=True)
            return JSONResponse({"error": "Internal error"}, status_code=500)

    # App-pushed model context (MCP Apps PR #6, `ui/update-model-context`).
    # Like app_tool_call it bypasses quota / RAG / file resolution / title
    # and runs NO model turn — we rebuild the conversation agent (so the
    # same cached `agent.state` is reused) and stash the payload under
    # `mcp_apps.context[resource_uri]`. The next real user turn merges and
    # clears it. Inert behind the host flag (no live App ever calls this).
    if input_data.app_context_update is not None:
        acu = input_data.app_context_update
        try:
            request_inference_params = dict(input_data.inference_params or {})
            caching_enabled, inference_params, mantle_api_mode, mantle_region, registry_provider = await _resolve_model_settings(
                model_id=input_data.model_id,
                explicit_caching_enabled=input_data.caching_enabled,
                request_inference_params=request_inference_params,
            )
            agent = await get_agent(
                session_id=input_data.session_id,
                user_id=user_id,
                auth_token=auth_token,
                enabled_tools=input_data.enabled_tools,
                model_id=input_data.model_id,
                system_prompt=input_data.system_prompt,
                caching_enabled=caching_enabled,
                provider=input_data.provider or registry_provider,
                inference_params=inference_params,
                mantle_api_mode=mantle_api_mode,
                mantle_region=mantle_region,
                agent_type=effective_agent_type,
                is_resume=False,
                accessible_skill_ids=effective_skill_ids,
            )
            payload = dispatch_app_context_update(
                agent,
                resource_uri=acu.resource_uri,
                content=acu.content,
                structured_content=acu.structured_content,
            )
            return JSONResponse(payload)
        except AppContextUpdateError as e:
            return JSONResponse({"error": e.message}, status_code=e.code)
        except HTTPException:
            raise
        except Exception:
            logger.error("app context update invocation failed", exc_info=True)
            return JSONResponse({"error": "Internal error"}, status_code=500)

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

    # Deduplicate files by (filename, content_type) before partitioning.
    # The same file can arrive via both `files` (direct base64) and
    # `file_upload_ids` (resolved from S3), or a client may submit the same
    # upload ID twice. Sending two document blocks with the same sanitized
    # name to Bedrock ConverseStream raises:
    #   ValidationException: Messages can't contain duplicate document names.
    # We keep the first occurrence and drop subsequent duplicates.
    if all_files:
        seen_file_keys: set = set()
        deduped_files = []
        for f in all_files:
            key = (f.filename.lower(), f.content_type.lower())
            if key not in seen_file_keys:
                seen_file_keys.add(key)
                deduped_files.append(f)
            else:
                logger.info(
                    "Dropping duplicate file attachment: %s (%s)",
                    f.filename,
                    f.content_type,
                )
        if len(deduped_files) < len(all_files):
            logger.info(
                "Deduplicated %d -> %d file(s) before sending to Bedrock",
                len(all_files),
                len(deduped_files),
            )
        all_files = deduped_files

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
    if not is_resume and not is_continuation:
        is_new_session = await ensure_session_metadata_exists(input_data.session_id, user_id)
        try:
            from apis.shared.sessions.metadata import clear_paused_turn
            await clear_paused_turn(input_data.session_id, user_id)
        except Exception as e:
            logger.error("Failed to clear stale paused_turn on new turn: %s", e, exc_info=True)

    # Invalidate any prior max_tokens "Continue" marker on every new model
    # turn that isn't an interrupt-resume — both a fresh turn and a
    # continuation supersede it. If a continuation itself re-truncates, the
    # stream_coordinator intercept re-sets the marker.
    interrupted_turn_reason: Optional[str] = None
    if not is_resume:
        try:
            from apis.shared.sessions.metadata import clear_truncated_turn
            await clear_truncated_turn(input_data.session_id, user_id)
        except Exception as e:
            logger.error("Failed to clear stale truncated_turn on new turn: %s", e, exc_info=True)

        # Same lifecycle for the interrupted-turn marker: any new non-resume
        # turn supersedes a prior interruption, so a stale marker can't
        # resurrect the "response interrupted" state against a turn the user
        # has moved past. The pop returns the settled reason (user_stopped
        # beats connection_lost via write precedence) so this same read+write
        # also drives the one-turn interruption note prepended to the prompt
        # in the stream generator below.
        try:
            from apis.shared.sessions.metadata import clear_interrupted_turn
            interrupted_turn_reason = await clear_interrupted_turn(input_data.session_id, user_id)
        except Exception as e:
            logger.error("Failed to clear stale interrupted_turn on new turn: %s", e, exc_info=True)

    # First turn → kick off title generation concurrently with the stream.
    # Runs as a background task so it doesn't add latency to TTFT. The
    # targeted UpdateExpression in update_session_title is race-safe with
    # the post-stream _update_session_metadata write. The task handle is
    # kept so stream_with_quota_warning can push the finished title to the
    # client mid-stream as a `session_title` SSE event.
    title_task: Optional["asyncio.Task[str]"] = None
    if is_new_session and input_data.message:
        title_task = asyncio.create_task(
            generate_conversation_title(
                session_id=input_data.session_id,
                user_id=user_id,
                user_input=input_data.message,
            )
        )

    # Check quota if enforcement is enabled
    quota_warning_event = None
    quota_exceeded_event = None
    if is_quota_enforcement_enabled() and not is_resume and not is_continuation:
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
    # Agent Designer Phase 3: governed capabilities resolved per invoking user
    # (D5). None ⇒ resolve exactly as today. Set in the assistant block below,
    # consumed at model resolution / prompt assembly; None on resume/continuation.
    agent_model_override = None
    agent_memory = None
    # Agent Designer: an Agent's ``tool`` bindings, resolved per invoker (D5), replace
    # the request's ``enabled_tools`` for the turn (like ``model_override`` replaces the
    # model). None ⇒ the Agent binds no tools ⇒ the request's enabled_tools drive the turn.
    agent_tools_override = None
    # An Agent's ``skill`` bindings, resolved per invoker (D5). When set they replace the
    # request's skills AND force skill-mode (agent_type="skill") for the turn. None ⇒ the
    # Agent binds no skills ⇒ the request's agent_type/enabled_skills drive the turn.
    agent_skills_override = None
    # Version snapshots (§4): which Agent snapshot this turn resolved to, for the log line
    # below. ``None`` means the live record ran — a plain chat turn with no Agent, an Agent
    # with nothing published, or the owner running their own draft.
    #
    # ⚠️ Deliberately **not** in the agent cache key, despite what the spec's §4.2 says. The
    # key is built from construction *values*, and everything a version changes about
    # behavior already reaches it: instructions via ``system_prompt``, tool bindings via
    # ``enabled_tools``, skills via ``skills_hash``/``agent_type``, the model via
    # ``model_id``, and a memory binding by skipping the cache entirely (extra_tools). So
    # promoting a version already misses. Adding the number would buy no discrimination and
    # would cost real safety: the resume path rebuilds its key from ``PausedTurnSnapshot``,
    # so a new key element the snapshot did not carry orphans the paused agent and breaks
    # OAuth-consent / tool-approval resumes (``service.py`` warns about exactly this).
    resolved_version = None

    logger.info(
        "Invocation request - processing with assistant context"
    )

    if input_data.rag_assistant_id and not is_resume and not is_continuation:
        # Local imports to avoid circular dependency
        from apis.shared.assistants.rag_service import (
            augment_prompt_with_context,
            search_assistant_knowledgebase_with_formatting,
        )
        from apis.shared.assistants.service import (
            get_assistant_with_access_check,
            mark_share_as_interacted,
        )
        from apis.shared.assistants.version_resolution import (
            AgentVersionUnavailableError,
            resolve_invocation_agent,
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
        #
        # Marketplace D11: an `@`-mention turn skips BOTH rules on purpose. It
        # borrows the Agent for one turn without binding the conversation, so
        # "you already have a different Agent" and "this thread already has
        # messages" are the normal case rather than the error case. The Agent's
        # own access check below is untouched — skipping this block relaxes
        # *binding* semantics, never authorization. Rule in
        # ``agent_binding_policy`` so it is testable without this stack.
        if binds_conversation(
            is_agent_mention=is_agent_mention,
            is_preview=is_preview_session(input_data.session_id),
        ):
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
            logger.info(
                "Turn does not bind the conversation (mention=%s) - skipping session state validation"
                % is_agent_mention
            )

        # 2. Load assistant with access check
        logger.info("Loading assistant with access check...")
        assistant, _ = await get_assistant_with_access_check(
            assistant_id=input_data.rag_assistant_id, user_id=user_id, user_email=current_user.email
        )

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

        # 2a. Version snapshots (§4) — decide WHICH configuration this caller runs.
        #
        # This is the seam the whole epic was built toward: everything below resolves
        # against ``assistant``, so swapping in the published snapshot here changes what
        # runs without touching binding resolution, the system prompt, or the harness.
        #
        # Everyone but the owner runs the reviewed snapshot; the owner runs their own draft
        # so they can iterate before resubmitting. An Agent with nothing published (never
        # submitted, private, or in review) returns unchanged — that is the common case and
        # it behaves exactly as it did before this feature.
        #
        # ⚠️ Ordered *before* the access check's side effects below on purpose: it is not an
        # access decision and must not be read as one. The caller was already admitted.
        try:
            assistant, resolved_version = await resolve_invocation_agent(assistant, user_id)
        except AgentVersionUnavailableError as unavailable:
            # A published Agent whose snapshot is missing fails the turn rather than
            # falling back to the draft — the fallback would serve unreviewed instructions
            # to a pinned user at exactly the moment something is already broken.
            logger.error(f"Published version unavailable: {unavailable}")
            raise HTTPException(
                status_code=503,
                detail=(
                    "This agent's published version could not be loaded. Please try again, "
                    "or contact an administrator if it persists."
                ),
            ) from unavailable

        # Which configuration actually ran. Worth a line: "this agent behaved oddly" is not
        # answerable without knowing whether the turn ran an approved snapshot or a draft.
        logger.info(
            "Agent configuration for this turn: %s",
            f"published version {resolved_version}" if resolved_version else "live record / draft",
        )

        # Mark as viewed if this is a shared assistant (not owned)
        if assistant.owner_id != user_id:
            await mark_share_as_interacted(assistant_id=input_data.rag_assistant_id, user_email=current_user.email)

        # KB sync inactivity signal: any user's chat use counts. Throttled
        # to one write/day inside bump_last_used_at (conditional update);
        # the winning bump also wakes any inactivity-paused sync policies.
        # Best-effort — a bookkeeping failure must never break a chat turn.
        try:
            from apis.shared.assistants.service import bump_last_used_at
            from apis.shared.sync_policies.service import resume_inactive_policies

            if await bump_last_used_at(input_data.rag_assistant_id):
                await resume_inactive_policies(input_data.rag_assistant_id)
        except Exception as bump_err:
            logger.warning(f"lastUsedAt bump failed for assistant {input_data.rag_assistant_id}: {bump_err}")

        # 2b. Agent Designer Phase 3 — resolve the Agent's governed capabilities
        # for the INVOKING user (D5), before the expensive KB search. v1 blocks
        # with a conversational message when the invoker lacks a required model.
        if agents_enabled():
            try:
                agent_plan = await resolve_agent_invocation(assistant, current_user)
                agent_model_override = agent_plan.model_override
                agent_memory = agent_plan.memory
                agent_tools_override = agent_plan.tools
                agent_skills_override = agent_plan.skills
            except AgentBindingBlockedError as block:
                blocked_event = ConversationalErrorEvent(
                    code=ErrorCode.FORBIDDEN, message=block.message, recoverable=False
                )
                return StreamingResponse(
                    stream_conversational_message(
                        message=block.message,
                        stop_reason="error",
                        metadata_event=blocked_event,
                        session_id=input_data.session_id,
                        user_id=user_id,
                        user_input=input_data.message,
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": input_data.session_id},
                )

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

        # 5b. Agent Designer Phase 3: inject the bound Memory Space content (read-only)
        # after instructions, in either branch. Hydration re-reads via the invoker
        # (MemorySpaceService re-checks viewer+ internally). Empty for a fresh space.
        if agent_memory is not None:
            from apis.shared.memory.hydration import render_memory_block, resolve_always_load
            from apis.shared.memory.service import MemorySpaceService

            try:
                fragments = await asyncio.to_thread(
                    resolve_always_load,
                    MemorySpaceService(),
                    agent_memory.space_id,
                    user_id,
                    current_user.email,
                    agent_memory.always_load,
                )
                memory_block = render_memory_block(agent_memory.space_name, fragments)
                if memory_block:
                    system_prompt = f"{system_prompt}\n\n{memory_block}" if system_prompt else memory_block
                    logger.info("Injected bound Memory Space content into system prompt")
            except Exception:
                # Never fail a turn on a memory-read hiccup — the permission was already
                # resolved; injection is best-effort context.
                logger.error("Failed to hydrate bound Memory Space; continuing", exc_info=True)

        # 6. Save assistant_id to session preferences (persist for future loads)
        # Skip persistence for preview sessions
        #
        # Marketplace D11: a mention turn deliberately writes nothing. Persisting
        # here would silently convert the whole conversation to the Agent — the
        # SPA self-heals its `assistantId` query param from these preferences on
        # reload — so one `@` would bind the thread forever, which is the exact
        # behavior the per-turn design rejects. Same predicate as the validation
        # above, deliberately: validating without persisting would refuse the
        # second mention in a thread, and persisting without validating would let
        # a mention annex the conversation.
        if binds_conversation(
            is_agent_mention=is_agent_mention,
            is_preview=is_preview_session(input_data.session_id),
        ):
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
            logger.info(
                "Turn does not bind the conversation (mention=%s) - skipping assistant_id persistence"
                % is_agent_mention
            )

    # Append active custom system prompt (if any). Gating rules + lookup live
    # in `system_prompt_resolver.py` so they can be unit-tested independently
    # of the route.
    if should_resolve_custom_prompt(
        is_resume=is_resume,
        is_continuation=is_continuation,
        is_preview=is_preview_session(input_data.session_id),
        has_assistant=bool(input_data.rag_assistant_id),
    ):
        resolved = await resolve_active_prompt_text(
            session_id=input_data.session_id,
            user_id=user_id,
            request_prompt_id=input_data.selected_prompt_id,
        )
        if resolved:
            prompt_name, prompt_text = resolved
            # Build the base system prompt if not already built (no-assistant
            # path can leave system_prompt unset).
            if not system_prompt:
                from agents.main_agent.core.system_prompt_builder import SystemPromptBuilder
                system_prompt = SystemPromptBuilder().build(include_date=True)
            system_prompt = append_active_prompt(system_prompt, prompt_name, prompt_text)
            logger.info(f"Appended custom system prompt: {prompt_name!r}")

    # Per-session single-flight guard (docs/specs/session-single-flight-guard.md,
    # follow-up to PR #653). A client abort doesn't propagate through the
    # AgentCore Runtime data plane and the Runtime can route a duplicate
    # invocation to a *different* container, so two agent loops could otherwise
    # run concurrently against one AgentCore Memory session and corrupt
    # tool-pairing history. Acquire a distributed lease at turn-start; reject a
    # duplicate with 409. Resume / max-tokens continuation re-enter a loop that
    # already ended, so they take the lease with force=True (never blocked, but
    # still install it so a fresh duplicate during them is rejected). Preview
    # sessions and the local no-DynamoDB path (lease None) skip the guard.
    session_lease = None
    if not is_preview_session(input_data.session_id):
        from apis.shared.sessions.session_lease import (
            acquire_session_lease,
            SessionBusyError,
        )

        try:
            session_lease = await acquire_session_lease(
                input_data.session_id,
                user_id,
                force=is_resume or is_continuation,
            )
        except SessionBusyError:
            logger.warning(
                "Rejected duplicate concurrent invocation for session %s (409)",
                scrub_log(input_data.session_id),
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A response is already streaming for this conversation. "
                    "Wait for it to finish before sending another message."
                ),
            )

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
                mantle_api_mode=snapshot.mantle_api_mode,
                mantle_region=snapshot.mantle_region,
                agent_type=snapshot.agent_type,
                is_resume=True,
                # Resume must rebuild the SAME cache key the original turn used,
                # or the paused agent is orphaned. New snapshots carry the
                # original turn's exact effective set in enabled_skills, so
                # replay it verbatim — including [] (a turn that deliberately
                # carried no skills → empty skills_hash).
                #
                # Skills v2: this must NOT be gated on snapshot.agent_type ==
                # "skill" any more. A plain-chat turn now carries skills too, so
                # that gate would resolve a skills-bearing "chat" snapshot back
                # to None and orphan its paused agent. The snapshot's own field
                # is the authority.
                #
                # Legacy snapshots (written before enabled_skills existed) still
                # fall back to this request's resolution, and only for a "skill"
                # turn — that is exactly what those turns were built with.
                accessible_skill_ids=(
                    snapshot.enabled_skills
                    if snapshot.enabled_skills is not None
                    else (effective_skill_ids if snapshot.agent_type == "skill" else None)
                ),
            )
            # The `stream_with_quota_warning` closure below references
            # `effective_enabled_tools` unconditionally (attachment guidance,
            # tabular inventory). It is only assigned in the non-resume branch,
            # so a resume turn — e.g. the client re-invoking after granting an
            # OAuth-gated MCP tool's consent, or answering a tool-approval
            # interrupt — would otherwise raise `NameError: cannot access free
            # variable 'effective_enabled_tools'`, surfacing as a 500 from the
            # container and a 424 Failed Dependency to the caller. Bind it to the
            # snapshot's toolset, the same source the resume `get_agent` used.
            effective_enabled_tools = snapshot.enabled_tools
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
            if agent_model_override is not None:
                # The Agent's governed modelConfig wins over the request / user-default
                # chain. Already access-checked against the invoker in the resolver (R2),
                # so the earlier request-only gate at the top doesn't leave a hole.
                effective_model_id = agent_model_override.model_id
                effective_provider = agent_model_override.provider or effective_provider
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

            # Agent-authored params sit as defaults BENEATH explicit request params,
            # then flow through _resolve_model_settings' admin bounds/locks like any
            # other request params — an author can't smuggle out-of-bounds values.
            if agent_model_override is not None and agent_model_override.params:
                request_inference_params = {**agent_model_override.params, **request_inference_params}

            # Single registry lookup resolves caching + inference params +
            # the Mantle endpoint path + provider, merging admin defaults with
            # request overrides.
            caching_enabled, inference_params, mantle_api_mode, mantle_region, registry_provider = await _resolve_model_settings(
                model_id=effective_model_id,
                explicit_caching_enabled=input_data.caching_enabled,
                request_inference_params=request_inference_params,
            )

            # Recover the provider from the registry when neither the request nor
            # the Agent's model binding carried one. Agent bindings persist only
            # ``model_id`` (no provider), so without this a Mantle model like
            # ``openai.gpt-5.4`` resolves to provider=None → Bedrock and blows up
            # in ConverseStream with "invalid model identifier" — even though the
            # same model works from the normal chat path, which always sends
            # ``provider`` alongside ``model_id``.
            if not effective_provider and registry_provider:
                effective_provider = registry_provider

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
            # An Agent's tool bindings replace the request's enabled_tools for this
            # turn (D5, resolved per invoker above). None ⇒ no tool binding ⇒ the
            # request drives the toolset exactly as today. Drives both the built-in
            # extra tools (spreadsheet/artifact gate on specific ids) and get_agent.
            effective_enabled_tools = (
                agent_tools_override.tool_ids
                if agent_tools_override is not None
                else input_data.enabled_tools
            )

            # An Agent's skill bindings replace the request's skills for this turn so
            # ChatAgent's AgentSkills plugin discloses exactly the bound set (D5,
            # resolved per invoker above). Reassigning these function-scope locals here
            # (before the main-turn get_agent below) makes them flow into construction —
            # and thus the paused-turn snapshot — so a bound-skill agent resumes on the
            # same skills_hash. None ⇒ no skill binding ⇒ the request drives skills/type.
            if agent_skills_override is not None:
                effective_agent_type = "skill"
                effective_skill_ids = agent_skills_override.skill_ids

            extra_tools = _build_spreadsheet_tools(
                enabled_tools=effective_enabled_tools,
                assistant_id=input_data.rag_assistant_id,
                session_id=input_data.session_id,
                user_id=user_id,
            ) + _build_artifact_tools(
                enabled_tools=effective_enabled_tools,
                session_id=input_data.session_id,
                user_id=user_id,
            ) + _build_word_document_tools(
                enabled_tools=effective_enabled_tools,
                session_id=input_data.session_id,
                user_id=user_id,
            ) + _build_workspace_tools(
                enabled_tools=effective_enabled_tools,
                session_id=input_data.session_id,
                user_id=user_id,
            ) + _build_excel_spreadsheet_tools(
                enabled_tools=effective_enabled_tools,
                session_id=input_data.session_id,
                user_id=user_id,
            ) + _build_powerpoint_presentation_tools(
                enabled_tools=effective_enabled_tools,
                session_id=input_data.session_id,
                user_id=user_id,
            ) + _build_memory_tools(
                agent_memory=agent_memory,
                user_id=user_id,
                user_email=current_user.email,
            )

            agent = await get_agent(
                session_id=input_data.session_id,
                user_id=user_id,
                auth_token=auth_token,
                enabled_tools=effective_enabled_tools,
                model_id=effective_model_id,
                system_prompt=system_prompt,  # Use assistant's instructions if available
                caching_enabled=caching_enabled,
                provider=effective_provider,
                inference_params=inference_params,
                mantle_api_mode=mantle_api_mode,
                mantle_region=mantle_region,
                agent_type=effective_agent_type,
                extra_tools=extra_tools,
                is_resume=False,
                accessible_skill_ids=effective_skill_ids,
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
            # One-shot `session_title` SSE: once the concurrent title task
            # (kicked off before the quota check on first turns) finishes,
            # push the title to the client so the sidebar/header rename in
            # parallel with the pending response instead of at stream end.
            # Checked between agent events — never awaited, so it adds no
            # latency; a stream that outruns Nova Micro simply never emits
            # and the SPA's post-close metadata refresh covers it.
            title_emitted = False

            def _session_title_sse() -> Optional[str]:
                nonlocal title_emitted
                if title_emitted or title_task is None or not title_task.done():
                    return None
                title_emitted = True
                try:
                    generated_title = title_task.result()
                except Exception as title_err:  # noqa: BLE001 - cancelled/failed task must not break the stream
                    logger.warning("Title task unavailable for SSE emit: %s", title_err)
                    return None
                # Generation failures return the "New Conversation"
                # placeholder — nothing worth pushing over the wire.
                if not generated_title or generated_title == "New Conversation":
                    return None
                payload = {
                    "type": "session_title",
                    "sessionId": input_data.session_id,
                    "title": generated_title,
                }
                return f"event: session_title\ndata: {json.dumps(payload)}\n\n"

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
                diverted_tabular, oversized_inline, effective_enabled_tools
            )
            # When multiple spreadsheets are visible, ship the full inventory
            # up front so the agent can disambiguate intentionally instead of
            # silently picking whichever file the vector search ranked first.
            tabular_inventory = await _build_tabular_inventory(
                session_id=input_data.session_id,
                assistant_id=input_data.rag_assistant_id,
                enabled_tools=effective_enabled_tools,
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

            # MCP Apps PR #6: drain any context an embedded App pushed via
            # `ui/update-model-context` since the last turn and prepend it
            # to this turn only. Skipped on resume/continuation (Strands
            # ignores `final_message` there) so a pending update survives
            # until the next real user turn instead of being silently
            # cleared. Kept out of persisted history via the
            # `original_message` path below (cache-prefix-safe).
            if not is_resume and not is_continuation:
                pending_ctx_block = merge_and_clear_pending_context(agent)
                if pending_ctx_block:
                    final_message = f"{pending_ctx_block}\n\n{final_message}"

                # Interrupted-turn context: the prior turn ended early (Stop /
                # refresh / dropped connection) and its marker was popped at
                # turn start — tell the model, with reason-appropriate
                # guidance, before it reads the new message. Prepended last so
                # the note sits topmost. Rides the same `original_message`
                # displayText split as the ctx block, so the user never sees
                # it while it stays an honest part of persisted history.
                # Continuation turns skip this (Strands ignores the message
                # there — the model just continues the persisted partial).
                if interrupted_turn_reason:
                    final_message = (
                        f"{_build_interruption_note(interrupted_turn_reason)}\n\n{final_message}"
                    )

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
                continue_truncated=is_continuation,
                # Which Agent ran this turn (#756). Recorded on the cost row so a
                # deliberate `@`-mention prefix swap is distinguishable from the
                # nondeterministic-ordering regression the fingerprints exist to catch.
                # Passed per turn rather than read off the agent: the agent instance is
                # cached and shared across turns, so per-turn state must never live on it
                # (see #741/#751).
                turn_agent_id=input_data.rag_assistant_id,
            ):
                yield event
                # Interleave the finished title between agent events (same
                # non-blocking drain pattern as the MCP Apps broker in the
                # stream coordinator). SSE events are self-delimited, so
                # injecting between events is always frame-safe.
                title_sse = _session_title_sse()
                if title_sse:
                    yield title_sse

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

        # Wrap the agent stream so the single-flight session lease is heartbeat-
        # renewed while the turn runs and released when the stream ends. FastAPI
        # runs this generator *after* the handler returns, so the lease can't be
        # released in the handler body without ending it prematurely — the
        # generator's finally is the release site for the happy path (the two
        # except handlers below cover pre-stream failures).
        async def _guarded_stream() -> AsyncGenerator[str, None]:
            heartbeat_task = (
                asyncio.create_task(_lease_heartbeat_loop(session_lease, agent))
                if session_lease is not None
                else None
            )
            try:
                async for chunk in stream_with_quota_warning():
                    yield chunk
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    # Await the cancelled task so its CancelledError is retrieved
                    # (never re-raised) before the lease is released.
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                from apis.shared.sessions.session_lease import release_session_lease
                await release_session_lease(session_lease)

        # Stream response from agent as SSE (with optional files)
        # Note: Compression is handled by GZipMiddleware if configured in main.py
        return StreamingResponse(
            _guarded_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": input_data.session_id},
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is (e.g., from auth). Release the lease
        # first — a failure raised after acquire (e.g. resume/interrupt 400s)
        # means the turn won't stream, so its generator finally never runs.
        from apis.shared.sessions.session_lease import release_session_lease
        await release_session_lease(session_lease)
        raise
    except Exception as e:
        # Stream error as a conversational assistant message for better UX.
        # The agent turn won't run, so release the lease here (the error stream
        # is a canned single message, not an agent loop).
        logger.error("Error in invocations", exc_info=True)
        from apis.shared.sessions.session_lease import release_session_lease
        await release_session_lease(session_lease)

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
