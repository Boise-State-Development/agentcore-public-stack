"""Pydantic models for system settings and first-boot flow."""

from typing import Optional

from pydantic import BaseModel, Field


class FirstBootRequest(BaseModel):
    """Request body for the first-boot admin registration endpoint."""

    username: str = Field(..., min_length=3, max_length=128)
    email: str = Field(..., pattern=r"^[^@]+@[^@]+\.[^@]+$")
    password: str = Field(..., min_length=8)


class FirstBootResponse(BaseModel):
    """Response body for a successful first-boot registration."""

    success: bool
    user_id: str
    message: str


class SystemStatusResponse(BaseModel):
    """Response body for the system status endpoint."""

    first_boot_completed: bool


class LoginPageSettingsUpdate(BaseModel):
    """Request body for updating the login-page settings."""

    hide_cognito_login: bool = Field(
        ..., description="Hide the Cognito sign-in button on the login page"
    )


class LoginPageSettingsResponse(BaseModel):
    """The saved login-page settings and how they resolve right now."""

    hide_cognito_login: bool = Field(
        ..., description="What the admin asked for"
    )
    cognito_login_visible: bool = Field(
        ...,
        description=(
            "Whether the login page shows the Cognito button. True whenever no "
            "federated provider is enabled, whatever hide_cognito_login says."
        ),
    )
    enabled_provider_count: int
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
