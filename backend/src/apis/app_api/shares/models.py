"""Share API request/response models

This module contains all share-related data models including:
- CreateShareRequest / UpdateShareRequest for share operations
- ShareResponse for share metadata
- SharedConversationResponse for full shared conversation data
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apis.shared.sessions.models import MessageResponse


class CreateShareRequest(BaseModel):
    """Request body for creating a share"""

    model_config = ConfigDict(populate_by_name=True)

    access_level: Literal["public", "specific"] = Field(
        ..., alias="accessLevel", description="Access level for the share"
    )
    allowed_emails: Optional[List[str]] = Field(
        default=None,
        alias="allowedEmails",
        description="Email addresses allowed to view (required when accessLevel is 'specific')",
    )

    @model_validator(mode="after")
    def validate_allowed_emails(self) -> "CreateShareRequest":
        if self.access_level == "specific" and (
            not self.allowed_emails or len(self.allowed_emails) == 0
        ):
            raise ValueError(
                "allowed_emails is required when access_level is 'specific'"
            )
        return self


class UpdateShareRequest(BaseModel):
    """Request body for updating share settings"""

    model_config = ConfigDict(populate_by_name=True)

    access_level: Optional[Literal["public", "specific"]] = Field(
        default=None, alias="accessLevel", description="New access level for the share"
    )
    allowed_emails: Optional[List[str]] = Field(
        default=None,
        alias="allowedEmails",
        description="Updated email addresses allowed to view",
    )

    @model_validator(mode="after")
    def validate_allowed_emails(self) -> "UpdateShareRequest":
        if self.access_level == "specific" and (
            not self.allowed_emails or len(self.allowed_emails) == 0
        ):
            raise ValueError(
                "allowed_emails is required when access_level is 'specific'"
            )
        return self


class ShareResponse(BaseModel):
    """Response model for share operations"""

    model_config = ConfigDict(populate_by_name=True)

    share_id: str = Field(..., alias="shareId", description="Unique share identifier")
    session_id: str = Field(..., alias="sessionId", description="Original session identifier")
    owner_id: str = Field(..., alias="ownerId", description="User ID of the share creator")
    access_level: Literal["public", "specific"] = Field(
        ..., alias="accessLevel", description="Access level for the share"
    )
    allowed_emails: Optional[List[str]] = Field(
        default=None, alias="allowedEmails", description="Allowed email addresses"
    )
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of share creation")
    share_url: str = Field(..., alias="shareUrl", description="Shareable URL for the conversation")


class ShareListResponse(BaseModel):
    """Response model for listing all shares for a session"""

    model_config = ConfigDict(populate_by_name=True)

    shares: List[ShareResponse] = Field(..., description="List of shares for the session")


class SharedConversationResponse(BaseModel):
    """Response model for retrieving a shared conversation"""

    model_config = ConfigDict(populate_by_name=True)

    share_id: str = Field(..., alias="shareId", description="Unique share identifier")
    title: str = Field(..., description="Conversation title")
    access_level: Literal["public", "specific"] = Field(
        ..., alias="accessLevel", description="Access level for the share"
    )
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of share creation")
    owner_id: str = Field(..., alias="ownerId", description="User ID of the share creator")
    messages: List[MessageResponse] = Field(..., description="Snapshot of conversation messages")
