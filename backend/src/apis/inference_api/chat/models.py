"""Chat feature models

Contains Pydantic models for chat API requests and responses.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from apis.shared.sessions.session_lease import STEER_QUEUE_MAX_CHARS

# Hard upper bound on a user-supplied custom system prompt. Mirrors the
# limit applied inside SystemPromptBuilder.from_user_prompt — surfacing
# it at the API layer so oversized payloads are rejected before any
# downstream work runs.
MAX_USER_SYSTEM_PROMPT_CHARS = 8 * 1024


class FileContent(BaseModel):
    """File content (base64 encoded)"""

    filename: str
    content_type: str
    bytes: str  # Base64 encoded


class InterruptResponseEntry(BaseModel):
    """One user response to a Strands interrupt, in the SDK's prompt shape.

    Posted by the frontend after the user completes (or declines) an OAuth
    consent popup. The backend forwards the list verbatim to
    `agent.stream_async(...)` to resume the paused turn.
    """

    interruptId: str
    response: Any = None


class AppToolCallEntry(BaseModel):
    """An app-initiated `tools/call` proxied from an embedded MCP App.

    MCP Apps PR #5. The iframe's JSON-RPC `tools/call` is relayed by
    app-api to `/invocations` with this directive. When set, the route
    does NOT run a model turn: it dispatches the single named tool against
    the conversation's live MCP client (rebuilding the agent like a resume
    so the client session/auth are wired identically), then returns the
    `CallToolResult` and publishes synthesized `tool_use`/`tool_result`
    into the conversation thread via the per-session event broker.

    `tool_use_id` is the originating MCP App's tool-use id; proxied calls
    inherit that conversation/iframe binding for provenance.
    """

    tool_use_id: str
    tool_name: str
    arguments: Dict[str, Any] = {}


class AppContextUpdateEntry(BaseModel):
    """App-supplied model context pushed via `ui/update-model-context`.

    MCP Apps PR #6. The embedded App's JSON-RPC `ui/update-model-context`
    is relayed by app-api to `/invocations` with this directive. Like
    `app_tool_call` it runs NO model turn — it stashes the payload on the
    conversation agent's Strands `agent.state` under
    `mcp_apps.context[resource_uri]`. The next real user turn merges any
    pending entries into that turn's prompt and clears them.

    `resource_uri` is the bound MCP App resource (`ui://...`) and is the
    dedupe key: the host keeps only the last update per resource between
    turns (spec: "if multiple updates are received before the next user
    message, Host SHOULD only send the last"). `content` /
    `structured_content` mirror the spec's `ui/update-model-context`
    params; at least one is set.
    """

    resource_uri: str
    content: Optional[List[Dict[str, Any]]] = None
    structured_content: Optional[Dict[str, Any]] = None


class CarriedSteerEntry(BaseModel):
    """A queued follow-up carried into the turn this request starts.

    Mid-turn steering's paused-turn path (docs/specs/mid-turn-steering.md).
    A turn paused for OAuth consent or tool approval has no running loop to
    steer, and the pause releases its lease — inbox and all — when the stream
    closes. The follow-ups the user typed meanwhile are still in their
    composer, so the resume request carries them here and the route seeds them
    onto the resumed turn's lease, where the ordinary ``SteeringHook`` picks
    them up at its first tool boundary.

    ``id`` is the client-minted queue-entry id, unchanged from the one the
    normal ``/sessions/{id}/steer`` path uses: it is what ``steering_applied``
    names back, and what makes carrying an entry idempotent against the
    composer's own end-of-turn flush.
    """

    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=STEER_QUEUE_MAX_CHARS)


class InvocationRequest(BaseModel):
    """Input for /invocations endpoint with multi-provider support"""

    session_id: str
    message: str = ""
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    system_prompt: Optional[str] = None
    caching_enabled: Optional[bool] = None
    enabled_tools: Optional[List[str]] = None  # User-specific tool preferences
    files: Optional[List[FileContent]] = None  # Direct file content (base64-encoded)
    file_upload_ids: Optional[List[str]] = None  # Upload IDs to resolve from S3
    provider: Optional[str] = None  # LLM provider: "bedrock", "openai", or "gemini"
    max_tokens: Optional[int] = None  # Maximum tokens to generate
    # Per-request canonical inference param overrides (temperature, top_p,
    # top_k, max_tokens, thinking, reasoning_effort, ...). Layered on top of
    # the managed model's admin defaults. Unsupported params are dropped
    # silently by the merge step in routes.py.
    inference_params: Optional[Dict[str, Any]] = None
    # NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
    # AgentCore Runtime's internal 'assistant_id' field handling.
    # AgentCore Runtime returns 424 when it sees a non-empty 'assistant_id' field,
    # likely trying to resolve it as an AWS Bedrock Agent ID.
    rag_assistant_id: Optional[str] = None
    # Marketplace D11: this turn was handed to the Agent by an `@`-mention in
    # the composer, rather than the whole conversation being bound to it.
    #
    # A mention borrows the Agent for ONE turn: the route skips the
    # bind-once-per-session validation (a mention is legitimate in a thread
    # that already has messages, and in a thread bound to a different Agent),
    # and skips writing `preferences.assistant_id`, so the next plain turn is
    # plain again. Everything else — access check, RAG, binding resolution,
    # memory injection — is identical to a bound turn, because the same Agent
    # is running with the same governance.
    #
    # ⚠️ It is the *client's* claim about intent, never an access decision:
    # `get_assistant_with_access_check` still gates the Agent itself, so the
    # worst a forged flag can do is decline to persist a binding.
    agent_mention: Optional[bool] = None
    # Marketplace D2: this turn is a marketplace **reviewer** test-driving a submission
    # before deciding on it. Two things change, and nothing else does.
    #
    # 1. The Agent resolves to the snapshot under review (`submittedVersion` while one is
    #    pending, `publishedVersion` otherwise) rather than to the published-or-draft rule
    #    every other caller gets. A reviewer who test-drove the author's live draft would
    #    be testing something approval is not going to publish.
    # 2. The PRIVATE access check is bypassed, because a PRIVATE Agent can be — and often
    #    is — sitting in the review queue, and `get_assistant_with_access_check` refuses a
    #    non-owner outright on one.
    #
    # ⚠️ Unlike `agent_mention`, this is NOT merely a claim about intent, so it cannot be
    # treated like one: it widens access. The route re-checks `admin.marketplace` against
    # the caller's own roles before honoring it, and a caller without the scope gets a 403
    # rather than a quietly-ignored flag — a silently downgraded preview would run the
    # wrong configuration and report it as the reviewed one.
    review_preview: Optional[bool] = None
    # When set, the route resumes a paused agent turn instead of starting a
    # new one. `message` is ignored in that case — the original prompt is
    # already in the agent's interrupt context.
    interrupt_responses: Optional[List[InterruptResponseEntry]] = None
    # Follow-ups the user queued while this session's turn was paused awaiting
    # consent or approval. Seeded onto this turn's steering inbox after the
    # lease is acquired, so the agent reads them at its next tool boundary
    # instead of the user having to send them as a separate turn that abandons
    # the pause. Ignored when mid-turn steering is disabled.
    steering: Optional[List[CarriedSteerEntry]] = None
    # When true, this is a "Continue" after a max_tokens truncation. Like a
    # resume, `message` is ignored: instead of synthesizing a new user turn,
    # the agent re-enters the loop with an empty prompt so the model
    # continues the truncated assistant message already in restored history
    # (assistant-prefill). Bypasses quota / RAG / file resolution like resume.
    continue_truncated: Optional[bool] = None
    # Legacy cache/marker dimension. Skills v2 retired the SkillAgent subclass:
    # both "chat" and "skill" build a ChatAgent. When the turn carries
    # accessible_skill_ids, ChatAgent adds the AgentSkills disclosure plugin. The
    # value is kept as a cache-key/resume-snapshot dimension; a turn with no
    # skills behaves as plain chat regardless.
    agent_type: Optional[str] = None
    # Per-turn selection of which accessible skills are active. None/absent =
    # all RBAC-accessible skills (back-compat with clients that predate the
    # skills picker). A list is intersected server-side with the accessible set
    # — client input can narrow the set, never grant. An empty (or fully
    # inaccessible) list yields zero skills, so the turn is plain chat.
    enabled_skills: Optional[List[str]] = None
    # User-selected custom system prompt ("conversation mode") for this
    # turn. The frontend forwards the active selection on every submit so
    # the inference path doesn't have to round-trip session metadata to
    # discover the choice — important on first-turn-of-a-new-session where
    # no metadata row exists yet. The resolver also persists this id back
    # to session preferences so the choice survives a refresh / new device.
    selected_prompt_id: Optional[str] = None
    # When set, this invocation is an app-initiated tools/call proxied from
    # an embedded MCP App (PR #5). `message` is ignored; no model turn runs.
    app_tool_call: Optional[AppToolCallEntry] = None
    # When set, this invocation pushes app-supplied model context onto the
    # conversation agent's state (PR #6, `ui/update-model-context`).
    # `message` is ignored; no model turn runs. The context is merged into
    # (and cleared before) the next real user turn's prompt.
    app_context_update: Optional[AppContextUpdateEntry] = None

    @field_validator("system_prompt")
    @classmethod
    def _bound_system_prompt_length(cls, value: Optional[str]) -> Optional[str]:
        """Reject user-supplied system prompts larger than the configured cap.

        The cap is also enforced inside ``SystemPromptBuilder.from_user_prompt``
        as defense in depth. Surfacing it at the request boundary lets us
        return a proper 4xx instead of silently truncating downstream.
        """
        if value is None:
            return value
        if len(value) > MAX_USER_SYSTEM_PROMPT_CHARS:
            raise ValueError(f"system_prompt exceeds maximum length of {MAX_USER_SYSTEM_PROMPT_CHARS} characters")
        return value


class InvocationResponse(BaseModel):
    """AgentCore Runtime standard response format"""

    output: Dict[str, Any]


class ChatRequest(BaseModel):
    """Chat request from client"""

    session_id: str
    message: str
    files: Optional[List[FileContent]] = None  # Direct file content (base64-encoded)
    file_upload_ids: Optional[List[str]] = None  # Upload IDs to resolve from S3
    enabled_tools: Optional[List[str]] = None  # User-specific tool preferences (tool IDs)
    assistant_id: Optional[str] = None  # Assistant ID for RAG-enabled chat


class ChatEvent(BaseModel):
    """SSE event sent to client"""

    type: str  # "text" | "tool_use" | "tool_result" | "error" | "complete"
    content: str
    metadata: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.model_dump(), ensure_ascii=False)


class SessionInfo(BaseModel):
    """Session information"""

    session_id: str
    message_count: int
    created_at: str
    updated_at: str


class GenerateTitleRequest(BaseModel):
    """Request to generate a conversation title"""

    session_id: str
    input: str  # Truncated user message (up to ~500 tokens)


class GenerateTitleResponse(BaseModel):
    """Response with generated conversation title"""

    title: str
    session_id: str
