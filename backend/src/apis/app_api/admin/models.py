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


# =============================================================================
# Managed Models (Model Management)
# =============================================================================

class ManagedModelCreate(BaseModel):
    """Request model for creating a managed model."""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId", min_length=1)
    model_name: str = Field(..., alias="modelName", min_length=1)
    provider: str = Field(..., min_length=1)
    provider_name: str = Field(..., alias="providerName", min_length=1)
    input_modalities: List[str] = Field(..., alias="inputModalities", min_length=1)
    output_modalities: List[str] = Field(..., alias="outputModalities", min_length=1)
    max_input_tokens: int = Field(..., alias="maxInputTokens", ge=1)
    max_output_tokens: int = Field(..., alias="maxOutputTokens", ge=1)
    available_to_roles: List[str] = Field(..., alias="availableToRoles", min_length=1)
    enabled: bool = True
    input_price_per_million_tokens: float = Field(..., alias="inputPricePerMillionTokens", ge=0)
    output_price_per_million_tokens: float = Field(..., alias="outputPricePerMillionTokens", ge=0)
    is_reasoning_model: bool = Field(False, alias="isReasoningModel")
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")


class ManagedModelUpdate(BaseModel):
    """Request model for updating a managed model."""
    model_config = ConfigDict(populate_by_name=True)

    model_id: Optional[str] = Field(None, alias="modelId", min_length=1)
    model_name: Optional[str] = Field(None, alias="modelName")
    provider: Optional[str] = None
    provider_name: Optional[str] = Field(None, alias="providerName")
    input_modalities: Optional[List[str]] = Field(None, alias="inputModalities")
    output_modalities: Optional[List[str]] = Field(None, alias="outputModalities")
    max_input_tokens: Optional[int] = Field(None, alias="maxInputTokens", ge=1)
    max_output_tokens: Optional[int] = Field(None, alias="maxOutputTokens", ge=1)
    available_to_roles: Optional[List[str]] = Field(None, alias="availableToRoles")
    enabled: Optional[bool] = None
    input_price_per_million_tokens: Optional[float] = Field(None, alias="inputPricePerMillionTokens", ge=0)
    output_price_per_million_tokens: Optional[float] = Field(None, alias="outputPricePerMillionTokens", ge=0)
    is_reasoning_model: Optional[bool] = Field(None, alias="isReasoningModel")
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")


class ManagedModel(BaseModel):
    """Managed model with full details."""
    model_config = ConfigDict(populate_by_name=True)

    id: str
    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str
    provider_name: str = Field(..., alias="providerName")
    input_modalities: List[str] = Field(..., alias="inputModalities")
    output_modalities: List[str] = Field(..., alias="outputModalities")
    max_input_tokens: int = Field(..., alias="maxInputTokens")
    max_output_tokens: int = Field(..., alias="maxOutputTokens")
    available_to_roles: List[str] = Field(..., alias="availableToRoles")
    enabled: bool
    input_price_per_million_tokens: float = Field(..., alias="inputPricePerMillionTokens")
    output_price_per_million_tokens: float = Field(..., alias="outputPricePerMillionTokens")
    is_reasoning_model: bool = Field(..., alias="isReasoningModel")
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")


class ManagedModelsListResponse(BaseModel):
    """Response model for listing managed models."""
    model_config = ConfigDict(populate_by_name=True)

    models: List[ManagedModel]
    total_count: int = Field(..., alias="totalCount")
