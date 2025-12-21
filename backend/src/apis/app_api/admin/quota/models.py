"""Request/response models for quota admin API."""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Literal
from agents.strands_agent.quota.models import (
    QuotaTier,
    QuotaAssignment,
    QuotaEvent,
    QuotaAssignmentType,
    QuotaOverride
)


# ========== Tier Models ==========

class QuotaTierCreate(BaseModel):
    """Create quota tier request"""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId", description="Unique tier identifier")
    tier_name: str = Field(..., alias="tierName", description="Display name")
    description: Optional[str] = None

    monthly_cost_limit: float = Field(..., alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)
    period_type: Literal["daily", "monthly"] = Field(default="monthly", alias="periodType")
    soft_limit_percentage: float = Field(default=80.0, alias="softLimitPercentage", ge=0, le=100)
    action_on_limit: Literal["block", "warn"] = Field(default="block", alias="actionOnLimit")
    enabled: bool = True


class QuotaTierUpdate(BaseModel):
    """Update quota tier request (partial)"""
    model_config = ConfigDict(populate_by_name=True)

    tier_name: Optional[str] = Field(None, alias="tierName")
    description: Optional[str] = None
    monthly_cost_limit: Optional[float] = Field(None, alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)
    period_type: Optional[Literal["daily", "monthly"]] = Field(None, alias="periodType")
    enabled: Optional[bool] = None


# ========== Assignment Models ==========

class QuotaAssignmentCreate(BaseModel):
    """Create quota assignment request"""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId")
    assignment_type: QuotaAssignmentType = Field(..., alias="assignmentType")

    # Conditional fields based on assignment type
    user_id: Optional[str] = Field(None, alias="userId")
    jwt_role: Optional[str] = Field(None, alias="jwtRole")
    email_domain: Optional[str] = Field(None, alias="emailDomain")

    priority: int = Field(default=100, ge=0)
    enabled: bool = True


class QuotaAssignmentUpdate(BaseModel):
    """Update quota assignment request (partial)"""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: Optional[str] = Field(None, alias="tierId")
    priority: Optional[int] = Field(None, ge=0)
    enabled: Optional[bool] = None


# ========== User Quota Info (Inspector) ==========

class UserQuotaInfo(BaseModel):
    """Comprehensive quota information for a user (admin inspector)"""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    email: str
    roles: List[str]

    # Resolved quota
    tier: Optional[QuotaTier] = None
    assignment: Optional[QuotaAssignment] = None
    matched_by: Optional[str] = Field(None, alias="matchedBy")

    # Current usage
    current_period: str = Field(..., alias="currentPeriod")
    current_usage: float = Field(..., alias="currentUsage")
    quota_limit: Optional[float] = Field(None, alias="quotaLimit")
    percentage_used: float = Field(..., alias="percentageUsed")
    remaining: Optional[float] = None

    # Recent events
    recent_blocks: int = Field(default=0, alias="recentBlocks", description="Blocks in last 24h")
    last_block_time: Optional[str] = Field(None, alias="lastBlockTime")


# ========== Override Models ==========

class QuotaOverrideCreate(BaseModel):
    """Create quota override request"""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    override_type: Literal["custom_limit", "unlimited"] = Field(..., alias="overrideType")

    monthly_cost_limit: Optional[float] = Field(None, alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)

    valid_from: str = Field(..., alias="validFrom")
    valid_until: str = Field(..., alias="validUntil")
    reason: str


class QuotaOverrideUpdate(BaseModel):
    """Update quota override request (partial)"""
    model_config = ConfigDict(populate_by_name=True)

    valid_until: Optional[str] = Field(None, alias="validUntil")
    enabled: Optional[bool] = None
    reason: Optional[str] = None
