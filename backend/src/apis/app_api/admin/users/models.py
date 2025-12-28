"""Request/response models for admin users API."""

from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional


class UserListItemResponse(BaseModel):
    """User item for list views."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    status: str
    last_login_at: str = Field(..., alias="lastLoginAt")
    email_domain: Optional[str] = Field(None, alias="emailDomain")

    # Quick stats (optional, populated for dashboard views)
    current_month_cost: Optional[float] = Field(None, alias="currentMonthCost")
    quota_usage_percentage: Optional[float] = Field(None, alias="quotaUsagePercentage")


class UserListResponse(BaseModel):
    """Paginated user list response."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    users: List[UserListItemResponse]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
    total_count: Optional[int] = Field(None, alias="totalCount")


class QuotaStatusResponse(BaseModel):
    """User's current quota status."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    tier_id: Optional[str] = Field(None, alias="tierId")
    tier_name: Optional[str] = Field(None, alias="tierName")
    matched_by: Optional[str] = Field(None, alias="matchedBy")
    monthly_limit: Optional[float] = Field(None, alias="monthlyLimit")
    current_usage: float = Field(0.0, alias="currentUsage")
    usage_percentage: float = Field(0.0, alias="usagePercentage")
    remaining: Optional[float] = None
    has_active_override: bool = Field(False, alias="hasActiveOverride")
    override_reason: Optional[str] = Field(None, alias="overrideReason")


class CostSummaryResponse(BaseModel):
    """User's current month cost summary."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    total_cost: float = Field(0.0, alias="totalCost")
    total_requests: int = Field(0, alias="totalRequests")
    total_input_tokens: int = Field(0, alias="totalInputTokens")
    total_output_tokens: int = Field(0, alias="totalOutputTokens")
    cache_savings: float = Field(0.0, alias="cacheSavings")
    primary_model: Optional[str] = Field(None, alias="primaryModel")


class QuotaEventSummary(BaseModel):
    """Summary of a quota event."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    event_id: str = Field(..., alias="eventId")
    event_type: str = Field(..., alias="eventType")
    timestamp: str
    percentage_used: float = Field(..., alias="percentageUsed")


class UserProfileResponse(BaseModel):
    """Full user profile."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    roles: List[str] = Field(default_factory=list)
    picture: Optional[str] = None
    email_domain: str = Field(..., alias="emailDomain")
    created_at: str = Field(..., alias="createdAt")
    last_login_at: str = Field(..., alias="lastLoginAt")
    status: str


class UserDetailResponse(BaseModel):
    """Comprehensive user detail for admin view."""
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    profile: UserProfileResponse
    cost_summary: CostSummaryResponse = Field(..., alias="costSummary")
    quota_status: QuotaStatusResponse = Field(..., alias="quotaStatus")
    recent_events: List[QuotaEventSummary] = Field(
        default_factory=list,
        alias="recentEvents"
    )
