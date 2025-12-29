"""Sessions API request/response models"""

from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict


class SessionPreferences(BaseModel):
    """User preferences for a session"""
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    last_model: Optional[str] = Field(None, alias="lastModel", description="Last model used in this session")
    last_temperature: Optional[float] = Field(None, alias="lastTemperature", description="Last temperature setting used")
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools", description="List of enabled tool names")
    selected_prompt_id: Optional[str] = Field(None, alias="selectedPromptId", description="ID of selected prompt template")
    custom_prompt_text: Optional[str] = Field(None, alias="customPromptText", description="Custom prompt text if used")

    # System prompt hash for tracking exact prompt version sent to the model
    # This is a hash of the FINAL rendered system prompt (after date injection, variable substitution, etc.)
    # Use cases:
    # - Track which exact prompt was used for each session
    # - Correlate prompt changes with model performance/cost metrics
    # - Detect when two sessions used identical prompts even if they selected different templates
    # - Enable prompt A/B testing and version tracking
    system_prompt_hash: Optional[str] = Field(None, alias="systemPromptHash", description="MD5 hash of final rendered system prompt")


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
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    session_id: str = Field(..., alias="sessionId", description="Session identifier")
    user_id: str = Field(..., alias="userId", description="User identifier")
    title: str = Field(..., description="Session title (usually from first message)")
    status: Literal['active', 'archived', 'deleted'] = Field(..., description="Session status")
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
    status: Optional[Literal['active', 'archived', 'deleted']] = Field(None, description="Session status")
    starred: Optional[bool] = Field(None, description="Whether session is starred")
    tags: Optional[List[str]] = Field(None, description="Custom tags")
    last_model: Optional[str] = Field(None, alias="lastModel", description="Last model used")
    last_temperature: Optional[float] = Field(None, alias="lastTemperature", description="Last temperature setting")
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools", description="Enabled tools list")
    selected_prompt_id: Optional[str] = Field(None, alias="selectedPromptId", description="Selected prompt ID")
    custom_prompt_text: Optional[str] = Field(None, alias="customPromptText", description="Custom prompt text")
    system_prompt_hash: Optional[str] = Field(None, alias="systemPromptHash", description="MD5 hash of final rendered system prompt")


class SessionMetadataResponse(BaseModel):
    """Response containing session metadata"""
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId", description="Session identifier")
    title: str = Field(..., description="Session title")
    status: Literal['active', 'archived', 'deleted'] = Field(..., description="Session status")
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

    session_ids: List[str] = Field(
        ...,
        alias="sessionIds",
        description="List of session IDs to delete",
        min_length=1,
        max_length=20
    )


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
