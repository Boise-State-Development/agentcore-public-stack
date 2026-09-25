"""Sessions API request/response models

This module contains all session-related data models including:
- Session metadata models
- Message models (Message, MessageContent, MessageResponse, etc.)
- Session preferences and configuration
"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.sessions.session_lease import STEER_QUEUE_MAX_CHARS


class VisualDisplayState(BaseModel):
    """Display state for a single promoted visual (inline tool result)"""

    model_config = ConfigDict(populate_by_name=True)

    dismissed: bool = Field(default=False, description="User dismissed this visual")
    expanded: bool = Field(default=True, description="Visual is expanded vs collapsed")


class PendingInterrupt(BaseModel):
    """A paused-turn breadcrumb the frontend uses to rediscover prompts on
    reload — without it, a browser refresh leaves the prompt stuck and the
    tool call orphaned in ``pending`` forever.

    Four variants share this shape (discriminated by ``kind``):

    - ``oauth`` — written by ``OAuthConsentHook``. Carries ``provider_id``;
      the frontend re-fetches a fresh consent URL via ``initiate-consent``
      on Connect (URLs are short-lived; storing them invites stale-URL bugs).
    - ``tool_approval`` — written by ``MCPExternalApprovalHook``. Carries
      ``tool_name`` + ``tool_input`` + ``message`` so the inline approve/decline
      prompt rehydrates with the same context the user saw before refresh.
      ``tool_input`` is stored as a JSON-encoded string to avoid DynamoDB's
      Decimal/float coercion when the agent's tool input contains nested
      objects with floats.
    - ``user_question`` — written for the ``ask_user_question`` tool's own
      interrupt. Carries ``questions`` (JSON-encoded, same reasoning as
      ``tool_input``) so the picker rehydrates with the questions the user was
      already looking at. Unlike the other two this interrupt is raised by the
      tool itself via ``ToolContext``, not by a hook — the persisted shape and
      the resume path are identical either way.
    - ``browser_login`` — written for ``request_user_login``. Carries
      ``browser_session`` (JSON-encoded :class:`BrowserSessionRef`) so the live
      view can be re-offered after a refresh. Deliberately holds **no URL**:
      live-view URLs are SigV4 query-signed and expire within 300 seconds, so
      a stored one is always stale by the time it is read — app-api mints a
      fresh one per request instead (``docs/specs/authenticated-web-
      assessment.md`` D2).

    Default ``kind`` is ``oauth`` for backward compatibility with rows
    written before per-tool approval shipped.
    """

    model_config = ConfigDict(populate_by_name=True)
    interrupt_id: str = Field(..., alias="interruptId", description="Strands interrupt id used to resume the paused turn")
    kind: Literal["oauth", "tool_approval", "user_question", "browser_login"] = Field(
        default="oauth",
        description="Discriminator: which variant this interrupt represents",
    )
    triggering_message_id: Optional[str] = Field(
        None,
        alias="triggeringMessageId",
        description="Id of the assistant message whose tool call triggered this interrupt, when known",
    )
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp when the interrupt was recorded")

    # OAuth-only fields
    provider_id: Optional[str] = Field(
        default=None,
        alias="providerId",
        description="(oauth) Connector providerId needing consent",
    )

    # tool_approval-only fields
    tool_use_id: Optional[str] = Field(
        default=None,
        alias="toolUseId",
        description="(tool_approval) Strands tool-use id of the paused call",
    )
    tool_name: Optional[str] = Field(
        default=None,
        alias="toolName",
        description="(tool_approval) MCP-server-exposed name of the tool",
    )
    tool_input: Optional[str] = Field(
        default=None,
        alias="toolInput",
        description="(tool_approval) JSON-encoded tool input arguments",
    )
    message: Optional[str] = Field(
        default=None,
        description="(tool_approval) Admin-supplied or default approval message",
    )

    # user_question-only fields
    questions: Optional[str] = Field(
        default=None,
        description="(user_question) JSON-encoded list of questions to re-render",
    )

    # browser_login-only fields
    browser_session: Optional[str] = Field(
        default=None,
        alias="browserSession",
        description=(
            "(browser_login) JSON-encoded BrowserSessionRef — identifiers and "
            "viewport only, never a live-view URL"
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description="(browser_login) The agent's one-line explanation of what needs signing into",
    )


class PausedTurnSnapshot(BaseModel):
    """Frozen agent-construction context for a turn that paused on OAuth consent.

    Written once per paused turn so the resume request can rebuild the same
    ``MainAgent`` shape (matching tool registry, model, prompt) regardless of
    whether the in-process agent cache still holds it. Strands' session
    manager separately persists ``_interrupt_state`` to AgentCore Memory, so
    once the agent is rebuilt with the right shape the interrupt restores
    automatically and the paused tool call can resume.

    Snapshot wins over current request state on resume: a turn the user
    already authorized completes with the connector set it was authorized
    against, even if the user toggled connectors mid-pause.
    """

    model_config = ConfigDict(populate_by_name=True)
    enabled_tools: Optional[List[str]] = Field(default=None, alias="enabledTools")
    model_id: Optional[str] = Field(default=None, alias="modelId")
    provider: Optional[str] = Field(default=None)
    temperature: Optional[float] = Field(default=None)
    system_prompt: Optional[str] = Field(default=None, alias="systemPrompt")
    caching_enabled: Optional[bool] = Field(default=None, alias="cachingEnabled")
    max_tokens: Optional[int] = Field(default=None, alias="maxTokens")
    agent_type: Optional[str] = Field(default=None, alias="agentType")
    enabled_skills: Optional[List[str]] = Field(
        default=None,
        alias="enabledSkills",
        description="Effective skill ids the paused skill turn was built with. "
                    "Resume rebuilds the same skills_hash cache key from these "
                    "even if the user toggles skills mid-pause. None on chat "
                    "turns and on snapshots written before the field existed "
                    "(those fall back to request-time resolution).",
    )
    inference_params: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="inferenceParams",
        description="Canonical inference param dict captured at pause. When present, "
                    "supersedes the legacy temperature/max_tokens fields on resume."
    )
    mantle_api_mode: Optional[str] = Field(
        default=None,
        alias="mantleApiMode",
        description="Bedrock Mantle API surface ('chat' or 'responses') captured at "
                    "pause so a resumed Mantle turn rebuilds the same model class. None "
                    "for non-Mantle turns and snapshots written before the field existed.",
    )
    mantle_region: Optional[str] = Field(
        default=None,
        alias="mantleRegion",
        description="Bedrock Mantle region override captured at pause so a resumed "
                    "Mantle turn targets the same region. None for non-Mantle turns and "
                    "snapshots written before the field existed.",
    )
    assistant_id: Optional[str] = Field(
        default=None,
        alias="assistantId",
        description="Assistant (RAG corpus) the paused turn ran against. It is an "
                    "agent-cache key element because the spreadsheet-analysis tools "
                    "close over it, so resume replays it verbatim to land on the "
                    "paused agent's slot. None for assistant-less turns and snapshots "
                    "written before the field existed (those miss and rebuild).",
    )
    memory_binding: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="memoryBinding",
        description="The Agent's resolved Memory-Space binding ({spaceId, spaceName, "
                    "access}) the paused turn was built with. A cache-key element: "
                    "resume replays it or the paused agent is orphaned. None when the "
                    "turn had no binding and on snapshots written before the field existed.",
    )
    captured_at: str = Field(..., alias="capturedAt", description="ISO 8601 timestamp when the turn paused")
    expires_at: str = Field(..., alias="expiresAt", description="ISO 8601 timestamp after which the snapshot is no longer valid for resume")


class SessionPreferences(BaseModel):
    """User preferences for a session"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")
    last_model: Optional[str] = Field(default=None, alias="lastModel", description="Last model used in this session")
    enabled_tools: Optional[List[str]] = Field(default=None, alias="enabledTools", description="List of enabled tool names")
    selected_prompt_id: Optional[str] = Field(default=None, alias="selectedPromptId", description="ID of selected prompt template")
    custom_prompt_text: Optional[str] = Field(default=None, alias="customPromptText", description="Custom prompt text if used")
    assistant_id: Optional[str] = Field(default=None, alias="assistantId", description="Assistant ID attached to this session")
    project_id: Optional[str] = Field(default=None, alias="projectId", description="Shared Project whose harness this session runs, when assistantId is a project's agent")
    agent_type: Optional[str] = Field(default=None, alias="agentType", description="Agent mode this conversation runs in ('skill' or 'chat'); reopening the session restores it")

    # System prompt hash for tracking exact prompt version sent to the model
    # This is a hash of the FINAL rendered system prompt (after date injection, variable substitution, etc.)
    # Use cases:
    # - Track which exact prompt was used for each session
    # - Correlate prompt changes with model performance/cost metrics
    # - Detect when two sessions used identical prompts even if they selected different templates
    # - Enable prompt A/B testing and version tracking
    system_prompt_hash: Optional[str] = Field(default=None, alias="systemPromptHash", description="MD5 hash of final rendered system prompt")

    # Visual state for promoted tool results (charts, tables, etc.)
    # Keyed by tool_use_id, stores whether each visual is dismissed or collapsed
    visual_state: Optional[Dict[str, VisualDisplayState]] = Field(
        default=None,
        alias="visualState",
        description="Display state for promoted visuals, keyed by tool_use_id"
    )


