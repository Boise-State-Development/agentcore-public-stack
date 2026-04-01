"""Admin request/response models for fine-tuning access management."""

from pydantic import BaseModel, Field
from typing import List
from apis.app_api.fine_tuning.models import FineTuningAccessGrant


class GrantAccessRequest(BaseModel):
    """Request body for granting fine-tuning access."""
    email: str
    monthly_quota_hours: float = Field(default=10.0, gt=0)


class UpdateQuotaRequest(BaseModel):
    """Request body for updating a user's GPU-hour quota."""
    monthly_quota_hours: float = Field(gt=0)


class AccessListResponse(BaseModel):
    """Response for listing all access grants."""
    grants: List[FineTuningAccessGrant]
    total_count: int


# ========== Cost Dashboard Models ==========


class UserCostBreakdown(BaseModel):
    """Per-user cost breakdown for a billing period."""
    email: str
    total_cost_usd: float
    total_gpu_hours: float
    training_job_count: int
    inference_job_count: int


class FineTuningCostDashboard(BaseModel):
    """Aggregated cost dashboard for admin fine-tuning cost view."""
    period: str = Field(description="YYYY-MM billing period")
    total_cost_usd: float
    total_gpu_hours: float
    active_user_count: int
    training_job_count: int
    inference_job_count: int
    users: List[UserCostBreakdown]
