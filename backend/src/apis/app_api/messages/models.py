"""Messages API models"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


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