class ExportReceipt(BaseModel):
    """Record of a successful conversation export to a connected app.

    Persisted on the session (and appended, one per export) so the SPA can
    show a "Saved to <app> · Open" affordance that survives a page reload.
    The write-direction mirror of `DocumentProvenance`: it records which
    connector/adapter produced the file, the remote file's identity and
    viewer link, and when the export happened. Generic across destinations —
    Google Drive is the first, OneDrive/Dropbox/Box reuse it unchanged.
    """

    model_config = ConfigDict(populate_by_name=True)

    connector_id: str = Field(..., alias="connectorId", description="OAuth connector the conversation was saved to")
    adapter_key: str = Field(..., alias="adapterKey", description="Export-target adapter that created the file")
    format: str = Field(..., description="Export format produced (e.g. 'google_doc', 'markdown')")
    file_id: str = Field(..., alias="fileId", description="Provider-side identifier of the created file")
    file_name: str = Field(..., alias="fileName", description="Name the file was created with")
    web_view_link: Optional[str] = Field(None, alias="webViewLink", description="Viewer URL surfaced as 'Open in <app>'; None if the provider returns none")
    exported_at: str = Field(..., alias="exportedAt", description="ISO 8601 timestamp of the export")


class SessionMetadata(BaseModel):
    """Complete session metadata

    DynamoDB Schema:
        PK: USER#{user_id}
        SK: S#ACTIVE#{last_message_at}#{session_id} (active sessions)
            S#DELETED#{deleted_at}#{session_id} (deleted sessions)

        GSI: SessionLookupIndex
            GSI_PK: SESSION#{session_id}
            GSI_SK: META
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")
    session_id: str = Field(..., alias="sessionId", description="Session identifier")
    user_id: str = Field(..., alias="userId", description="User identifier")
    title: str = Field(..., description="Session title (usually from first message)")
    status: Literal["active", "archived", "deleted"] = Field(..., description="Session status")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of session creation")
    last_message_at: str = Field(..., alias="lastMessageAt", description="ISO 8601 timestamp of last message")
    message_count: int = Field(..., alias="messageCount", description="Total number of messages in session")
    starred: Optional[bool] = Field(False, description="Whether session is starred/favorited")
    tags: Optional[List[str]] = Field(default_factory=list, description="Custom tags for organization")
    preferences: Optional[SessionPreferences] = Field(None, description="User preferences for this session")

    # Soft delete fields
    deleted: Optional[bool] = Field(False, description="Whether session is soft-deleted")
    deleted_at: Optional[str] = Field(None, alias="deletedAt", description="ISO 8601 timestamp of deletion")

    # OAuth consent state
    pending_interrupts: Optional[List[PendingInterrupt]] = Field(
        default=None,
        alias="pendingInterrupts",
        description="Pending OAuth consent interrupts that paused agent turns in this session",
    )
    paused_turn: Optional[PausedTurnSnapshot] = Field(
        default=None,
        alias="pausedTurn",
        description="Agent-construction snapshot for a turn paused on OAuth consent; cleared on successful resume or when a new turn supersedes it",
    )
    last_turn_continuable: Optional[bool] = Field(
        default=None,
        alias="lastTurnContinuable",
        description="True when the last turn ended in a recoverable max_tokens truncation; lets the 'Continue' affordance survive a page refresh. Cleared at the start of any new (non-interrupt-resume) turn",
    )
    last_turn_interrupted: Optional[bool] = Field(
        default=None,
        alias="lastTurnInterrupted",
        description="True when the last turn was interrupted before completion (user Stop, refresh, or dropped connection). Lets a reload show the 'response interrupted' state. Cleared at the start of any new (non-interrupt-resume) turn",
    )
    last_turn_interrupt_reason: Optional[
        Literal["user_stopped", "navigated_away", "connection_lost", "unknown"]
    ] = Field(
        default=None,
        alias="lastTurnInterruptReason",
        description=(
            "Why the last turn was interrupted, strongest client-attested reason first: "
            "'user_stopped' (deliberate Stop) > 'navigated_away' (page hidden/unloaded) > "
            "'connection_lost' (the server-side cancellation fallback) > 'unknown'. "
            "A weaker reason can never overwrite a stronger one — see set_interrupted_turn"
        ),
    )
    last_turn_interrupted_at: Optional[str] = Field(
        default=None,
        alias="lastTurnInterruptedAt",
        description="ISO 8601 timestamp when the interruption was detected",
    )
    pending_attachment_upload_ids: Optional[List[str]] = Field(
        default=None,
        alias="pendingAttachmentUploadIds",
        description=(
            "Upload IDs sent inline on the turn currently in flight, recorded before the "
            "model call. Inline document bytes are stripped from restored history, so a turn "
            "that dies before the model reads them consumes them for good — these let the "
            "next turn re-send them. Cleared as soon as a turn produces assistant content"
        ),
    )
    pending_attachments_at: Optional[str] = Field(
        default=None,
        alias="pendingAttachmentsAt",
        description="ISO 8601 timestamp the pending-attachment marker was written; recovery is TTL-bounded against it",
    )
    browser_session: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="browserSession",
        description=(
            "Identity of the AgentCore browser session this conversation is "
            "driving, projected here so app-api can mint a live-view URL for a "
            "takeover (docs/specs/authenticated-web-assessment.md D4). The "
            "agent-side source of truth stays on `agent.state`; this is the "
            "copy app-api can read, and per the 'one session, more than one "
            "agent' rule it is re-read per turn rather than cached on an agent "
            "instance. Identifiers, viewport and control state only — never a "
            "live-view URL, which is SigV4 query-signed and dead within 300 "
            "seconds of being minted"
        ),
    )

    # Denormalized cost + context aggregates for the session-cost badge.
    # Maintained by _bump_session_aggregates after each turn (write-time
    # aggregation), and lazily backfilled on read for legacy sessions.
    total_cost: Optional[float] = Field(
        default=None,
        alias="totalCost",
        description="Running USD cost summed across all message metadata records in this session",
    )
    last_context_tokens: Optional[int] = Field(
        default=None,
        alias="lastContextTokens",
        description="Input tokens consumed by the most recent turn (includes system prompt + tools)",
    )
    context_window: Optional[int] = Field(
        default=None,
        alias="contextWindow",
        description="Model max input tokens at the time of the most recent turn",
    )

    # Cumulative count of turns rolled into a compaction summary across this
    # session's lifetime. Lifted out of the nested `compaction` map at GET
    # time so the frontend can rehydrate the end-of-conversation indicator
    # without knowing the internal compaction-state shape.
    total_summarized_turns: Optional[int] = Field(
        default=None,
        alias="totalSummarizedTurns",
        description="Cumulative count of turns rolled into a compaction summary in this session",
    )

    # Receipts for conversation exports to connected apps (e.g. Google Drive),
    # appended one per successful "Save to…" so the UI can show a "Saved · Open"
    # affordance that survives a reload. Written race-free via add_export_receipt.
    export_receipts: Optional[List[ExportReceipt]] = Field(
        default=None,
        alias="exportReceipts",
        description="Receipts for conversation exports to connected apps, newest appended last",
    )

    # Durable "unread" flag: set when an UNATTENDED run (scheduled / headless)
    # completes in this session, since the user by definition wasn't watching.
    # Interactive turns never set it (the client tracks same-tab unread state).
    # Cleared server-side when the user opens the session (mark_session_read),
    # so the dot survives reload and reaches other devices. Not part of the SK.
    unread: Optional[bool] = Field(
        False,
        description="True when an unattended (scheduled) run left a response the user hasn't opened yet",
    )


class UpdateSessionMetadataRequest(BaseModel):
    """Request body for updating session metadata"""

    model_config = ConfigDict(populate_by_name=True)
    title: Optional[str] = Field(None, description="Session title")
    status: Optional[Literal["active", "archived", "deleted"]] = Field(None, description="Session status")
    starred: Optional[bool] = Field(None, description="Whether session is starred")
    tags: Optional[List[str]] = Field(None, description="Custom tags")
    last_model: Optional[str] = Field(None, alias="lastModel", description="Last model used")
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools", description="Enabled tools list")
    selected_prompt_id: Optional[str] = Field(None, alias="selectedPromptId", description="ID of selected prompt — send null to explicitly clear, omit to leave unchanged")
    custom_prompt_text: Optional[str] = Field(None, alias="customPromptText", description="Custom prompt text")
    system_prompt_hash: Optional[str] = Field(None, alias="systemPromptHash", description="MD5 hash of final rendered system prompt")
    assistant_id: Optional[str] = Field(None, alias="assistantId", description="Assistant ID attached to this session")
    agent_type: Optional[Literal["skill", "chat"]] = Field(None, alias="agentType", description="Agent mode for this conversation")


class SessionInterruptRequest(BaseModel):
    """Request body for the client stop signal (POST /sessions/{id}/interrupt).

    Only client-*attested* reasons are accepted here — the ones the browser
    is the sole witness to:

      * `user_stopped`    — the Stop button. Deliberate rejection of the
                            in-flight response.
      * `navigated_away`  — the page was hidden or unloaded (refresh, tab
                            close, navigation) while a turn was streaming.
                            NOT a rejection: the user left, they didn't say
                            "stop". Sent from a `pagehide` handler.

    `connection_lost` is never client-sent. It is the server-side
    cancellation backstop's fallback — literally "the stream died and nothing
    told us why" — and accepting it from a client would let a caller
    downgrade an attested reason.

    The distinction exists because `connection_lost` is otherwise
    unattributable: a refresh, a dropped socket, and a platform-side idle
    timeout all reach the container as an identical cancellation. Labelling
    departures at the source is what makes the remainder diagnosable.
    """

    reason: Literal["user_stopped", "navigated_away"] = Field(
        description="Interruption reason. Only client-attested reasons are accepted",
    )


class SessionSteerRequest(BaseModel):
    """Request body for a mid-turn steer (POST /sessions/{id}/steer).

    A follow-up the user typed while a response was still streaming. The
    client mints `entry_id` so the whole round trip is idempotent: it is the
    id the SPA holds on the queued composer entry, the id the runtime clears
    once the injection is committed to history, and the id the
    `steering_applied` SSE event names back. See docs/specs/mid-turn-steering.md.

    `text` is the user's words verbatim — the same string that would have been
    sent as a normal turn had the queue flushed at end of turn instead.
    """

    text: str = Field(
        min_length=1,
        max_length=STEER_QUEUE_MAX_CHARS,
        description="The follow-up to inject at the running turn's next tool boundary",
    )
    entry_id: str = Field(
        min_length=1,
        max_length=128,
        alias="entryId",
        description="Client-minted id for this queue entry; echoed back on steering_applied",
    )

    model_config = ConfigDict(populate_by_name=True)


class SessionSteerResponse(BaseModel):
    """Result of arming a mid-turn steer.

    `queued=False` is not an error: it means the turn ended between the user
    typing and this request landing, and the SPA should send the text as a
    normal turn instead (which is exactly what its end-of-turn flush already
    does). The endpoint answers 200 either way so the SPA never has to
    distinguish a lost race from a failure.
    """

    queued: bool = Field(description="Whether the entry was armed against a live turn")
    entry_id: str = Field(alias="entryId", description="The client-minted entry id")

    model_config = ConfigDict(populate_by_name=True)


class BrowserLiveViewResponse(BaseModel):
    """A short-lived Live View URL for the conversation's browser session.

    The URL is SigV4 *query*-signed and lives at most 300 seconds, so this is
    minted per request rather than stored or streamed
    (``docs/specs/authenticated-web-assessment.md`` D2). The viewer re-requests
    against ``expiresAt``, which is why a twenty-minute sign-in works.

    ``viewport`` rides the response because DCV's ``remoteWidth``/
    ``remoteHeight`` must match the browser session's real viewport or the
    stream crops — the viewer must not carry its own copy of 1280x800.

    ⚠️ This is the one place a presigned browser URL is allowed to travel, and
    only because it goes to an authenticated browser over the SPA's own
    session. It must never reach a tool result, an SSE event, a persisted row
    or a log line.
    """

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(..., description="Presigned Live View URL, valid until expiresAt")
    expires_at: str = Field(
        ...,
        alias="expiresAt",
        description="ISO 8601 instant the signature expires; the viewer refreshes before this",
    )
    viewport: Dict[str, int] = Field(
        ...,
        description="The browser session's real viewport, for DCV remoteWidth/remoteHeight",
    )
    control_state: str = Field(
        default="agent",
        alias="controlState",
        description=(
            "'user' while the automation stream is DISABLED and the human can "
            "drive; 'agent' when the view is read-only because the agent holds it"
        ),
    )


class SessionMetadataResponse(BaseModel):
    """Response containing session metadata"""

    model_config = ConfigDict(populate_by_name=True)
    session_id: str = Field(..., alias="sessionId", description="Session identifier")
    title: str = Field(..., description="Session title")
    status: Literal["active", "archived", "deleted"] = Field(..., description="Session status")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    last_message_at: str = Field(..., alias="lastMessageAt", description="ISO 8601 timestamp of last message")
    message_count: int = Field(..., alias="messageCount", description="Total message count")
    starred: Optional[bool] = Field(False, description="Whether starred")
    tags: Optional[List[str]] = Field(default_factory=list, description="Custom tags")
    preferences: Optional[SessionPreferences] = Field(None, description="Session preferences")
    deleted: Optional[bool] = Field(False, description="Whether session is soft-deleted")
    deleted_at: Optional[str] = Field(None, alias="deletedAt", description="ISO 8601 timestamp of deletion")
    total_cost: Optional[float] = Field(
        None,
        alias="totalCost",
        description="Running USD cost summed across all message metadata records in this session",
    )
    last_context_tokens: Optional[int] = Field(
        None,
        alias="lastContextTokens",
        description="Input tokens consumed by the most recent turn",
    )
    context_window: Optional[int] = Field(
        None,
        alias="contextWindow",
        description="Model max input tokens at the time of the most recent turn",
    )
    total_summarized_turns: Optional[int] = Field(
        default=None,
        alias="totalSummarizedTurns",
        description="Cumulative count of turns rolled into a compaction summary in this session",
    )
    last_turn_continuable: Optional[bool] = Field(
        default=None,
        alias="lastTurnContinuable",
        description="True when the last turn ended in a recoverable max_tokens truncation, so the client can re-show the 'Continue' affordance after a refresh",
    )
    last_turn_interrupted: Optional[bool] = Field(
        default=None,
        alias="lastTurnInterrupted",
        description="True when the last turn was interrupted before completion (user Stop, refresh, or dropped connection), so the client can show the 'response interrupted' state after a reload",
    )
    last_turn_interrupt_reason: Optional[str] = Field(
        default=None,
        alias="lastTurnInterruptReason",
        description="Why the last turn was interrupted: 'user_stopped' (deliberate Stop), 'navigated_away' (page hidden/unloaded mid-turn), or 'connection_lost' (the unattributable server-side fallback). Only 'user_stopped' suppresses the 'Continue' affordance on reload",
    )
    last_turn_interrupted_at: Optional[str] = Field(
        default=None,
        alias="lastTurnInterruptedAt",
        description="ISO 8601 timestamp when the interruption was detected",
    )
    export_receipts: Optional[List[ExportReceipt]] = Field(
        default=None,
        alias="exportReceipts",
        description="Receipts for conversation exports to connected apps, newest appended last; lets the UI restore a 'Saved · Open' affordance after a reload",
    )
    unread: Optional[bool] = Field(
        False,
        description="True when an unattended (scheduled) run left a response the user hasn't opened yet; drives the blue unread dot in the session list",
    )


class SessionsListResponse(BaseModel):
    """Response for listing sessions with pagination support"""

    model_config = ConfigDict(populate_by_name=True)
    sessions: List[SessionMetadataResponse] = Field(..., description="List of sessions for the user")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for retrieving the next page of results")


class BulkDeleteSessionsRequest(BaseModel):
    """Request body for bulk deleting sessions"""

    model_config = ConfigDict(populate_by_name=True)
    session_ids: List[str] = Field(..., alias="sessionIds", description="List of session IDs to delete", min_length=1, max_length=20)


class BulkDeleteSessionResult(BaseModel):
    """Result for a single session in bulk delete operation"""

    model_config = ConfigDict(populate_by_name=True)
    session_id: str = Field(..., alias="sessionId", description="Session identifier")
    success: bool = Field(..., description="Whether deletion was successful")
    error: Optional[str] = Field(None, description="Error message if deletion failed")


class BulkDeleteSessionsResponse(BaseModel):
    """Response for bulk delete sessions operation"""

    model_config = ConfigDict(populate_by_name=True)
    deleted_count: int = Field(..., alias="deletedCount", description="Number of sessions successfully deleted")
    failed_count: int = Field(..., alias="failedCount", description="Number of sessions that failed to delete")
    results: List[BulkDeleteSessionResult] = Field(..., description="Individual results for each session")


# ============================================================================
# Message Models
# ============================================================================

class MessageContent(BaseModel):
    """Individual content block in a message

    Supports all Bedrock Converse API content types including:
    - text: Plain text content
    - toolUse: Tool/function call
    - toolResult: Result from tool execution
    - image: Image content
    - document: Document content
    - reasoningContent: Chain-of-thought reasoning (Claude extended thinking, etc.)
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(..., description="Content type (text, toolUse, toolResult, reasoningContent, etc.)")
    text: Optional[str] = Field(None, description="Text content")
    # Add other fields as needed for different content types
    tool_use: Optional[Dict[str, Any]] = Field(None, alias="toolUse")
    tool_result: Optional[Dict[str, Any]] = Field(None, alias="toolResult")
    image: Optional[Dict[str, Any]] = Field(None)
    document: Optional[Dict[str, Any]] = Field(None)
    # Reasoning content for models that support extended thinking (Claude 3.7+, etc.)
    reasoning_content: Optional[Dict[str, Any]] = Field(None, alias="reasoningContent")


