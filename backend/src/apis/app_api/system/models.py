"""Pydantic models for system settings and first-boot flow."""

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
