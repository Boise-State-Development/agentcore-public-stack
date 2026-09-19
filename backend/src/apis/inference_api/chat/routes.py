"""AgentCore Runtime standard endpoints

Implements AgentCore Runtime required endpoints:
- POST /invocations (required)
- GET /ping (required)

These endpoints are at the root level to comply with AWS Bedrock AgentCore Runtime requirements.
"""

import asyncio
import json
import logging
from collections import OrderedDict
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
from apis.inference_api.runtime_health import ping_payload
from apis.shared.feature_flags import (
    agent_preparing_phase_enabled,
    agents_enabled,
    attachment_turn_guard_enabled,
    mid_turn_steering_enabled,
    skills_enabled,
)
from apis.shared.files.file_resolver import get_file_resolver
from apis.shared.files.models import (
    INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
    MAX_FILES_PER_MESSAGE,
)
from apis.shared.models.managed_models import list_managed_models
from apis.shared.quota import (
    QuotaExceededEvent,
    build_no_quota_configured_event,
    build_quota_exceeded_event,
    build_quota_session_notice_event,
    build_quota_warning_event,
    get_quota_checker,
    is_quota_enforcement_enabled,
)

from apis.shared.rbac.service import get_app_role_service
from apis.shared.skills.bundle import slugify_skill_name
from apis.inference_api.chat.agent_binding_resolver import (
    AgentBindingBlockedError,
    resolve_agent_invocation,
)
from apis.shared.sessions.metadata import (
    ensure_session_metadata_exists,
    load_session_meta,
)
from apis.shared.tools.injected import (
    ARTIFACT_TOOL_IDS,
    EXCEL_SPREADSHEET_TOOL_IDS,
    POWERPOINT_PRESENTATION_TOOL_IDS,
    SPREADSHEET_TOOL_IDS,
    WORD_DOCUMENT_TOOL_IDS,
    WORKSPACE_TOOL_IDS,
    injected_tools_are_key_described,
)
from apis.shared.user_settings.repository import UserSettingsRepository

from .app_context_dispatch import (
    AppContextUpdateError,
    dispatch_app_context_update,
    merge_and_clear_pending_context,
)
from apis.shared.mcp_apps.error_envelope import app_tool_error_response

from .app_tool_dispatch import AppToolCallError, dispatch_app_tool_call
from .agent_binding_policy import binds_conversation
from .models import FileContent, InvocationRequest
from .service import generate_conversation_title, get_agent
from .turn_timing import TurnPrelude
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
    """Arm cancellation for the running turn (cooperative stop).

    Two signals, because they stop different things:

    - ``session_manager.cancelled`` is read by StopHook (tool boundaries) and
      by the stream coordinator (mid-generation). It ends the turn, but only
      at a point where our own code regains control.
    - ``agent.cancel()`` is Strands' own signal. As of strands-agents 1.51.0 it
      propagates into an **in-flight MCP tool call** and makes the sequential
      tool executor skip the tools still queued behind it. That is the gap the
      flag alone cannot close: StopHook fires only *before* a tool starts, so a
      Stop pressed during a slow MCP call previously ran that call to
      completion — the shape behind the MCP Apps proxy-call timeouts.

    Both are cleared at the head of the next turn by
    ``reset_cancellation_state``; neither may leak onto the cached agent.

    Defensive throughout: a nonstandard agent missing either surface is a
    no-op, and cancelling is idempotent.
    """
    session_manager = getattr(agent, "session_manager", None)
    if session_manager is not None:
        session_manager.cancelled = True

    cancel = getattr(agent, "cancel", None)
    if callable(cancel):
        cancel()

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


async def _release_turn_lease(heartbeat_task, lease) -> None:
    """End the turn's lease heartbeat and release the lease. Cancellation-proof.

    This runs from a stream generator's ``finally``, and the case that matters
    is the one where cancellation is what *put us here*: on a client
    disconnect the whole request task is being torn down, so a bare ``await``
    in that ``finally`` is itself cancelled at the first suspension point and
    the release never lands. The lease then survives for the rest of its 90s
    window, and the user's resend is rejected as a duplicate turn — a 409 that
    the AgentCore Runtime rewrites to 424 and the SPA shows as "Chat Request
    Failed", seconds after the dropped stream already showed them a network
    error.

    Same hazard and same remedy as ``_persist_interruption`` in the stream
    coordinator: do the work on an independent task and shield the wait on it,
    so the release completes even when our wait for it does not.
    """
    async def _do() -> None:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            # Await the cancelled task so its CancelledError is retrieved
            # (never re-raised) before the lease is released.
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        from apis.shared.sessions.session_lease import release_session_lease

        await release_session_lease(lease)

    try:
        await asyncio.shield(asyncio.ensure_future(_do()))
    except asyncio.CancelledError:
        # The shield was cancelled from outside while _do() ran; the inner
        # task finishes on its own. Swallowing here cannot suppress whatever
        # unwound the stream — that exception is still in flight and resumes
        # propagating once this `finally` returns.
        logger.debug("lease-release shield cancelled; inner release continues")