class LatencyMetrics(BaseModel):
    """Latency measurements in milliseconds.

    ``time_to_first_token`` is ``None`` when the provider did not emit
    ``timeToFirstByteMs`` and we couldn't compute it locally — distinct from
    a measured value of 0ms (which is physically impossible). Aggregations
    over TTFT must filter ``None`` so a missing measurement doesn't pull
    averages toward zero.
    """

    model_config = ConfigDict(populate_by_name=True)

    time_to_first_token: Optional[int] = Field(
        None,
        alias="timeToFirstToken",
        description="Time from request start to first token (ms); None if not measured",
    )
    end_to_end_latency: int = Field(..., alias="endToEndLatency", description="Total time from request start to completion (ms)")


class TokenUsage(BaseModel):
    """Token usage statistics from LLM"""

    model_config = ConfigDict(populate_by_name=True)

    input_tokens: int = Field(..., alias="inputTokens", description="Input tokens consumed")
    output_tokens: int = Field(..., alias="outputTokens", description="Output tokens generated")
    total_tokens: int = Field(..., alias="totalTokens", description="Total tokens (input + output)")
    cache_write_input_tokens: Optional[int] = Field(None, alias="cacheWriteInputTokens", description="Tokens written to cache")
    cache_read_input_tokens: Optional[int] = Field(None, alias="cacheReadInputTokens", description="Tokens read from cache")


