"""Chat feature models

Contains Pydantic models for chat API requests and responses.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


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
    # Selects which agent factory variant builds the turn. Defaults to "chat"
    # (MainAgent / ChatAgent) when omitted, so existing clients are unaffected.
    # Pass "skill" to route through SkillAgent's progressive skill disclosure.
    agent_type: Optional[str] = None
    # When set, this invocation is an app-initiated tools/call proxied from
    # an embedded MCP App (PR #5). `message` is ignored; no model turn runs.
    app_tool_call: Optional[AppToolCallEntry] = None
    # When set, this invocation pushes app-supplied model context onto the
    # conversation agent's state (PR #6, `ui/update-model-context`).
    # `message` is ignored; no model turn runs. The context is merged into
    # (and cleared before) the next real user turn's prompt.
    app_context_update: Optional[AppContextUpdateEntry] = None


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