async def _session_has_messages(*, session_id: str, user_id: str) -> bool:
    """True when the session already carries at least one persisted message.

    Both binding rules turn on this: an Agent may only be attached to a thread that
    has no history, because history produced under other instructions, tools and
    skills is exactly what a binding would misrepresent.

    Deliberately `limit=1` — the question is existence, not count, and this runs on
    the invocation path.
    """
    from apis.shared.sessions.messages import get_messages

    response = await get_messages(session_id=session_id, user_id=user_id, limit=1)
    return bool(response.messages)


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

    **Omission means unsupported, for any model that declares a spec at all.**
    A param the spec doesn't mention is dropped rather than passed through.
    This inverts the original default, which forwarded any request key that
    appeared in ``KNOWN_CANONICAL_PARAMS``. That was a latent turn-killer:
    Anthropic deprecated ``temperature``/``top_p``/``top_k`` on Claude Opus 4.7
    and later, where a non-default value returns a hard 400. Our curated
    templates for those models correctly *omit* the params — but omitting is
    not the same as declaring ``supported: false``, so a request that carried a
    temperature reached Bedrock and killed the turn mid-stream. Inverting the
    default closes the whole class instead of requiring every future template
    to enumerate each newly-deprecated param and never miss one.

    Models with **no** spec keep the permissive behavior: an admin who
    hand-created a record without a ``supportedParams`` block hasn't declared
    anything, so there is nothing to read an omission against. Every drop is
    logged, which is what makes the inversion observable if it takes away a
    param someone was relying on.
    """
    merged: dict = {}
    spec_map = {}
    if managed_model and managed_model.supported_params:
        spec_map = managed_model.supported_params.params or {}

    # A non-empty spec is the signal that the admin described this model's
    # params deliberately. An empty/absent one is silence, not a claim.
    spec_is_authoritative = bool(spec_map)

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

    # Request keys the spec doesn't mention. For a model that declared a spec,
    # silence means unsupported and the key is dropped. For one that declared
    # nothing, fall back to the canonical allow-list: without that gate a user
    # could submit a future canonical key (or one a future provider mapping
    # starts forwarding) and bypass the admin's per-model bounds entirely.
    # The provider translation table remains the second line of defense.
    for name, value in request_params.items():
        if name in seen_keys or value is None:
            continue
        if spec_is_authoritative:
            # Logged at INFO on purpose: this is the inversion taking something
            # away. If a param a caller depended on starts disappearing, this
            # line is how it gets found — grep `omitted from its supportedParams`.
            logger.info(
                "Dropping inference param '%s' for model %s — omitted from its "
                "supportedParams spec, which declares %d param(s)",
                _sanitize_log(name),
                _sanitize_log(getattr(managed_model, "model_id", "?")),
                len(spec_map),
            )
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
    The API-surface fields are server-authoritative (recorded on the model):
    ``mantle_api_mode`` selects Chat Completions vs the Responses API and
    ``mantle_region`` optionally pins inference to a specific region. Both are
    meaningful on either OpenAI-compatible Bedrock surface — ``"mantle"`` and
    ``"bedrock-responses"`` — and ``None`` for every other provider.
    ``provider`` is the model's registered provider, returned so callers can
    recover it when the request/binding didn't carry one — without it a Mantle
    model like ``openai.gpt-5.4`` misroutes to Bedrock ConverseStream and fails
    with an invalid-model-identifier error. Resolving these here keeps them off
    the client request — the SPA can't override.
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
# Document Read Tool Injection (docs/specs/document-context-offload.md §4B)
# ============================================================

#: Sessions known to carry a readable document. The gate is one DynamoDB
#: query per turn otherwise; a positive answer is memoized because it is
#: monotonic in practice (an upload stays unless the user deletes it, and a
#: stale tool on a session whose files were deleted just returns "not found").
#: Negative answers are never memoized — the next turn may be the upload.
_DOCUMENT_SESSIONS: "OrderedDict[str, bool]" = OrderedDict()
_DOCUMENT_SESSIONS_MAX = 10_000


def _remember_document_session(session_id: str) -> None:
    _DOCUMENT_SESSIONS[session_id] = True
    _DOCUMENT_SESSIONS.move_to_end(session_id)
    while len(_DOCUMENT_SESSIONS) > _DOCUMENT_SESSIONS_MAX:
        _DOCUMENT_SESSIONS.popitem(last=False)


async def _session_has_documents(
    session_id: str,
    user_id: str,
    turn_upload_ids: list | None = None,
) -> bool:
    """Whether ``document_read`` should exist on this turn.

    True when this turn attaches uploads (their metadata rows already exist,
    so no query is needed), when the session was seen carrying a document
    earlier in this process, or when the session's upload rows include at
    least one readable document (PDF, Word, text, markdown, HTML — not
    spreadsheets, decks or images, which have other paths). Fail-closed on
    error: a turn without the tool is today's behavior, never a broken turn.
    """
    if turn_upload_ids:
        _remember_document_session(session_id)
        return True
    if _DOCUMENT_SESSIONS.get(session_id):
        return True
    try:
        from apis.shared.files.document_read import session_has_documents

        present = await session_has_documents(user_id, session_id)
    except Exception:  # noqa: BLE001 - the gate must never fail a turn
        logger.warning("document_read gate lookup failed; tool not injected this turn", exc_info=True)
        return False
    if present:
        _remember_document_session(session_id)
    return present


async def _document_tools_gate(
    session_id: str,
    user_id: str,
    turn_upload_ids: list | None = None,
) -> bool:
    """The single answer to "does this turn carry ``document_read``" — the
    builder and the resume path's cache key both read it, so the two can
    never disagree (a disagreement orphans a paused agent)."""
    from apis.shared.feature_flags import document_read_enabled

    if not document_read_enabled():
        return False
    if not session_id or not user_id:
        return False
    return await _session_has_documents(session_id, user_id, turn_upload_ids)


async def _build_document_tools(
    session_id: str,
    user_id: str,
    turn_upload_ids: list | None = None,
) -> list:
    """Context-bound ``document_read`` for a session that has a readable attachment.

    **Not gated on ``enabled_tools``** — the governing capability is the user's
    own attachment, exactly as the Memory-Space tools are governed by an
    Agent's binding. Its id stays out of ``INJECTED_TOOL_IDS``. Kill switch:
    ``DOCUMENT_READ_ENABLED=false``. The gate's answer also feeds the agent
    cache key (``has_document_tools``), so an agent cached before the first
    upload is never served without the tool afterwards.
    """
    if not await _document_tools_gate(session_id, user_id, turn_upload_ids):
        return []

    from agents.builtin_tools.document_read_tool import make_document_read_tool

    tools = [make_document_read_tool(session_id, user_id)]
    logger.info("Created document_read tool (session has a readable document)")
    return tools


# ============================================================
# Attachment Partitioning (#206)
# ============================================================

# ============================================================
# Spreadsheet Analysis auto-enable on attachment
# (docs/specs/load-test-assessment-2026-09.md P2-E)
# ============================================================

#: Sessions known to hold a spreadsheet attachment. Same contract as
#: ``_DOCUMENT_SESSIONS``: one query per turn otherwise, positive answers
#: memoized because an upload stays unless the user deletes it, negative
#: answers never memoized because the next turn may be the upload.
_TABULAR_SESSIONS: "OrderedDict[str, bool]" = OrderedDict()
_TABULAR_SESSIONS_MAX = 10_000


def _remember_tabular_session(session_id: str) -> None:
    _TABULAR_SESSIONS[session_id] = True
    _TABULAR_SESSIONS.move_to_end(session_id)
    while len(_TABULAR_SESSIONS) > _TABULAR_SESSIONS_MAX:
        _TABULAR_SESSIONS.popitem(last=False)


async def _session_has_tabular(
    session_id: str,
    user_id: str,
    turn_has_tabular: bool = False,
) -> bool:
    """Whether this session's turns need the Spreadsheet Analysis tools.

    True when this turn attaches a CSV/XLSX (already partitioned, no query),
    when the session was seen holding one earlier in this process, or when
    the session's READY uploads include one. Sticky by design: the answer
    feeds ``enabled_tools`` and therefore the agent-cache key, so it must
    not flip between the attach turn and the follow-up. Fail-closed on
    error: a turn without the tools is today's behavior, never a broken turn.
    """
    if turn_has_tabular:
        _remember_tabular_session(session_id)
        return True
    if _TABULAR_SESSIONS.get(session_id):
        return True
    if not session_id or not user_id:
        return False
    try:
        from apis.shared.files.document_read import session_has_tabular_files

        present = await session_has_tabular_files(user_id, session_id)
    except Exception:  # noqa: BLE001 - the gate must never fail a turn
        logger.warning("spreadsheet auto-enable lookup failed; tools not injected this turn", exc_info=True)
        return False
    if present:
        _remember_tabular_session(session_id)
    return present


def _with_auto_enabled_tools(enabled_tools: list | None, auto_ids: list[str]) -> list | None:
    """``enabled_tools`` plus ``auto_ids`` not already present, appended in the
    order given. Returns the same object when there is nothing to add, so a
    caller that passed ``None`` still passes ``None`` and every consumer of the
    list (cache key, builders, guidance, ToolFilter) sees one value."""
    if not auto_ids:
        return enabled_tools
    current = list(enabled_tools or [])
    missing = [tool_id for tool_id in auto_ids if tool_id not in current]
    if not missing:
        return enabled_tools
    return current + missing


async def _auto_enabled_attachment_tool_ids(
    current_user: User,
    session_id: str,
    user_id: str,
    turn_has_tabular: bool = False,
) -> list[str]:
    """The Spreadsheet Analysis ids this turn should carry regardless of the
    picker, in a fixed order: every id in ``SPREADSHEET_TOOL_IDS`` the
    caller's RBAC grant admits, when the session holds a spreadsheet.

    Enables, never grants: ``can_access_tool`` is the same predicate the
    picker and Agent bindings answer to (role grant ∪ public tools). A user
    whose roles do not carry the tool gets today's behavior — the attachment
    note tells them the tool is not available to their account.
    """
    from apis.shared.feature_flags import attachment_tool_autoenable_enabled

    if not attachment_tool_autoenable_enabled():
        return []
    if not await _session_has_tabular(session_id, user_id, turn_has_tabular):
        return []
    role_service = get_app_role_service()
    allowed: list[str] = []
    for tool_id in sorted(SPREADSHEET_TOOL_IDS):
        try:
            if await role_service.can_access_tool(current_user, tool_id):
                allowed.append(tool_id)
        except Exception:  # noqa: BLE001 - an RBAC lookup failure must not fail the turn
            logger.warning("RBAC check for %s failed; not auto-enabling", tool_id, exc_info=True)
    if allowed:
        logger.info(
            "Auto-enabled %s for a session with a spreadsheet attachment (session=%s)",
            allowed, scrub_log(session_id),
        )
    return allowed


async def _apply_attachment_tool_autoenable(
    enabled_tools: list | None,
    current_user: User,
    session_id: str,
    user_id: str,
    turn_has_tabular: bool = False,
) -> list | None:
    """``enabled_tools`` for this turn with the attachment auto-enable applied.

    The single seam every ``get_agent`` caller on the invocation path goes
    through, so the main turn and the MCP App dispatch paths compute the same
    effective list — and therefore the same agent-cache slot — for a session
    holding a spreadsheet.
    """
    auto_ids = await _auto_enabled_attachment_tool_ids(
        current_user, session_id, user_id, turn_has_tabular=turn_has_tabular
    )
    return _with_auto_enabled_tools(enabled_tools, auto_ids)


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
) -> tuple[list, list, list, list]:
    """Split attachments into
    (inline_for_bedrock, tabular, presentations, oversized_non_tabular).

    - Tabular files (csv/xlsx) are never sent inline — they route through
      the spreadsheet analysis tools. Keeps Bedrock's 4.5MB document limit
      from exploding on XLSX files that expand during internal parsing.
    - Presentations (pptx) are never sent inline either, but for a harder
      reason: Bedrock's document-block format enum has no `pptx`, so an
      inline deck is a guaranteed ValidationException. They route through
      the PowerPoint tools (see `is_presentation_file`).
    - Non-tabular files larger than INLINE_DOCUMENT_MAX_BYTES are dropped
      from the inline set with a user-facing note, to prevent mid-stream
      ValidationException on the raw AWS error path.
    - Everything else rides along as a regular document/image content block.

    Both diverted classes are checked before the size gate: they never go
    inline at any size, so an oversized note would misdescribe them.
    """
    from apis.shared.files.models import (
        INLINE_DOCUMENT_MAX_BYTES,
        is_presentation_file,
        is_tabular_file,
    )

    inline: list = []
    tabular: list = []
    presentations: list = []
    oversized: list = []

    for file in all_files:
        if is_tabular_file(file.filename, file.content_type):
            tabular.append(file)
            continue
        if is_presentation_file(file.filename, file.content_type):
            presentations.append(file)
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

    return inline, tabular, presentations, oversized


def _apply_message_file_cap(
    direct_files: list,
    upload_ids: list,
    max_files: int,
) -> tuple[list, list, list, int]:
    """Hold a message to ``max_files`` attachments across both request paths.

    Returns ``(direct_files, upload_ids, dropped_names, dropped_total)``.
    Direct ``files`` come first (they are already in the request body), then
    ``file_upload_ids`` fill whatever budget remains. Attachment order is
    kept, so the first N the user attached are the N that survive.

    The cap is applied to the upload IDs *before* they are resolved: the old
    resolver default truncated silently after the fact, and letting every ID
    through just to name the losers would fan out one S3 read per ID a client
    chose to send. IDs beyond the budget are therefore counted, not named —
    ``dropped_names`` holds the direct files (names known) and
    ``dropped_total`` counts both. ``max_files <= 0`` disables the cap.
    """
    if max_files <= 0:
        return direct_files, upload_ids, [], 0

    kept_direct = direct_files[:max_files]
    dropped_names = [f.filename for f in direct_files[max_files:]]
    id_budget = max(0, max_files - len(kept_direct))
    kept_ids = upload_ids[:id_budget]
    dropped_total = len(dropped_names) + (len(upload_ids) - len(kept_ids))
    return kept_direct, kept_ids, dropped_names, dropped_total


def _apply_inline_byte_budget(
    inline: list,
    max_total_bytes: int,
) -> tuple[list, list, int]:
    """Hold the inline set (documents *and* images) to one message's byte
    budget. Returns ``(kept, over_budget, requested_bytes)``.

    Why this exists: the turn's inline attachments are persisted as one
    AgentCore Memory event, and past ~7.5 MB of raw bytes that write fails
    with ``SessionException`` — a hole in history, not a degraded turn. See
    ``INLINE_ATTACHMENTS_MAX_TOTAL_BYTES`` for the derivation.

    Policy — first-fit in attachment order: walk the files as the user
    attached them, keep each one that still fits, and move any that would
    push the running total over the budget to ``over_budget``. Earlier
    attachments win, and a later, smaller file that still fits rides along
    rather than being punished for a large neighbour. Order within both
    lists is the attachment order, so the marker text and the guidance note
    are deterministic (they land in the cacheable prefix on later turns).

    Images count toward the budget: they are part of the same message and
    the same event, even though the per-file document gate skips them.
    ``max_total_bytes <= 0`` disables the budget.
    """
    requested = sum(_estimate_decoded_size(f) for f in inline)
    if max_total_bytes <= 0:
        return list(inline), [], requested

    kept: list = []
    over: list = []
    running = 0
    for file in inline:
        size = _estimate_decoded_size(file)
        if running + size > max_total_bytes:
            over.append(file)
            continue
        running += size
        kept.append(file)
    return kept, over, requested


def _emit_attachment_over_quota_metric(
    requested_bytes: int,
    cap_bytes: int,
    inline_count: int,
    dropped_count: int,
) -> None:
    """One content-free EMF record in ``AgentCoreStack/Compaction`` when a
    turn's inline attachments had to be trimmed to the byte budget. Never
    raises. ``AttachmentTurnOverQuota`` carries the requested bytes so the
    rate *and* the size distribution of over-quota turns are measurable
    (spec §4E put the rate at ~1.3–1.4% of attachment turns from a proxy;
    this is the direct count).
    """
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={"AttachmentTurnOverQuota": requested_bytes},
            properties={
                "capBytes": cap_bytes,
                "inlineFileCount": inline_count,
                "droppedFileCount": dropped_count,
            },
            units={"AttachmentTurnOverQuota": "Bytes"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("AttachmentTurnOverQuota EMF skipped: %s", e)


def _attachment_marker_names(all_files: list, oversized_inline: list) -> list:
    """Filenames for the ``[Attached files: …]`` marker on the user message.

    The SPA replays that marker on session load to rebuild attachment cards
    (``restoreFileAttachments``): the ``fileAttachment`` content block it
    renders from is built client-side at send time and is never persisted, so
    the marker is the only surviving link between a file and the message it
    was attached to.

    Deliberately NOT ``files_to_send``. Diverted spreadsheets and decks are
    still in the session and still reachable through their tools, so their
    cards have to survive a reload too — deriving this from the inline set
    alone is exactly what made an uploaded .pptx vanish from history while
    the file itself remained perfectly present.

    Oversized files are excluded: those were dropped from the turn entirely
    and the guidance text already explains their absence.

    Order follows ``all_files`` (how the user attached them) rather than a
    concatenation of the partition buckets, so the text is deterministic —
    it lands in the cacheable prefix on every later turn.
    """
    oversized_names = {f.filename for f in oversized_inline}
    return [f.filename for f in all_files if f.filename not in oversized_names]


def _build_attachment_guidance(
    diverted_tabular: list,
    diverted_presentations: list,
    oversized_inline: list,
    enabled_tools: list | None,
    over_budget: list | None = None,
    dropped_over_count_names: list[str] | None = None,
    dropped_over_count_total: int = 0,
    max_files: int = 0,
) -> str:
    """Return a short markdown addendum describing how attachments will be
    handled, to append to the user's message so the agent (and the user)
    both understand why a file isn't inline.

    ``oversized_inline`` is the per-file case (the file itself is too big;
    the fix is a smaller file). ``over_budget`` is the aggregate case (each
    file is fine, together they exceed one message's budget; the fix is a
    follow-up message). They get separate sentences because the remedy
    differs. ``dropped_over_count_*`` describe files beyond the per-message
    count cap: names where known (direct ``files``), a count otherwise.
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
            # Reached only when the auto-enable did not apply: the caller's
            # roles do not carry the tool, or the kill switch is set. Say so
            # without sending them to a toggle that may not be there.
            parts.append(
                f"_Attached spreadsheet(s) {names} can't be read inline. "
                f"Analyzing them needs the **Spreadsheet Analysis** tool, which "
                f"isn't available in this conversation — if it is listed under "
                f"Customize → Tools in the sidebar, enable it and re-send your "
                f"message; if it isn't, your account doesn't have access to it._"
            )

    if diverted_presentations:
        names = ", ".join(f"`{f.filename}`" for f in diverted_presentations)
        tool_is_enabled = bool(enabled_tools) and bool(
            POWERPOINT_PRESENTATION_TOOL_IDS.intersection(enabled_tools)
        )
        if tool_is_enabled:
            parts.append(
                f"_Attached presentation(s) {names} are available through the "
                f"PowerPoint Presentations tool rather than inline — use "
                f"`read_powerpoint_presentation` to read a deck's slide text "
                f"and speaker notes, or pass it as `template_name` to "
                f"`create_powerpoint_presentation` to build on its layouts._"
            )
        else:
            parts.append(
                f"_Attached presentation(s) {names} can't be read inline. To "
                f"work with them, enable **PowerPoint Presentations** under "
                f"Customize → Tools in the sidebar, then re-send your "
                f"message._"
            )

    if oversized_inline:
        names = ", ".join(f"`{f.filename}`" for f in oversized_inline)
        parts.append(
            f"_Attached file(s) {names} exceed the inline document size limit "
            f"and were skipped. Try a smaller file, or convert to CSV/XLSX "
            f"and use the Spreadsheet Analysis tool._"
        )

    if over_budget:
        names = ", ".join(f"`{f.filename}`" for f in over_budget)
        parts.append(
            f"_Attached file(s) {names} were skipped because this message's "
            f"attachments together exceed the combined size limit for a "
            f"single message. Send them in a follow-up message._"
        )

    if dropped_over_count_total > 0:
        limit = f"{max_files} file" + ("s" if max_files != 1 else "")
        if dropped_over_count_names:
            names = ", ".join(f"`{n}`" for n in dropped_over_count_names)
            unnamed = dropped_over_count_total - len(dropped_over_count_names)
            tail = f" and {unnamed} more" if unnamed > 0 else ""
            parts.append(
                f"_Only the first {limit} per message are attached; "
                f"{names}{tail} were not. Send them in a follow-up message._"
            )
        else:
            noun = "file was" if dropped_over_count_total == 1 else "files were"
            parts.append(
                f"_Only the first {limit} per message are attached; "
                f"{dropped_over_count_total} more {noun} not. "
                f"Send them in a follow-up message._"
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


def _select_recovered_attachments(
    recovered_upload_ids: list[str],
    *,
    request_upload_ids: Optional[list] = None,
    request_files: Optional[list] = None,
) -> list[str]:
    """Decide whether a previous turn's unconsumed attachments may be re-sent.

    Returns the IDs to re-attach, or ``[]`` to leave the turn alone.

    The user's own attachments always win: when this turn carries anything of
    its own, recovered IDs are dropped entirely rather than merged. Two
    reasons. The resolver caps a turn at 5 files, so prepending stale IDs
    could silently push out a file the user deliberately attached now. And a
    user who re-uploads by hand has already expressed what they want sent —
    merging would duplicate those bytes, which Bedrock rejects outright once
    two document blocks share a sanitized name.
    """
    if not recovered_upload_ids:
        return []
    if request_upload_ids or request_files:
        logger.info(
            "Dropping %d recovered attachment(s) — this turn carries its own",
            len(recovered_upload_ids),
        )
        return []
    return list(recovered_upload_ids)


def _build_attachment_recovery_note(filenames: list[str]) -> str:
    """Note prepended when this turn re-sends attachments the previous turn
    never got an answer for (see `pop_pending_attachments`).

    Without it the model sees documents the user's current message says
    nothing about — the `[Attached files: …]` marker is appended by
    `PromptBuilder.build_prompt` whether the user attached them this turn or
    the server recovered them — and is left to guess whether the user meant
    to re-send them. Rides the same `original_message` displayText split as
    the interruption note, so the user never sees it.
    """
    names = ", ".join(filenames)
    return (
        "<attachment_recovery_note>\n"
        f"The attachment(s) {names} are re-sent from the user's previous turn, "
        "which failed before you could read them. The user did not attach them "
        "again — treat them as part of the request they were originally sent "
        "with, and do not ask the user to upload them.\n"
        "</attachment_recovery_note>"
    )


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

    AgentCore's idle reaper reads ``time_of_last_update`` (int epoch seconds)
    alongside ``status`` and terminates the microVM once
    ``now - time_of_last_update`` exceeds ``idleRuntimeSessionTimeout``.

    The payload is built by ``apis.inference_api.runtime_health``, which
    reports ``HealthyBusy`` with a refreshing timestamp while a turn is in
    flight — preserving the mid-stream reap protection this endpoint gained
    in PR #338 (bedrock-agentcore-sdk-python#471) — and ``Healthy`` with a
    *frozen* timestamp once the container is idle, so the reaper can
    actually fire. Returning a fresh timestamp unconditionally, as this
    handler previously did, made every microVM immortal until
    ``maxLifetime``; see that module's docstring for the measured cost.
    """
    return ping_payload()


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


def _resolve_invoked_skill_slugs(
    effective_skill_ids: Optional[list[str]], invoked_skills: Optional[list[str]]
) -> list[str]:
    """Activation slugs for the skills the user named with a `/` command.

    Intersected against the turn's **effective** set — the same narrow-never-grant
    rule ``_apply_enabled_skills_filter`` applies, re-run here because the effective
    set can still shrink after that call (an Agent's skill bindings replace it
    wholesale). A slash command for a skill the turn does not actually disclose is
    dropped rather than honoured: the directive would name a skill that is absent
    from ``<available_skills>``, and the model would burn a tool call discovering
    that.

    Returns slugs, not ids, because the slug is the activation key the ``skills``
    tool takes — the id never appears in anything the model can see.
    """
    if not invoked_skills or not effective_skill_ids:
        return []
    requested = set(invoked_skills)
    # Ordered by the effective set, not by the request: the directive is part of
    # the persisted message, and a list whose order followed client input would
    # differ between two turns that named the same skills.
    return [slugify_skill_name(sid) for sid in effective_skill_ids if sid in requested]


def _build_skill_invocation_note(skill_slugs: list[str]) -> str:
    """Directive appended to a turn whose user invoked skills by slash command.

    A slash command is an explicit instruction, not a hint — the user picked the
    skill by name from a menu. But the only activation path is the plugin's
    ``skills`` tool, which the *model* has to call, so "explicit" has to be
    expressed as a directive rather than enforced by pre-loading the instructions
    (doing that server-side would duplicate the plugin's response formatting and
    bypass its activation-state tracking).

    Kept to one line per skill. It rides the user message, so it is paid once as
    input on this turn and then again as cached history on every later turn of the
    session; the disclosure block it points at is already in the prefix either way,
    so this is the whole cost of the feature.
    """
    named = ", ".join(f"`{slug}`" for slug in skill_slugs)
    plural = "s" if len(skill_slugs) > 1 else ""
    return (
        f"[The user invoked the {named} skill{plural} with a slash command. "
        f"Activate {'each' if plural else 'it'} with the `skills` tool before "
        "answering, and follow the loaded instructions for this message.]"
    )


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

    # Where the pre-stream time goes. Everything between here and the
    # `StreamingResponse` return happens with NO channel open to the client —
    # measured at 3.75s on a warm turn — so this is the only way to see which
    # stage owns it. Pure timing: nothing reaches the model.
    # See `turn_timing.py` and docs/specs/agent-state-feedback.md.
    prelude = TurnPrelude()
    # Whether this turn's agent is built inside the stream (PR-3). Recorded on
    # the `turn_prelude` line so the two shapes stay distinguishable in the
    # logs once the flag has been on for a while.
    deferred_build = False

    # Refuse a turn against a session id another user already owns.
    #
    # Session ids travel in shareable URLs (`/s/{sessionId}`). Opening someone
    # else's link 404s on the metadata read, but the SPA then treats the
    # session as new and lets the user send — which used to fork the id: a
    # SECOND metadata row under the requester, on the same session, invisible
    # to both parties. In prod on 2026-08-31 that also left the original
    # owner's session resolving non-deterministically between the two rows.
    #
    # Not a confidentiality fix — conversation content is keyed by actor id in
    # AgentCore Memory, so the second user only ever saw an empty thread. This
    # stops the id from being forked at all. 404 rather than 403 so the
    # response says nothing about whether the session exists, matching what
    # `GET /sessions/{id}/metadata` already returns for the same case.
    #
    # ONE read of the session's META row, shared by everything in the preamble
    # that used to fetch it again (PR-2, docs/specs/turn-latency-preamble.md).
    # Measured on dev: eight separate reads of this item cost ~445ms of a
    # ~455ms stage, because a GSI query from an AgentCore Runtime container is
    # ~53ms rather than the ~12ms an in-region figure would suggest.
    #
    # Deliberately explicit rather than a per-request memo inside
    # `_get_session_by_gsi`: CLAUDE.md's "never cache session state" rule has
    # been paid for twice (#741, #751), and a snapshot callers opt into cannot
    # leak into one that needs a fresh read.
    session_meta = await load_session_meta(input_data.session_id, user_id)
    if session_meta.owned_by_other:
        logger.warning(
            "Rejected invocation for session %s — owned by a different user",
            _sanitize_log(input_data.session_id),
        )
        raise HTTPException(status_code=404, detail="Session not found")
    # First of the preamble's five sub-stages (docs/specs/turn-latency-preamble.md).
    # The coarse `preamble` number survives as `groups.preamble` in the emitted
    # line, so the four-turn baseline in the agent-state-feedback spec stays
    # comparable across this split.
    prelude.mark("preamble.ownership")
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
    # Near-zero on a turn that selects no skills — the opt-in default (D6)
    # short-circuits before touching RBAC or the skill table. A non-trivial
    # number here means the RBAC cache missed or the owner-index query is slow.
    prelude.mark("preamble.skills")
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
                # Same auto-enable seam as the main turn, so a spreadsheet
                # session's dispatch reads the slot the real turns fill.
                enabled_tools=await _apply_attachment_tool_autoenable(
                    input_data.enabled_tools, current_user, input_data.session_id, user_id
                ),
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
                # This path builds no injected tools, but shares a cache slot
                # with the real turns that do. Read the slot; never seed it.
                cache_write=False,
                assistant_id=input_data.rag_assistant_id,
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
            # 200 + envelope, not `status_code=e.code`: AgentCore Runtime
            # rewrites any non-2xx to a generic 424 and discards the
            # message, so a deliberate 409 ("connect the account") reached
            # the SPA as "check your CloudWatch logs". app-api restores the
            # real status. See `mcp_apps.error_envelope`.
            return app_tool_error_response(e.message, e.code)
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
                # Same auto-enable seam as the main turn, so a spreadsheet
                # session's dispatch reads the slot the real turns fill.
                enabled_tools=await _apply_attachment_tool_autoenable(
                    input_data.enabled_tools, current_user, input_data.session_id, user_id
                ),
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
                # Same partial-toolset hazard as app_tool_call above.
                cache_write=False,
                assistant_id=input_data.rag_assistant_id,
            )
            payload = dispatch_app_context_update(
                agent,
                resource_uri=acu.resource_uri,
                content=acu.content,
                structured_content=acu.structured_content,
            )
            return JSONResponse(payload)
        except AppContextUpdateError as e:
            # Same AgentCore flattening as the app_tool_call path above.
            return app_tool_error_response(e.message, e.code)
        except HTTPException:
            raise
        except Exception:
            logger.error("app context update invocation failed", exc_info=True)
            return JSONResponse({"error": "Internal error"}, status_code=500)

    if input_data.enabled_tools:
        logger.info(f"Enabled tools ({len(input_data.enabled_tools)})")

    # Recover attachments the PREVIOUS turn sent but never got an answer for.
    #
    # Inline document bytes are one-shot: they are stripped out of restored
    # history (see `TurnBasedSessionManager._strip_document_bytes`, which
    # exists because Bedrock rejects duplicate document names), so a turn that
    # dies before the model reads them consumes them for good. Prod session
    # `5f34d2b0` is the case this fixes — a ConverseStream carrying two PDFs
    # failed with ServiceUnavailableException, the PDFs were gone, and the
    # model's only recourse was to ask the user to upload them again.
    #
    # `pop_pending_attachments` clears the marker as it reads, and the marker
    # is TTL-bounded, so this can only ever influence the one turn directly
    # after a failure. Skipped entirely when the user attached something to
    # THIS turn — their own attachments are the authoritative intent, and the
    # 5-file resolver cap means silently prepending stale IDs could push a
    # deliberate attachment out of the request. See the write-ahead half of
    # this pair (`set_pending_attachments`) further down.
    recovered_upload_ids: list[str] = []
    if not is_resume and not is_continuation:
        try:
            from apis.shared.sessions.metadata import pop_pending_attachments
            recovered_upload_ids = await pop_pending_attachments(
                input_data.session_id, user_id, snapshot=session_meta
            )
        except Exception as e:
            logger.error("Failed to recover pending attachments: %s", e, exc_info=True)

        recovered_upload_ids = _select_recovered_attachments(
            recovered_upload_ids,
            request_upload_ids=input_data.file_upload_ids,
            request_files=input_data.files,
        )
        if recovered_upload_ids:
            logger.info(
                "Re-attaching %d file(s) from the previous unanswered turn",
                len(recovered_upload_ids),
            )
            input_data.file_upload_ids = recovered_upload_ids

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
    #   - presentation_files: pptx, also never sent inline — Bedrock's
    #     document-block format enum has no `pptx` member at all, so an
    #     inline deck is an unconditional ValidationException. They remain
    #     available via list/read_powerpoint_presentation, which extract
    #     slide text with python-pptx in Code Interpreter.
    #   - oversized_files: non-tabular docs that exceed our inline size
    #     budget; we skip them inline and surface a note instead of
    #     letting Bedrock reject the turn.
    all_files = list(input_data.files) if input_data.files else []
    upload_ids_to_resolve = list(input_data.file_upload_ids or [])

    # Per-message file count (spec §4E / PR-6). Applied here, before the
    # S3 fetch, so a sixth file is reported to the user instead of silently
    # truncated by the resolver — and so a client cannot fan out unbounded
    # S3 reads. With the guard off, the resolver's own backstop (5) applies
    # exactly as it did before.
    turn_guard_on = attachment_turn_guard_enabled()
    dropped_over_count_names: list[str] = []
    dropped_over_count_total = 0
    if turn_guard_on:
        (
            all_files,
            upload_ids_to_resolve,
            dropped_over_count_names,
            dropped_over_count_total,
        ) = _apply_message_file_cap(all_files, upload_ids_to_resolve, MAX_FILES_PER_MESSAGE)
        if dropped_over_count_total:
            logger.warning(
                "Dropped %d attachment(s) over the %d-per-message cap",
                dropped_over_count_total,
                MAX_FILES_PER_MESSAGE,
            )

    if upload_ids_to_resolve:
        try:
            file_resolver = get_file_resolver()
            resolved_files = await file_resolver.resolve_files(
                user_id=user_id,
                upload_ids=upload_ids_to_resolve,
                # Already capped above when the guard is on; the resolver's
                # own backstop is the pre-guard behaviour.
                max_files=None if turn_guard_on else 5,
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

    (
        files_to_send,
        diverted_tabular,
        diverted_presentations,
        oversized_inline,
    ) = _partition_attachments(all_files)
    if diverted_tabular:
        logger.info(
            f"Diverted {len(diverted_tabular)} tabular file(s) from inline document blocks; "
            f"available via spreadsheet tools: {[f.filename for f in diverted_tabular]}"
        )
    if diverted_presentations:
        logger.info(
            f"Diverted {len(diverted_presentations)} presentation(s) from inline document "
            f"blocks (Bedrock has no pptx document format); available via PowerPoint tools: "
            f"{[f.filename for f in diverted_presentations]}"
        )
    if oversized_inline:
        logger.warning(
            f"Skipped {len(oversized_inline)} oversized file(s) (> inline limit): "
            f"{[(f.filename, _estimate_decoded_size(f)) for f in oversized_inline]}"
        )

    # Aggregate budget for the turn (spec §4E / PR-6): the inline set is one
    # persisted message, and a message over ~7.5 MB raw fails the AgentCore
    # Memory write with SessionException. Trim first-fit in attachment order;
    # the trimmed files join the oversized note path, never the exception.
    over_budget_inline: list = []
    if turn_guard_on and files_to_send:
        files_to_send, over_budget_inline, requested_inline_bytes = _apply_inline_byte_budget(
            files_to_send, INLINE_ATTACHMENTS_MAX_TOTAL_BYTES
        )
        if over_budget_inline:
            logger.warning(
                "Attachment turn over quota: requested_bytes=%d cap_bytes=%d "
                "inline_files=%d dropped_files=%d",
                requested_inline_bytes,
                INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
                len(files_to_send) + len(over_budget_inline),
                len(over_budget_inline),
            )
            _emit_attachment_over_quota_metric(
                requested_bytes=requested_inline_bytes,
                cap_bytes=INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
                inline_count=len(files_to_send) + len(over_budget_inline),
                dropped_count=len(over_budget_inline),
            )

    # Both classes were dropped from the turn entirely; the marker must not
    # promise a card for either.
    attachment_marker_names = _attachment_marker_names(
        all_files, oversized_inline + over_budget_inline
    )

    # Covers the unconsumed-attachment recovery read, the S3 fetch behind
    # `resolve_files`, and the inline/tabular/oversized partitioning. Expected
    # to be ~0 on a turn with no attachments; if it is not, the hypothesis in
    # docs/specs/turn-latency-preamble.md is wrong about where the time is.
    prelude.mark("preamble.files")

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
        is_new_session = await ensure_session_metadata_exists(
            input_data.session_id, user_id, snapshot=session_meta
        )
        try:
            from apis.shared.sessions.metadata import (
                clear_paused_turn,
                clear_pending_interrupts,
            )
            await clear_paused_turn(input_data.session_id, user_id, snapshot=session_meta)
            # The snapshot's breadcrumbs go with it. They are the other half of
            # the same record, and a breadcrumb that outlives the snapshot
            # re-renders a prompt the user can no longer answer: the resume
            # route 400s on an interrupt id the rebuilt agent never saw. Safe
            # here specifically because this runs at the *head* of a non-resume
            # turn — any breadcrumb this turn goes on to write lands later, on
            # its own `done` event.
            await clear_pending_interrupts(
                input_data.session_id, user_id, snapshot=session_meta
            )
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
            await clear_truncated_turn(input_data.session_id, user_id, snapshot=session_meta)
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
            interrupted_turn_reason = await clear_interrupted_turn(
                input_data.session_id, user_id, snapshot=session_meta
            )
        except Exception as e:
            logger.error("Failed to clear stale interrupted_turn on new turn: %s", e, exc_info=True)

        # Write-ahead half of the unconsumed-attachment pair (the pop lives
        # up with the file-resolution block). Recorded BEFORE the model call
        # so it survives every way a turn can die — including the ones no
        # error handler sees, like the AgentCore data plane dropping the
        # stream. Cleared by StreamCoordinator the moment the turn produces
        # assistant content, so a turn that succeeds never leaves a marker
        # behind. Runs after ensure_session_metadata_exists above, which is
        # what guarantees the session row exists to update.
        if input_data.file_upload_ids:
            try:
                from apis.shared.sessions.metadata import set_pending_attachments
                await set_pending_attachments(
                    input_data.session_id, user_id, input_data.file_upload_ids
                )
            except Exception as e:
                logger.error("Failed to record pending attachments: %s", e, exc_info=True)

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

    # The prime suspect: five of the preamble's eight reads of the session's
    # META row live in this stage (pre-create plus the four stale-marker
    # clears), each on its own round trip, and four of them short-circuit
    # without writing anything. The title task is inside the boundary because
    # spawning it is first-turn session state; it is an `asyncio.create_task`,
    # so it contributes nothing to the number.
    prelude.mark("preamble.session_state")

    # Check quota if enforcement is enabled
    quota_warning_event = None
    quota_session_notice_event = None
    quota_exceeded_event = None
    if is_quota_enforcement_enabled() and not is_resume and not is_continuation:
        try:
            quota_checker = get_quota_checker()
            # Hand the quota checker the session cost we already read (PR-2b).
            # ONLY when the row actually carries `totalCost`: absent means a
            # legacy row that `get_session_metadata` still needs to backfill,
            # and `None` routes the checker back to that read. Passing 0.0 for
            # a missing attribute would silence the notice on exactly the
            # long-lived conversations it exists to catch.
            session_total_cost = None
            if session_meta.row is not None and "totalCost" in session_meta.row:
                try:
                    session_total_cost = float(session_meta.row["totalCost"])
                except (TypeError, ValueError):
                    session_total_cost = None
            quota_result = await quota_checker.check_quota(
                user=current_user,
                session_id=input_data.session_id,
                session_total_cost=session_total_cost,
            )

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

                # Independent of the per-user ladder: is THIS conversation
                # eating the month? (#833 PR-5 — the incident session spent
                # 90% of a user's quota while every per-user warning stayed
                # quiet until the day the block landed.)
                quota_session_notice_event = build_quota_session_notice_event(quota_result)
                if quota_session_notice_event:
                    logger.info("Quota session notice for user")

        except Exception as e:
            # Log error but don't block request - fail open for quota errors
            logger.error("Error checking quota for user", exc_info=True)

    # The quota round trip: a cached tier resolve, a cached O(1) cost-summary
    # GetItem, and — the uncached one — the per-session notice, which reads the
    # META row for the eighth time this turn.
    prelude.mark("preamble.quota")

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

    # A **continuation** runs this block too, and that is the fix for a long-standing
    # invisible failure: "Continue" after a max_tokens truncation used to skip the whole
    # block, so a properly launched Agent conversation finished its reply with none of the
    # Agent's tools, skills, model or instructions — the same silent capability loss the
    # `@`-mention used to cause, on the path users are told to use. The SPA already resends
    # `rag_assistant_id` on a continuation for exactly this reason; only this guard
    # discarded it.
    #
    # Three steps inside stay gated on `not is_continuation`, each for its own reason:
    # binding **validation** and **persistence** (steps 1 and 6) because a continuation
    # binds nothing new — it is finishing a turn the binding already governs — and **RAG**
    # (steps 3/4) because a continuation carries an empty message, so a knowledge-base
    # search would spend a query on "" and augment nothing. The context the original turn
    # retrieved is already in the history being continued.
    #
    # A **resume** still skips the block entirely: it rebuilds from its `PausedTurnSnapshot`,
    # which replays the original turn's exact `enabled_tools` / `system_prompt` /
    # `enabled_skills` in order to reconstruct the same prompt-cache key. Re-resolving here
    # would risk a different effective set and orphan the paused agent.
    if input_data.rag_assistant_id and not is_resume:
        # Local imports to avoid circular dependency
        from apis.shared.assistants.kb_access import granted
        from apis.shared.assistants.rag_service import (
            augment_prompt_with_context,
            resolve_context_cap,
            search_assistant_knowledgebase_with_formatting,
        )
        from apis.shared.assistants.service import (
            get_assistant_with_access_check,
            mark_share_as_interacted,
        )
        from apis.shared.assistants.version_resolution import (
            AgentVersionUnavailableError,
            resolve_invocation_agent,
            resolve_review_agent,
        )
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

        # Does the thread already have messages? Only a *mention* turn needs the
        # answer — for every other turn `binds_conversation` says True regardless —
        # so this read stays off the hot path rather than costing every bound Agent
        # turn a query it cannot act on. `None` means "not looked up yet"; the
        # validation below reuses the value when it is already known.
        thread_is_empty: Optional[bool] = None
        if (
            is_agent_mention
            and not is_continuation
            and not is_preview_session(input_data.session_id)
        ):
            thread_is_empty = not await _session_has_messages(
                session_id=input_data.session_id, user_id=user_id
            )

        # 1. Check if session already has an assistant attached
        # If it does, verify it's the same assistant (can't change assistants mid-session)
        # If it doesn't, verify session has no messages (can only attach to new sessions)
        # Skip validation for preview sessions (they don't persist state)
        #
        # A mention that *starts* a thread binds it, exactly like launching the Agent:
        # there is no history produced under other instructions for either rule to
        # protect, and leaving it unbound is what made the next message silently lose
        # the Agent's tools. A mention into a thread that already has messages still
        # skips both rules — the current SPA opens a new conversation instead of
        # sending one, so this is the legacy-client path. The Agent's own access check
        # below is untouched either way: this relaxes *binding* semantics, never
        # authorization. Rule in ``agent_binding_policy`` so it is testable without
        # this stack.
        if not is_continuation and binds_conversation(
            is_agent_mention=is_agent_mention,
            is_preview=is_preview_session(input_data.session_id),
            thread_is_empty=bool(thread_is_empty),
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
                    # Reuse the mention path's lookup rather than repeating it.
                    if thread_is_empty is None:
                        thread_is_empty = not await _session_has_messages(
                            session_id=input_data.session_id, user_id=user_id
                        )
                    if not thread_is_empty:
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
        #
        # ⚠️ The reviewer preview is the ONE path that does not go through the visibility
        # gate, and it earns that by proving the scope first. A PRIVATE Agent can be sitting
        # in the review queue — approval refuses one, but submission does not — and
        # ``get_assistant_with_access_check`` refuses a non-owner outright on PRIVATE, so a
        # reviewer could not test-drive exactly the submissions most worth testing.
        #
        # The scope is re-resolved here against the caller's own roles rather than trusted
        # from the request, and a caller without it is refused rather than quietly demoted
        # to an ordinary turn: a silent downgrade would run the published snapshot (or the
        # author's draft) and report it to the reviewer as the version under review.
        is_review_preview = bool(input_data.review_preview)
        if is_review_preview:
            import os

            from apis.shared.auth import has_admin_scope
            from apis.shared.assistants.service import (
                _get_assistant_cloud_without_ownership_check,
            )

            if not await has_admin_scope(current_user, "admin.marketplace"):
                logger.warning("review_preview requested without the marketplace scope")
                raise HTTPException(
                    status_code=403,
                    detail="Reviewing an agent requires marketplace admin access.",
                )
            table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
            if not table_name:
                # Explicit, because the alternative is a confusing failure several frames
                # down inside boto3 rather than a message naming the missing variable.
                raise RuntimeError(
                    "DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required"
                )
            assistant = await _get_assistant_cloud_without_ownership_check(
                input_data.rag_assistant_id, table_name
            )
            # No permission was resolved, because this path deliberately bypassed the
            # gate that resolves one. Left as None so the knowledge base read below
            # fails closed (Requirement 25.1): `granted(...)` treats None as "grants
            # nothing", and the marketplace scope is authority to *review a
            # submission*, not evidence of a read grant on that owner's corpus.
            #
            # ⚠️ Consequence worth owning: a reviewer test-drives with an empty
            # knowledge base, which is a degraded review of a RAG-backed Agent — the
            # same class of problem develop's comment above warns about. Whether a
            # marketplace reviewer should receive corpus read is a policy decision for
            # the marketplace owner, not something to settle inside a merge conflict.
            # Tracked rather than guessed; failing closed is the safe default meanwhile.
            assistant_permission = None
        else:
            logger.info("Loading assistant with access check...")
            # The permission is kept, not discarded: it is what the knowledge base
            # retrieval below runs under (Requirement 25.1), so the grant that governs
            # the corpus read is provably the same one that admitted this turn.
            assistant, assistant_permission = await get_assistant_with_access_check(
                assistant_id=input_data.rag_assistant_id,
                user_id=user_id,
                user_email=current_user.email,
            )

        if not assistant:
            logger.warning(
                "Assistant lookup returned None (review_preview=%s)", is_review_preview
            )
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
            if is_review_preview:
                # The submitted snapshot, not the published one and not the author's draft —
                # the artifact the reviewer's decision is actually about. Shares its rule
                # with the admin submission read, so the page and the test drive can never
                # disagree about what is under review.
                assistant, resolved_version = await resolve_review_agent(assistant)
            else:
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
            f"published version {scrub_log(resolved_version)}" if resolved_version else "live record / draft",
        )

        # ⚠️ Both bookkeeping writes below are skipped for a reviewer preview, because a
        # review is not use. ``mark_share_as_interacted`` would stamp a share record the
        # reviewer does not have, and ``bump_last_used_at`` feeds the KB-sync inactivity
        # pause — a reviewer poking a submission once would read as the Agent being live
        # and wake sync policies that were correctly dormant. Persistence of the turn
        # itself is already handled by the ``preview-`` session id the reviewer's client
        # sends (``PREVIEW_SESSION_PREFIX``), which is what keeps the test drive out of the
        # author's conversation history.
        if not is_review_preview:
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
                logger.warning(
                    f"lastUsedAt bump failed for assistant "
                    f"{scrub_log(input_data.rag_assistant_id)}: {scrub_log(bump_err)}"
                )

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

        # Skipped on a continuation: the turn carries an empty message, so a
        # knowledge-base search would spend a query on "" and augment nothing. The
        # context the original turn retrieved is already in the history being continued.
        if not is_continuation:
            # 3. Search assistant knowledge base
            logger.info("Starting knowledge base search for assistant...")
            try:
                logger.info("Searching knowledge base for assistant...")
                context_chunks = await search_assistant_knowledgebase_with_formatting(
                    assistant_id=input_data.rag_assistant_id,
                    query=input_data.message,
                    top_k=5,
                    access=granted(input_data.rag_assistant_id, user_id, assistant_permission),
                )
                logger.info(f"Knowledge base search returned {len(context_chunks) if context_chunks else 0} chunks")
                if context_chunks:
                    for i, chunk in enumerate(context_chunks):
                        logger.info(f"Chunk {i + 1} retrieved")
                        logger.info(f"Chunk {i + 1} metadata retrieved")

                # 4. Augment message with context
                if context_chunks:
                    # Engine-aware cap (Requirement 3.2): managed gets 8,000 so
                    # reranking's top_k chunks actually reach the model; legacy keeps
                    # 2,000. See rag_service.resolve_context_cap / HANDOFF §5.40.
                    cap = resolve_context_cap(input_data.rag_assistant_id)
                    augmented_message = augment_prompt_with_context(user_message=input_data.message, context_chunks=context_chunks, max_context_length=cap)
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
        # Skip persistence for preview sessions and for continuations (a continuation
        # binds nothing new — it is finishing a turn the binding already governs).
        #
        # ⚠️ Must use the SAME predicate, with the same arguments, as the validation
        # above. Validating without persisting refuses the second mention in a thread;
        # persisting without validating lets a mention annex a conversation whose
        # history was produced under other instructions. `thread_is_empty` is the
        # argument that makes a thread-starting mention bind, so it has to be passed
        # here too — dropping it is a silent one-turn Agent all over again.
        if not is_continuation and binds_conversation(
            is_agent_mention=is_agent_mention,
            is_preview=is_preview_session(input_data.session_id),
            thread_is_empty=bool(thread_is_empty),
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

    # Assistant resolution, the knowledge-base search and its metadata writes.
    # Zero on a plain chat turn, which is what makes it worth separating.
    prelude.mark("rag")

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

        # Mid-turn steering, paused-turn path (docs/specs/mid-turn-steering.md).
        # A turn paused for consent or approval had no running loop to steer,
        # and the pause released its lease — inbox and all. Follow-ups the user
        # queued meanwhile ride the resume request and are seeded onto the lease
        # we just took, so the ordinary SteeringHook injects them at this turn's
        # first tool boundary. One injection path, one ack path, whichever way
        # the entry arrived. Best-effort: a failed seed degrades to the
        # composer's end-of-turn flush.
        if input_data.steering and mid_turn_steering_enabled():
            try:
                from apis.shared.sessions.session_lease import seed_steer_queue

                await seed_steer_queue(
                    session_lease,
                    [{"id": entry.id, "text": entry.text} for entry in input_data.steering],
                )
            except Exception:
                logger.warning("Failed to seed carried-over steering", exc_info=True)

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
                # The original turn's key carried whether the session had a
                # readable document; the gate is monotonic, so re-asking it
                # rebuilds the same key (an orphaned paused agent otherwise).
                has_document_tools=await _document_tools_gate(
                    input_data.session_id, user_id
                ),
                # The assistant the original turn ran against is a key element
                # (spreadsheet tools close over it). Replay the snapshot's
                # value, not the request's: a snapshot written before the
                # field existed carries None, which misses the slot and
                # rebuilds — the pre-existing eviction path, never a wrong hit.
                assistant_id=snapshot.assistant_id,
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
            # A session holding a spreadsheet gets the Spreadsheet Analysis
            # tools whether or not the picker has them on, gated on the
            # caller's RBAC grant. Applied to the *effective* list so it
            # flows into the cache key, every builder below, the attachment
            # guidance and the paused-turn snapshot as one value. Sticky
            # across the session (see `_session_has_tabular`), so the key
            # does not flip between the attach turn and the follow-up.
            effective_enabled_tools = await _apply_attachment_tool_autoenable(
                effective_enabled_tools,
                current_user,
                input_data.session_id,
                user_id,
                turn_has_tabular=bool(diverted_tabular),
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
            )

            memory_tools = _build_memory_tools(
                agent_memory=agent_memory,
                user_id=user_id,
                user_email=current_user.email,
            )
            extra_tools = extra_tools + memory_tools

            # document_read for any session that carries a readable attachment
            # (this turn's uploads count). Gated on session state, not the
            # picker; its presence goes into the cache key below rather than
            # vetoing the cache, so an attachment session that could keep a
            # warm agent still does.
            document_tools = await _build_document_tools(
                session_id=input_data.session_id,
                user_id=user_id,
                turn_upload_ids=input_data.file_upload_ids,
            )
            extra_tools = extra_tools + document_tools

            # Can this turn's agent be cached despite carrying injected tools?
            # Only when every builder that fired closes over values the cache
            # key already carries (session, user, enabled_tools). Derived from
            # the same `effective_enabled_tools` that goes into the key below —
            # passing the request's list here instead would let the predicate
            # and the key disagree about which builders ran.
            extra_tools_key_described = injected_tools_are_key_described(
                enabled_tools=effective_enabled_tools,
                has_memory_binding=bool(memory_tools),
            )

            # System-prompt assembly, the single-flight lease, skill
            # resolution and every tool builder (documents, attachments,
            # memory, agent binding).
            prelude.mark("tools")

            def _mark_build_stage(stage: str) -> None:
                """Namespace a build sub-stage under `agent_build.` so the
                emitted line groups them the way the preamble's are."""
                prelude.mark(f"agent_build.{stage}")

            async def _build_main_agent():
                """The turn's agent. Called eagerly here, or from the stream.

                A closure rather than an inline call because it now has two
                call sites — see `defer_agent_build` below — and eighteen
                keyword arguments that must not drift between them.
                """
                return await get_agent(
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
                    extra_tools_key_described=extra_tools_key_described,
                    has_document_tools=bool(document_tools),
                    assistant_id=input_data.rag_assistant_id,
                    build_stage_recorder=_mark_build_stage,
                )

            # Defer the build into the stream so it can be narrated.
            #
            # Measured on dev: a cold agent-cache miss spends 1478ms here
            # against a 2542ms pre-stream window, and every millisecond of it
            # is dead air — FastAPI flushes headers when this handler returns,
            # so until then there is no channel to say anything on. Deferring
            # opens the response first and emits a `preparing` frame, turning
            # the longest silence in the product into a sentence.
            #
            # A warm turn spends 0-38ms here, so this changes nothing for the
            # common case; it exists for the cold one.
            # See docs/specs/agent-state-feedback.md PR-3.
            if agent_preparing_phase_enabled():
                agent = None
                deferred_build = True
            else:
                agent = await _build_main_agent()
                # The remainder after the sub-stages the build itself recorded;
                # `groups.agent_build` sums them, keeping the pre-split
                # number comparable exactly as it did for the preamble.
                prelude.mark("agent_build.rest")

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

            # …then the per-session notice. Separate surface, separate
            # dismissal: "your month is 80% gone" and "this thread is a
            # quarter of your month" are different actions for the user.
            if quota_session_notice_event:
                yield quota_session_notice_event.to_sse_format()

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
                diverted_tabular,
                diverted_presentations,
                oversized_inline,
                effective_enabled_tools,
                over_budget=over_budget_inline,
                dropped_over_count_names=dropped_over_count_names,
                dropped_over_count_total=dropped_over_count_total,
                max_files=MAX_FILES_PER_MESSAGE,
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
                # Unconsumed-attachment recovery: this turn is re-sending
                # files the previous turn never got an answer for. Say so, or
                # the model has to guess why documents it was not asked about
                # are attached to the message.
                if recovered_upload_ids and attachment_marker_names:
                    final_message = (
                        f"{_build_attachment_recovery_note(attachment_marker_names)}\n\n{final_message}"
                    )

                if interrupted_turn_reason:
                    final_message = (
                        f"{_build_interruption_note(interrupted_turn_reason)}\n\n{final_message}"
                    )

                # Slash commands: the user named one or more skills in the
                # composer. Appended LAST, after every prepended note, so the
                # directive is the closest thing to the model's first token —
                # an instruction about what to do with the message it follows.
                # Narrowed against the effective set here rather than at parse
                # time because an Agent's skill bindings can still have
                # replaced that set above.
                invoked_skill_slugs = _resolve_invoked_skill_slugs(
                    effective_skill_ids, input_data.invoked_skills
                )
                if invoked_skill_slugs:
                    final_message = (
                        f"{final_message}\n\n{_build_skill_invocation_note(invoked_skill_slugs)}"
                    )

            message_will_be_modified = (
                final_message != input_data.message  # RAG augmentation / attachment guidance / inventory
                or bool(files_to_send)               # File attachments
                # The `[Attached files: …]` marker is appended for diverted
                # attachments too, so the persisted text differs from what the
                # user typed even when nothing went inline (a lone .pptx).
                or bool(attachment_marker_names)
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
                attachment_names=attachment_marker_names or None,
                citations=citations_for_storage if citations_for_storage else None,
                original_message=input_data.message if message_will_be_modified else None,
                interrupt_responses=interrupt_responses_payload,
                continue_truncated=is_continuation,
                # The turn's true start, so the end-of-turn recap spans the
                # WHOLE turn. The coordinator's own clock starts when its
                # generator is iterated, which is AFTER the deferred agent
                # build — measured on dev, that under-reported a 7.8s turn as
                # 2.1s.
                turn_started_at=prelude.started_at,
                # Which Agent ran this turn (#756). Recorded on the cost row so a
                # deliberate `@`-mention prefix swap is distinguishable from the
                # nondeterministic-ordering regression the fingerprints exist to catch.
                # Passed per turn rather than read off the agent: the agent instance is
                # cached and shared across turns, so per-turn state must never live on it
                # (see #741/#751).
                turn_agent_id=input_data.rag_assistant_id,
                # This turn's lease doubles as the mid-turn steering inbox
                # (docs/specs/mid-turn-steering.md). Passed per turn for the
                # same reason as turn_agent_id — the agent is cached, the lease
                # is not. None for preview sessions and the local
                # no-DynamoDB path, where steering is simply inert.
                turn_lease=session_lease,
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
            nonlocal agent

            heartbeat_task = None
            try:
                # The deferred agent build (PR-3). Narrated, because this is
                # where a cold turn spends over a second with nothing on the
                # wire. The frame goes out FIRST so the client hears something
                # the moment the response opens; the build follows.
                if agent is None:
                    # Announce the build unconditionally; the SPA decides
                    # whether it is worth SHOWING.
                    #
                    # This used to race the build against a 250ms timer here
                    # and emit only if it was still running, so a warm build
                    # (0-38ms) never flashed a phase nobody can read. That
                    # cannot work: `create_agent` is synchronous
                    # (`agent_factory.py`), so a cold build occupies the event
                    # loop for its whole duration and `asyncio.wait` cannot
                    # fire its timeout — it returned only once the build was
                    # already done, `finished` was non-empty, and the frame was
                    # never sent. Verified on dev: a 1548ms build, six times
                    # the threshold, emitted nothing.
                    #
                    # A timer only works where the clock actually runs, which
                    # is the client. The SPA holds this phase for 250ms before
                    # rendering it, so a warm build still never shows — see
                    # `message-list.component.ts`.
                    yield (
                        "event: agent_status\ndata: "
                        + json.dumps(
                            {
                                "type": "agent_status",
                                "sessionId": input_data.session_id,
                                "phase": "preparing",
                            }
                        )
                        + "\n\n"
                    )
                    try:
                        agent = await _build_main_agent()
                    except Exception as build_error:
                        # The handler has already returned, so the two `except`
                        # arms below cannot see this — a build that fails here
                        # would otherwise be a silent, hung stream. Surface it
                        # the way every other mid-stream failure is surfaced
                        # (CLAUDE.md: errors stream as assistant messages), and
                        # let the `finally` release the lease.
                        logger.error(
                            "Deferred agent build failed", exc_info=True
                        )
                        error_event = build_conversational_error_event(
                            code=ErrorCode.AGENT_ERROR,
                            error=build_error,
                            session_id=input_data.session_id,
                            recoverable=True,
                        )
                        async for frame in stream_conversational_message(
                            message=error_event.message,
                            stop_reason="error",
                            metadata_event=error_event,
                            session_id=input_data.session_id,
                            user_id=user_id,
                            user_input=input_data.message,
                        ):
                            yield frame
                        return
                    prelude.mark("agent_build.rest")

                    # Tell the client the build is OVER.
                    #
                    # Without this the SPA can only infer it from the next
                    # status, which is `thinking` — and that does not arrive
                    # until the head-of-turn context work and the event loop's
                    # startup have also run, well over the 250ms the SPA waits
                    # before rendering "Getting ready…". So a 1ms cache-hit
                    # build still showed the label: `preparing` was not a state
                    # with an end, it was just the latest event. Measured on
                    # dev, builds of 0ms, 1ms and 40ms all rendered it.
                    #
                    # `durationMs` is the build's own measured time, which is
                    # what makes this frame worth more than a bare marker: it
                    # says how long the thing the user was told about took.
                    yield (
                        "event: agent_status\ndata: "
                        + json.dumps(
                            {
                                "type": "agent_status",
                                "sessionId": input_data.session_id,
                                "phase": "prepared",
                                "durationMs": prelude.last_stage_ms,
                            }
                        )
                        + "\n\n"
                    )

                # Emitted here rather than before the return: with the build
                # deferred, "the window before the client can hear anything"
                # ends at the agent, not at the response.
                prelude.emit(
                    session_id=input_data.session_id,
                    stream_kind="agent",
                    extra={
                        "isResume": is_resume,
                        "hasAssistant": bool(input_data.rag_assistant_id),
                        "deferredBuild": deferred_build,
                    },
                )

                # Started only once the agent exists — it is the heartbeat's
                # first argument.
                heartbeat_task = (
                    asyncio.create_task(_lease_heartbeat_loop(session_lease, agent))
                    if session_lease is not None
                    else None
                )

                async for chunk in stream_with_quota_warning():
                    yield chunk
            finally:
                await _release_turn_lease(heartbeat_task, session_lease)

        # Everything after the agent build: citation assembly, the title task,
        # the lease acquire and the generator wiring. The `turn_prelude` line
        # itself is emitted from inside `_guarded_stream`, once the agent is
        # actually ready — with the build deferred, that is the true end of
        # the window this measures. The early-return paths above (quota
        # exceeded, app tool calls) are not agent turns and never emit.
        prelude.mark("stream_setup")

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
