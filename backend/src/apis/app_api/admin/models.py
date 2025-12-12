"""Admin API models."""

from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime


class UserInfo(BaseModel):
    """User information for admin endpoints."""
    email: str
    user_id: str
    name: str
    roles: List[str]
    picture: Optional[str] = None


class AllSessionsResponse(BaseModel):
    """Response model for listing all sessions (admin only)."""
    sessions: List[dict]
    total_count: int
    next_token: Optional[str] = None


class SessionDeleteResponse(BaseModel):
    """Response model for deleting a session."""
    success: bool
    session_id: str
    message: str


class SystemStatsResponse(BaseModel):
    """Response model for system statistics."""
    total_users: int
    total_sessions: int
    active_sessions: int
    total_messages: int
    stats_as_of: datetime = Field(default_factory=datetime.utcnow)


class FoundationModelSummary(BaseModel):
    """Summary information for a Bedrock foundation model."""
    model_config = ConfigDict(populate_by_name=True)
    
    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider_name: str = Field(..., alias="providerName")
    input_modalities: List[str] = Field(default_factory=list, alias="inputModalities")
    output_modalities: List[str] = Field(default_factory=list, alias="outputModalities")
    response_streaming_supported: bool = Field(default=False, alias="responseStreamingSupported")
    customizations_supported: List[str] = Field(default_factory=list, alias="customizationsSupported")
    inference_types_supported: List[str] = Field(default_factory=list, alias="inferenceTypesSupported")
    model_lifecycle: Optional[str] = Field(None, alias="modelLifecycle")


class BedrockModelsResponse(BaseModel):
    """Response model for listing Bedrock foundation models."""
    models: List[FoundationModelSummary]
    next_token: Optional[str] = Field(None, alias="nextToken")
    total_count: Optional[int] = Field(None, alias="totalCount")


class GeminiModelSummary(BaseModel):
    """Summary information for a Gemini model."""
    model_config = ConfigDict(populate_by_name=True)

    name: str
    base_model_id: Optional[str] = Field(None, alias="baseModelId")
    version: Optional[str] = None
    display_name: str = Field(..., alias="displayName")
    description: Optional[str] = None
    input_token_limit: Optional[int] = Field(None, alias="inputTokenLimit")
    output_token_limit: Optional[int] = Field(None, alias="outputTokenLimit")
    supported_generation_methods: List[str] = Field(default_factory=list, alias="supportedGenerationMethods")
    thinking: Optional[bool] = None
    temperature: Optional[float] = None
    max_temperature: Optional[float] = Field(None, alias="maxTemperature")
    top_p: Optional[float] = Field(None, alias="topP")
    top_k: Optional[int] = Field(None, alias="topK")


class GeminiModelsResponse(BaseModel):
    """Response model for listing Gemini models."""
    models: List[GeminiModelSummary]
    total_count: int = Field(..., alias="totalCount")


class OpenAIModelSummary(BaseModel):
    """Summary information for an OpenAI model."""
    model_config = ConfigDict(populate_by_name=True)

    id: str
    created: Optional[int] = None
    owned_by: str = Field(..., alias="ownedBy")
    object: Optional[str] = None


class OpenAIModelsResponse(BaseModel):
    """Response model for listing OpenAI models."""
    models: List[OpenAIModelSummary]
    total_count: int = Field(..., alias="totalCount")
