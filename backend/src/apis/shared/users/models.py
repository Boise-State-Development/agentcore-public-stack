"""Domain models for user management system."""

from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional
from enum import Enum


class UserStatus(str, Enum):
    """User account status"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class UserProfile(BaseModel):
    """User profile stored in DynamoDB, synced from JWT claims."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    roles: List[str] = Field(default_factory=list)
    picture: Optional[str] = None
    email_domain: str = Field(..., alias="emailDomain")
    created_at: str = Field(..., alias="createdAt")
    last_login_at: str = Field(..., alias="lastLoginAt")
    status: UserStatus = Field(default=UserStatus.ACTIVE)

    @field_validator('email', mode='before')
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        """Store email as lowercase for case-insensitive matching."""
        return v.lower() if v else v

    @field_validator('email_domain', mode='before')
    @classmethod
    def lowercase_domain(cls, v: str) -> str:
        """Store domain as lowercase for case-insensitive matching."""
        return v.lower() if v else v

    @field_validator('status', mode='before')
    @classmethod
    def coerce_status(cls, v) -> UserStatus:
        """Convert string to UserStatus enum."""
        if isinstance(v, UserStatus):
            return v
        if isinstance(v, str):
            return UserStatus(v.lower())
        return v


class UserListItem(BaseModel):
    """Minimal user info for list views (GSI projections)."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    status: UserStatus = Field(default=UserStatus.ACTIVE)
    last_login_at: str = Field(..., alias="lastLoginAt")
    email_domain: Optional[str] = Field(None, alias="emailDomain")

    @field_validator('status', mode='before')
    @classmethod
    def coerce_status(cls, v) -> UserStatus:
        """Convert string to UserStatus enum."""
        if isinstance(v, UserStatus):
            return v
        if isinstance(v, str):
            return UserStatus(v.lower())
        return v
