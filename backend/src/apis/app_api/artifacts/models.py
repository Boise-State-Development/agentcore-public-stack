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


class ArtifactSummary(BaseModel):
    """One artifact's current HEAD, for the session artifacts list.

    Snake-case JSON to match this domain's existing REST shape
    (RenderTokenResponse.expires_at). The SPA normalizes both this and
    the camelCase live SSE `artifact` event into one client model.
    """

    artifact_id: str
    version: int
    title: str
    content_type: str
    updated_at: str
    created_at: Optional[str] = None


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
