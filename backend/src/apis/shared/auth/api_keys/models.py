"""API Key request/response models."""

from typing import Optional

from pydantic import BaseModel, Field


class CreateApiKeyRequest(BaseModel):
    """Request model for creating a new API key."""

    name: str = Field(
        ..., min_length=1, max_length=128, description="Human-readable name for the key"
    )


class CreateApiKeyResponse(BaseModel):
    """Response model for key creation. The raw key is only returned once."""

    key_id: str = Field(..., description="Unique key identifier (use for delete/lookup)")
    name: str = Field(..., description="Human-readable name")
    key: str = Field(..., description="The raw API key — store it now, it won't be shown again")
    created_at: str = Field(..., description="ISO-8601 creation timestamp")
    expires_at: str = Field(..., description="ISO-8601 expiration timestamp")


class ApiKeyInfo(BaseModel):
    """Public metadata for an API key (never includes the raw key)."""

    key_id: str = Field(..., description="Unique key identifier")
    name: str = Field(..., description="Human-readable name")
    created_at: str = Field(..., description="ISO-8601 creation timestamp")
    expires_at: str = Field(..., description="ISO-8601 expiration timestamp")
    last_used_at: Optional[str] = Field(None, description="ISO-8601 last usage timestamp")


class GetApiKeyResponse(BaseModel):
    """Response model for getting the user's API key."""

    key: Optional[ApiKeyInfo] = Field(None, description="The user's API key, or null if none exists")


class DeleteApiKeyResponse(BaseModel):
    """Response model for key deletion."""

    key_id: str = Field(..., description="ID of the deleted key")
    deleted: bool = Field(default=True, description="Whether the key was deleted")


class ValidatedApiKey(BaseModel):
    """Result of a successful API key validation. Used internally."""

    key_id: str = Field(..., description="Unique key identifier")
    user_id: str = Field(..., description="Owner's user ID")
    name: str = Field(..., description="Human-readable key name")
