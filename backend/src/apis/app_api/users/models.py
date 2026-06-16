"""User search models for sharing functionality."""

from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class UserSearchResult(BaseModel):
    """User search result for sharing modal."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId", description="User identifier")
    email: str = Field(..., description="User email address")
    name: str = Field(..., description="User display name")


class UserSearchResponse(BaseModel):
    """Response containing user search results."""

    model_config = ConfigDict(populate_by_name=True)

    users: List[UserSearchResult] = Field(..., description="List of matching users")


class UserPermissionsResponse(BaseModel):
    """Response model for user effective permissions resolved from AppRoles."""

    model_config = ConfigDict(populate_by_name=True)

    app_roles: List[str] = Field(..., alias="appRoles", description="Resolved application roles")
    tools: List[str] = Field(..., description="Accessible tool IDs")
    models: List[str] = Field(..., description="Accessible model IDs")
    quota_tier: Optional[str] = Field(None, alias="quotaTier", description="Assigned quota tier")
    resolved_at: str = Field(..., alias="resolvedAt", description="ISO timestamp of resolution")


class UserProfileSyncRequest(BaseModel):
    """Request to sync user profile from the frontend ID token.

    Identity-display fields only. Authorization-relevant fields
    (``roles``, ``email``) are deliberately not accepted here — they
    flow from the IdP via the BFF token-exchange path and the validated
    JWT, never from a client-controlled request body.

    ``extra="allow"`` keeps the endpoint compatible with legacy clients
    that still send dropped fields like ``roles`` or ``email``: the
    extras are accepted, exposed via ``model_extra``, and ignored when
    building the persisted profile. The route handler logs a warning
    when it sees them so stale clients can be chased down.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str = Field("", description="User display name from ID token")
    picture: Optional[str] = Field(None, description="Profile picture URL from ID token")
    provider_sub: Optional[str] = Field(None, alias="provider_sub", description="IdP user identifier from ID token")