class PricingSnapshot(BaseModel):
    """Pricing rates at time of request for historical accuracy"""

    model_config = ConfigDict(populate_by_name=True)

    input_price_per_mtok: float = Field(..., alias="inputPricePerMtok", description="Price per million input tokens (USD)")
    output_price_per_mtok: float = Field(..., alias="outputPricePerMtok", description="Price per million output tokens (USD)")
    cache_write_price_per_mtok: Optional[float] = Field(
        None, alias="cacheWritePricePerMtok", description="Price per million cache write tokens (USD) - Bedrock only"
    )
    cache_read_price_per_mtok: Optional[float] = Field(
        None, alias="cacheReadPricePerMtok", description="Price per million cache read tokens (USD) - Bedrock only"
    )
    currency: str = Field(default="USD", description="Currency code")
    snapshot_at: str = Field(..., alias="snapshotAt", description="ISO timestamp when pricing was captured")


class ModelInfo(BaseModel):
    """Model information for cost calculation and tracking"""

    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId", description="Full model identifier (e.g., anthropic.claude-3-5-sonnet-20241022-v2:0)")
    model_name: str = Field(..., alias="modelName", description="Human-readable model name (e.g., Claude 3.5 Sonnet)")
    model_version: Optional[str] = Field(None, alias="modelVersion", description="Model version (e.g., v2)")
    provider: Optional[str] = Field(None, description="LLM provider (bedrock, openai, gemini)")
    # Pricing snapshot for historical cost accuracy (optional - can calculate from config later)
    pricing_snapshot: Optional[PricingSnapshot] = Field(None, alias="pricingSnapshot", description="Pricing at time of request")


