"""Sessions API request/response models"""

from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict


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


class SessionMetadataResponse(BaseModel):
    """Response containing session metadata"""
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId", description="Session identifier")
    user_id: str = Field(..., alias="userId", description="User identifier")
    title: str = Field(..., description="Session title")
    status: Literal['active', 'archived', 'deleted'] = Field(..., description="Session status")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    last_message_at: str = Field(..., alias="lastMessageAt", description="ISO 8601 timestamp of last message")
    message_count: int = Field(..., alias="messageCount", description="Total message count")
    starred: Optional[bool] = Field(False, description="Whether starred")
    tags: Optional[List[str]] = Field(default_factory=list, description="Custom tags")
    preferences: Optional[dict] = Field(None, description="Session preferences")
