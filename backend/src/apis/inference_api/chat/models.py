"""Chat feature models

Contains Pydantic models for chat API requests and responses.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator

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
    # When set, the route resumes a paused agent turn instead of starting a
    # new one. `message` is ignored in that case — the original prompt is
    # already in the agent's interrupt context.
    interrupt_responses: Optional[List[InterruptResponseEntry]] = None
    # When true, this is a "Continue" after a max_tokens truncation. Like a
    # resume, `message` is ignored: instead of synthesizing a new user turn,
    # the agent re-enters the loop with an empty prompt so the model
    # continues the truncated assistant message already in restored history
    # (assistant-prefill). Bypasses quota / RAG / file resolution like resume.
    continue_truncated: Optional[bool] = None
    # Selects which agent factory variant builds the turn. When omitted, the
    # server applies its default (PR-7: "skill" — admin-configurable via the
    # chat-mode policy, see routes.py), routing through SkillAgent's progressive
    # disclosure; a user with no granted skills degrades to plain ChatAgent
    # behavior. Pass "chat" to opt out of the skill path for a turn — honored
    # only while the admin policy allows mode toggling.
    agent_type: Optional[str] = None
    # Per-turn selection of which accessible skills are active (skill agent
    # path only). None/absent = all RBAC-accessible skills (back-compat with
    # clients that predate the skills picker). A list is intersected
    # server-side with the accessible set — client input can narrow the set,
    # never grant. An empty (or fully inaccessible) list yields zero skills,
    # so the SkillAgent degrades to plain chat behavior for the turn.
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


# ---------------------------------------------------------------------------
# API Converse models (direct Bedrock Converse API via API key auth)
# ---------------------------------------------------------------------------


class ConverseMessage(BaseModel):
    """A single message in the conversation."""

    role: str  # "user" or "assistant"
    content: str


class ConverseRequest(BaseModel):
    """Request model for /chat/api-converse endpoint.

    Supports both single-shot and multi-turn conversations.
    """

    model_id: str  # Bedrock model ID (e.g. "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    messages: List[ConverseMessage]
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = 4096
    stream: bool = False  # Whether to stream the response via SSE
    top_p: Optional[float] = None


class ConverseResponse(BaseModel):
    """Non-streaming response from /chat/api-converse."""

    role: str = "assistant"
    content: str
    model_id: str
    usage: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None
    reasoning: Optional[str] = None  # Populated for reasoning models
