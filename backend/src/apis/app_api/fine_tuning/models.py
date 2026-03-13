"""Pydantic models for fine-tuning access control and quota."""

from pydantic import BaseModel, Field
from typing import Optional


class FineTuningAccessGrant(BaseModel):
    """DynamoDB item shape for a fine-tuning access grant."""
    email: str
    granted_by: str
    granted_at: str
    monthly_quota_hours: float = Field(default=10.0)
    current_month_usage_hours: float = Field(default=0.0)
    quota_period: str = Field(description="YYYY-MM format for lazy reset detection")


class FineTuningAccessResponse(BaseModel):
    """User-facing response for access check."""
    has_access: bool
    monthly_quota_hours: Optional[float] = None
    current_month_usage_hours: Optional[float] = None
    quota_period: Optional[str] = None


class QuotaCheckResult(BaseModel):
    """Internal result of a quota check before job creation."""
    allowed: bool
    remaining_hours: float
    message: str