class Attribution(BaseModel):
    """Attribution information for cost tracking and billing"""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId", description="User identifier")
    session_id: str = Field(..., alias="sessionId", description="Session/conversation identifier")
    timestamp: str = Field(..., description="ISO 8601 timestamp of message creation")
    # Future: Organization/team for multi-tenant billing
    organization_id: Optional[str] = Field(None, alias="organizationId", description="Organization identifier for multi-tenant billing")
    # Future: Tags for cost allocation (project, department, etc.)
    tags: Optional[Dict[str, str]] = Field(None, description="Custom tags for cost allocation")


class Citation(BaseModel):
    """Citation from RAG document retrieval"""

    model_config = ConfigDict(populate_by_name=True)

    assistant_id: str = Field(..., alias="assistantId", description="Assistant identifier (needed for download URL endpoint)")
    document_id: str = Field(..., alias="documentId", description="Document identifier in the knowledge base")
    file_name: str = Field(..., alias="fileName", description="Original filename of the source document")
    text: str = Field(..., description="Relevant text excerpt from the document")


#: Reason codes a down-thumb may carry — the six buckets of
#: ``docs/specs/response-feedback.md`` §6, each of which routes to an
#: evaluator or an ops signal. A closed enum, never free text: the row is
#: content-free by construction so it can sit beside the ``C#`` cost row and
#: be read by the admin profile without reading the conversation. The spec's
#: "something else → free text" is deliberately not here; that hand-off is
#: the existing Agent report dialog (spec §3), which already has moderation.
FEEDBACK_REASONS = ("wrong", "instructions", "length", "tool_failed", "outdated", "other")
FeedbackReason = Literal["wrong", "instructions", "length", "tool_failed", "outdated", "other"]


