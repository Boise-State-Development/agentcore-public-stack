"""Messages API models"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class MessageContent(BaseModel):
    """Individual content block in a message"""
    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(..., description="Content type (text, toolUse, toolResult, etc.)")
    text: Optional[str] = Field(None, description="Text content")
    # Add other fields as needed for different content types
    tool_use: Optional[Dict[str, Any]] = Field(None, alias="toolUse")
    tool_result: Optional[Dict[str, Any]] = Field(None, alias="toolResult")
    image: Optional[Dict[str, Any]] = Field(None)
    document: Optional[Dict[str, Any]] = Field(None)


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
        None,
        alias="cacheWritePricePerMtok",
        description="Price per million cache write tokens (USD) - Bedrock only"
    )
    cache_read_price_per_mtok: Optional[float] = Field(
        None,
        alias="cacheReadPricePerMtok",
        description="Price per million cache read tokens (USD) - Bedrock only"
    )
    currency: str = Field(default="USD", description="Currency code")
    snapshot_at: str = Field(..., alias="snapshotAt", description="ISO timestamp when pricing was captured")


class ModelInfo(BaseModel):
    """Model information for cost calculation and tracking"""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId", description="Full model identifier (e.g., anthropic.claude-3-5-sonnet-20241022-v2:0)")
    model_name: str = Field(..., alias="modelName", description="Human-readable model name (e.g., Claude 3.5 Sonnet)")
    model_version: Optional[str] = Field(None, alias="modelVersion", description="Model version (e.g., v2)")
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


class MessageMetadata(BaseModel):
    """Metadata associated with a single message"""
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    latency: Optional[LatencyMetrics] = Field(None, description="Latency measurements")
    token_usage: Optional[TokenUsage] = Field(None, alias="tokenUsage", description="Token usage statistics")
    model_info: Optional[ModelInfo] = Field(None, alias="modelInfo", description="Model information for cost tracking")
    attribution: Optional[Attribution] = Field(None, description="Attribution for cost tracking and billing")
    cost: Optional[float] = Field(
        None,
        description="Total cost in USD for this message (computed from token usage and pricing)"
    )
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
    role: Literal['user', 'assistant', 'system'] = Field(..., description="Role of the message sender")
    content: List[MessageContent] = Field(..., description="List of content blocks in the message")
    created_at: str = Field(..., alias="createdAt", description="ISO timestamp when the message was created")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional metadata associated with the message")


class MessagesListResponse(BaseModel):
    """Response for listing messages with pagination support"""
    model_config = ConfigDict(populate_by_name=True)

    messages: List[MessageResponse] = Field(..., description="List of messages in the session")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for retrieving the next page of results")
