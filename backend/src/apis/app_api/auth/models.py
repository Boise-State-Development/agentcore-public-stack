"""Authentication models."""

from dataclasses import dataclass
from typing import List, Optional
from pydantic import BaseModel, Field


@dataclass
class User:
    """Authenticated user model."""
    email: str
    empl_id: str
    name: str
    roles: List[str]
    picture: Optional[str] = None


class TokenExchangeRequest(BaseModel):
    """Request model for token exchange endpoint."""
    code: str = Field(..., description="Authorization code from Entra ID")
    state: str = Field(..., description="State token for CSRF protection")
    redirect_uri: Optional[str] = Field(None, description="Redirect URI (must match authorization request)")


class TokenExchangeResponse(BaseModel):
    """Response model for token exchange endpoint."""
    access_token: str = Field(..., description="JWT access token")
    refresh_token: str = Field(..., description="Refresh token for obtaining new access tokens")
    id_token: Optional[str] = Field(None, description="ID token containing user information")
    token_type: str = Field(default="Bearer", description="Token type")
    expires_in: int = Field(..., description="Access token expiration time in seconds")
    scope: str = Field(..., description="Token scopes")


class TokenRefreshRequest(BaseModel):
    """Request model for token refresh endpoint."""
    refresh_token: str = Field(..., description="Refresh token from previous authentication")


class TokenRefreshResponse(BaseModel):
    """Response model for token refresh endpoint."""
    access_token: str = Field(..., description="New JWT access token")
    refresh_token: str = Field(..., description="New refresh token (may be same as input)")
    id_token: Optional[str] = Field(None, description="New ID token containing user information")
    token_type: str = Field(default="Bearer", description="Token type")
    expires_in: int = Field(..., description="Access token expiration time in seconds")
    scope: str = Field(..., description="Token scopes")


class LoginResponse(BaseModel):
    """Response model for login endpoint."""
    authorization_url: str = Field(..., description="URL to redirect user to for authentication")
    state: str = Field(..., description="State token for CSRF protection (should be validated on callback)")

