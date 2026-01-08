"""Assistants API request/response models"""

from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict


class Assistant(BaseModel):
    """Complete assistant model (internal use)"""
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    assistant_id: str = Field(..., alias="assistantId", description="Assistant identifier")
    owner_id: str = Field(..., alias="ownerId", description="User/owner identifier (internal, not returned in responses)")
    owner_name: str = Field(..., alias="ownerName", description="Owner display name (public)")
    name: str = Field(..., description="Assistant display name")
    description: str = Field(..., description="Short summary for UI cards")
    instructions: str = Field(..., description="System prompt for the assistant")
    vector_index_id: str = Field(..., alias="vectorIndexId", description="S3 vector index name")
    visibility: Literal['PRIVATE', 'PUBLIC', 'SHARED'] = Field(..., description="Access control level")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")
    usage_count: int = Field(0, alias="usageCount", description="Number of times used")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 timestamp of last update")
    status: Literal['DRAFT', 'COMPLETE', 'ARCHIVED'] = Field(..., description="Assistant lifecycle status")


class CreateAssistantDraftRequest(BaseModel):
    """Request body for creating a draft assistant (minimal fields)"""
    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, description="Assistant name (defaults to 'Untitled Assistant')")


class CreateAssistantRequest(BaseModel):
    """Request body for creating a complete assistant"""
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Assistant display name")
    description: str = Field(..., description="Short summary")
    instructions: str = Field(..., description="System prompt")
    visibility: Literal['PRIVATE', 'PUBLIC', 'SHARED'] = Field('PRIVATE', description="Access control")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")


class UpdateAssistantRequest(BaseModel):
    """Request body for updating an assistant (all fields optional)"""
    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, description="Assistant display name")
    description: Optional[str] = Field(None, description="Short summary")
    instructions: Optional[str] = Field(None, description="System prompt")
    visibility: Optional[Literal['PRIVATE', 'PUBLIC', 'SHARED']] = Field(None, description="Access control")
    tags: Optional[List[str]] = Field(None, description="Search keywords")
    status: Optional[Literal['DRAFT', 'COMPLETE', 'ARCHIVED']] = Field(None, description="Lifecycle status")


class AssistantResponse(BaseModel):
    """Response containing assistant data (owner_id excluded for privacy)"""
    model_config = ConfigDict(populate_by_name=True)

    assistant_id: str = Field(..., alias="assistantId", description="Assistant identifier")
    owner_name: str = Field(..., alias="ownerName", description="Owner display name")
    name: str = Field(..., description="Assistant display name")
    description: str = Field(..., description="Short summary")
    instructions: str = Field(..., description="System prompt")
    vector_index_id: str = Field(..., alias="vectorIndexId", description="S3 vector index name")
    visibility: Literal['PRIVATE', 'PUBLIC', 'SHARED'] = Field(..., description="Access control")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")
    usage_count: int = Field(..., alias="usageCount", description="Usage count")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 creation timestamp")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 update timestamp")
    status: Literal['DRAFT', 'COMPLETE', 'ARCHIVED'] = Field(..., description="Lifecycle status")


class AssistantsListResponse(BaseModel):
    """Response for listing assistants with pagination support"""
    model_config = ConfigDict(populate_by_name=True)

    assistants: List[AssistantResponse] = Field(..., description="List of assistants for the user")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for next page")


class AssistantTestChatRequest(BaseModel):
    """Request body for testing assistant chat with RAG"""
    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(..., description="User message to test")
    session_id: Optional[str] = Field(None, description="Optional session ID for ephemeral chat")