#: Implicit signals (response-feedback spec §10): denser than thumbs, no UI
#: cost, written to the same ``F#`` family under ``signal: "implicit"`` and
#: never summed with them. ``copy`` = the response was copied out;
#: ``continue`` = a truncated / interrupted response was resumed. Edit-and-
#: resend has no affordance in the SPA yet; abandonment is deferred (its
#: base rate is indistinguishable from a satisfied user going quiet).
IMPLICIT_SIGNAL_KINDS = ("copy", "continue")
ImplicitSignalKind = Literal["copy", "continue"]


class ImplicitSignalRequest(BaseModel):
    """Body of ``POST /sessions/{id}/messages/{messageId}/signals``."""

    model_config = ConfigDict(populate_by_name=True)

    kind: ImplicitSignalKind = Field(..., description="Which implicit signal fired (closed enum)")


class MessageFeedback(BaseModel):
    """One user's thumb on one assistant message (``F#`` row, see
    ``apis.shared.sessions.metadata``). ``value`` is +1 (up) or -1 (down);
    ``reason`` is an optional code from ``FEEDBACK_REASONS``."""

    model_config = ConfigDict(populate_by_name=True)

    value: Literal[1, -1] = Field(..., description="+1 for thumbs up, -1 for thumbs down")
    reason: Optional[FeedbackReason] = Field(None, description="Optional reason code (never free text)")
    retry_message_id: Optional[int] = Field(
        None, alias="retryMessageId", ge=0,
        description="Index of the user message sent as a retry-with-correction after this thumb (content-free link)",
    )
    updated_at: str = Field(..., alias="updatedAt", description="ISO timestamp of the latest thumb")


