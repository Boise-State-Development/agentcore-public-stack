"""Pydantic models for fine-tuning access control and quota.

The quota is denominated in **US dollars**, not GPU-hours.  Hours were a
proxy that stopped tracking the thing being budgeted the moment more than one
instance type was offered: ten hours buys about $14 on an ml.g5.xlarge and
roughly $450 on an ml.g6e.24xlarge.  Grants written against the old field are
migrated lazily on read — see ``repository.FineTuningAccessRepository``.
"""

from typing import Optional

from pydantic import BaseModel, Field


class FineTuningAccessGrant(BaseModel):
    """DynamoDB item shape for a fine-tuning access grant."""
    email: str
    granted_by: str
    granted_at: str
    monthly_quota_usd: float = Field(default=15.0)
    current_month_usage_usd: float = Field(default=0.0)
    quota_period: str = Field(description="YYYY-MM format for lazy reset detection")


class FineTuningAccessResponse(BaseModel):
    """User-facing response for access check."""
    has_access: bool
    monthly_quota_usd: Optional[float] = None
    current_month_usage_usd: Optional[float] = None
    quota_period: Optional[str] = None


class QuotaCheckResult(BaseModel):
    """Internal result of a quota check before job creation."""
    allowed: bool
    remaining_usd: float
    message: str
