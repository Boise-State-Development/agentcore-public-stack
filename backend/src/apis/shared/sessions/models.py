"""Sessions API request/response models

This module contains all session-related data models including:
- Session metadata models
- Message models (Message, MessageContent, MessageResponse, etc.)
- Session preferences and configuration
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class VisualDisplayState(BaseModel):
    """Display state for a single promoted visual (inline tool result)"""

    model_config = ConfigDict(populate_by_name=True)

    dismissed: bool = Field(default=False, description="User dismissed this visual")
    expanded: bool = Field(default=True, description="Visual is expanded vs collapsed")


class SessionPreferences(BaseModel):
    """User preferences for a session"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")
    last_model: Optional[str] = Field(default=None, alias="lastModel", description="Last model used in this session")
    last_temperature: Optional[float] = Field(default=None, alias="lastTemperature", description="Last temperature setting used")
    enabled_tools: Optional[List[str]] = Field(default=None, alias="enabledTools", description="List of enabled tool names")
    selected_prompt_id: Optional[str] = Field(default=None, alias="selectedPromptId", description="ID of selected prompt template")
    custom_prompt_text: Optional[str] = Field(default=None, alias="customPromptText", description="Custom prompt text if used")
    assistant_id: Optional[str] = Field(default=None, alias="assistantId", description="Assistant ID attached to this session")

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


class UpdateSessionMetadataRequest(BaseModel):
    """Request body for updating session metadata"""

    model_config = ConfigDict(populate_by_name=True)
    title: Optional[str] = Field(None, description="Session title")
    status: Optional[Literal["active", "archived", "deleted"]] = Field(None, description="Session status")
    starred: Optional[bool] = Field(None, description="Whether session is starred")
    tags: Optional[List[str]] = Field(None, description="Custom tags")
    last_model: Optional[str] = Field(None, alias="lastModel", description="Last model used")
    last_temperature: Optional[float] = Field(None, alias="lastTemperature", description="Last temperature setting")
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools", description="Enabled tools list")
    selected_prompt_id: Optional[str] = Field(None, alias="selectedPromptId", description="Selected prompt ID")
    custom_prompt_text: Optional[str] = Field(None, alias="customPromptText", description="Custom prompt text")
    system_prompt_hash: Optional[str] = Field(None, alias="systemPromptHash", description="MD5 hash of final rendered system prompt")
    assistant_id: Optional[str] = Field(None, alias="assistantId", description="Assistant ID attached to this session")


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
    """Latency measurements in milliseconds"""

    model_config = ConfigDict(populate_by_name=True)

    time_to_first_token: int = Field(..., alias="timeToFirstToken", description="Time from request start to first token received (ms)")
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


class MessageMetadata(BaseModel):
    """Metadata associated with a single message"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    latency: Optional[LatencyMetrics] = Field(None, description="Latency measurements")
    token_usage: Optional[TokenUsage] = Field(None, alias="tokenUsage", description="Token usage statistics")
    model_info: Optional[ModelInfo] = Field(None, alias="modelInfo", description="Model information for cost tracking")
    attribution: Optional[Attribution] = Field(None, description="Attribution for cost tracking and billing")
    cost: Optional[float] = Field(None, description="Total cost in USD for this message (computed from token usage and pricing)")
    citations: Optional[List[Dict[str, str]]] = Field(None, description="RAG citations for this message (stored as dicts for flexible JSON storage)")
    display_text: Optional[str] = Field(None, alias="displayText", description="Original user message text before RAG augmentation (for clean UI display)")
    # Note: Feedback will be added in future implementation
    # feedback: Optional[Feedback] = None


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