class MessageFeedbackRequest(BaseModel):
    """Body of ``PUT /sessions/{id}/messages/{messageId}/feedback``."""

    model_config = ConfigDict(populate_by_name=True)

    value: Literal[1, -1] = Field(..., description="+1 for thumbs up, -1 for thumbs down")
    reason: Optional[FeedbackReason] = Field(None, description="Optional reason code (never free text)")
    retry_message_id: Optional[int] = Field(
        None, alias="retryMessageId", ge=0,
        description="Set when the user sent a retry-with-correction: that user message's index",
    )


class MessageMetadata(BaseModel):
    """Metadata associated with a single message"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    latency: Optional[LatencyMetrics] = Field(None, description="Latency measurements")
    token_usage: Optional[TokenUsage] = Field(None, alias="tokenUsage", description="Token usage statistics")
    model_info: Optional[ModelInfo] = Field(None, alias="modelInfo", description="Model information for cost tracking")
    attribution: Optional[Attribution] = Field(None, description="Attribution for cost tracking and billing")
    cost: Optional[Union[float, Dict[str, float]]] = Field(None, description="Cost for this message — either a total float (legacy) or a breakdown dict with total, inputCost, outputCost, cacheReadCost, cacheWriteCost")
    citations: Optional[List[Dict[str, str]]] = Field(None, description="RAG citations for this message (stored as dicts for flexible JSON storage)")
    display_text: Optional[str] = Field(None, alias="displayText", description="Original user message text before RAG augmentation (for clean UI display)")
    # One user's thumb on this message, merged from the ``F#`` row on read
    # (see ``apis.shared.sessions.metadata``). Content-free: a ±1, a timestamp
    # and an optional reason code.
    feedback: Optional[MessageFeedback] = Field(None, description="User thumbs up/down on this assistant message")
    # Set on the LAST assistant message of a turn only — it describes the turn,
    # not the message. See `turn_duration_ms` on the write path.
    turn_duration_ms: Optional[int] = Field(
        None,
        alias="turnDurationMs",
        description=(
            "How long the whole turn took, in ms, measured server-side from "
            "the invocation arriving to the stream ending. Deliberately NOT "
            "derivable from `latency.endToEndLatency`, which prefers the "
            "provider's own API-call time and so excludes tool execution and "
            "the pre-stream agent build."
        ),
    )


class Message(BaseModel):
    """Individual message in a conversation"""

    model_config = ConfigDict(populate_by_name=True)

    role: str = Field(..., description="Message role (user, assistant)")
    content: List[MessageContent] = Field(..., description="Message content blocks")
    timestamp: Optional[str] = Field(None, description="Message timestamp")
    metadata: Optional[MessageMetadata] = Field(None, description="Message metadata (latency, tokens, etc.)")


class MessageResponse(BaseModel):
    """Response model for a single message (matches frontend expectations)"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Unique identifier for the message")
    role: Literal["user", "assistant", "system"] = Field(..., description="Role of the message sender")
    content: List[MessageContent] = Field(..., description="List of content blocks in the message")
    created_at: str = Field(..., alias="createdAt", description="ISO timestamp when the message was created")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional metadata associated with the message")
    citations: Optional[List[Citation]] = Field(None, description="RAG citations from knowledge base retrieval (assistant messages only)")


class MessagesListResponse(BaseModel):
    """Response for listing messages with pagination support"""

    model_config = ConfigDict(populate_by_name=True)

    messages: List[MessageResponse] = Field(..., description="List of messages in the session")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for retrieving the next page of results")
    pending_interrupts: List[PendingInterrupt] = Field(
        default_factory=list,
        alias="pendingInterrupts",
        description="OAuth consent interrupts that paused agent turns in this session and are awaiting user action",
    )
    ui_resources: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="uiResources",
        description="Persisted MCP App UI resources (SEP-1865) for this session, each shaped like the inline `ui_resource` SSE event ({type, toolUseId, resourceUri, html, mimeType, csp, permissions, sandboxOrigin}). Replayed on load to re-seed McpAppStateService and re-instantiate the mcp-app-frame iframe. Returned only on the first page.",
    )
    tool_summaries: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="toolSummaries",
        description="Persisted model-generated tool-batch summaries for this session, each shaped like the live `tool_group_summary` SSE event ({batchId, toolUseIds, summary}). Replayed on load so a reloaded conversation keeps the prose line the user saw live instead of downgrading to the client-side deterministic formatter. Returned only on the first page.",
    )
