"""Request/response models for the render-token endpoint."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RenderTokenRequest(BaseModel):
    version: int = Field(..., ge=1, description="Artifact version to render")
    session_id: Optional[str] = Field(
        default=None,
        validation_alias="sessionId",
        description="Originating chat session id — audit correlation only",
    )


class RenderTokenResponse(BaseModel):
    url: str = Field(
        ...,
        description="Artifact origin URL with the embedded render token "
        "(set directly as the iframe src)",
    )
    expires_at: str = Field(..., description="ISO-8601 UTC token expiry")
