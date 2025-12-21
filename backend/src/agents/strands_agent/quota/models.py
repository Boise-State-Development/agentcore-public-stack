"""Core domain models for quota management system."""

from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, Literal, Dict, Any
from enum import Enum


class QuotaAssignmentType(str, Enum):
    """How a quota is assigned to users"""
    DIRECT_USER = "direct_user"
    JWT_ROLE = "jwt_role"
    EMAIL_DOMAIN = "email_domain"
    DEFAULT_TIER = "default_tier"


class QuotaTier(BaseModel):
    """A quota tier configuration"""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId")
    tier_name: str = Field(..., alias="tierName")
    description: Optional[str] = None

    # Quota limits
    monthly_cost_limit: float = Field(..., alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)
    period_type: Literal["daily", "monthly"] = Field(default="monthly", alias="periodType")

    # Soft limit configuration
    soft_limit_percentage: float = Field(
        default=80.0,
        alias="softLimitPercentage",
        ge=0,
        le=100,
        description="Percentage at which warnings start"
    )

    # Hard limit behavior (warn or block)
    action_on_limit: Literal["block", "warn"] = Field(
        default="block",
        alias="actionOnLimit"
    )

    # Metadata
    enabled: bool = Field(default=True)
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: str = Field(..., alias="createdBy")


class QuotaAssignment(BaseModel):
    """Assignment of a quota tier to users"""
    model_config = ConfigDict(populate_by_name=True)

    assignment_id: str = Field(..., alias="assignmentId")
    tier_id: str = Field(..., alias="tierId")
    assignment_type: QuotaAssignmentType = Field(..., alias="assignmentType")

    # Assignment criteria (one populated based on type)
    user_id: Optional[str] = Field(None, alias="userId")
    jwt_role: Optional[str] = Field(None, alias="jwtRole")
    email_domain: Optional[str] = Field(None, alias="emailDomain")

    # Priority (higher = more specific, evaluated first)
    priority: int = Field(
        default=100,
        description="Higher priority overrides lower",
        ge=0
    )

    # Metadata
    enabled: bool = Field(default=True)
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: str = Field(..., alias="createdBy")

    @field_validator('user_id', 'jwt_role', 'email_domain')
    @classmethod
    def validate_criteria_match(cls, v, info):
        """Ensure criteria matches assignment type"""
        assignment_type = info.data.get('assignment_type')
        field_name = info.field_name

        if assignment_type == QuotaAssignmentType.DIRECT_USER and field_name == 'user_id':
            if not v:
                raise ValueError("user_id required for direct_user assignment")
        elif assignment_type == QuotaAssignmentType.JWT_ROLE and field_name == 'jwt_role':
            if not v:
                raise ValueError("jwt_role required for jwt_role assignment")
        elif assignment_type == QuotaAssignmentType.EMAIL_DOMAIN and field_name == 'email_domain':
            if not v:
                raise ValueError("email_domain required for email_domain assignment")

        return v


class QuotaEvent(BaseModel):
    """Track quota enforcement events (all event types)"""
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(..., alias="eventId")
    user_id: str = Field(..., alias="userId")
    tier_id: str = Field(..., alias="tierId")
    event_type: Literal["warning", "block", "reset", "override_applied"] = Field(
        ...,
        alias="eventType"
    )

    # Context
    current_usage: float = Field(..., alias="currentUsage")
    quota_limit: float = Field(..., alias="quotaLimit")
    percentage_used: float = Field(..., alias="percentageUsed")

    timestamp: str
    metadata: Optional[Dict[str, Any]] = None


class QuotaCheckResult(BaseModel):
    """Result of quota check"""
    model_config = ConfigDict(populate_by_name=True)

    allowed: bool
    message: str
    tier: Optional[QuotaTier] = None
    current_usage: float = Field(default=0.0, alias="currentUsage")
    quota_limit: Optional[float] = Field(None, alias="quotaLimit")
    percentage_used: float = Field(default=0.0, alias="percentageUsed")
    remaining: Optional[float] = None
    warning_level: Optional[Literal["none", "80%", "90%"]] = Field(
        default="none",
        alias="warningLevel"
    )


class ResolvedQuota(BaseModel):
    """Resolved quota information for a user"""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    tier: QuotaTier
    matched_by: str = Field(
        ...,
        alias="matchedBy",
        description="How quota was resolved (e.g., 'direct_user', 'jwt_role:Faculty', 'override')"
    )
    assignment: Optional[QuotaAssignment] = None  # None for overrides
    override: Optional['QuotaOverride'] = None


class QuotaOverride(BaseModel):
    """Temporary quota override for a user"""
    model_config = ConfigDict(populate_by_name=True)

    override_id: str = Field(..., alias="overrideId")
    user_id: str = Field(..., alias="userId")

    override_type: Literal["custom_limit", "unlimited"] = Field(
        ...,
        alias="overrideType"
    )

    # Custom limits (required if override_type == "custom_limit")
    monthly_cost_limit: Optional[float] = Field(None, alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)

    # Temporal bounds
    valid_from: str = Field(..., alias="validFrom")
    valid_until: str = Field(..., alias="validUntil")

    # Metadata
    reason: str = Field(..., description="Justification for override")
    created_by: str = Field(..., alias="createdBy")
    created_at: str = Field(..., alias="createdAt")
    enabled: bool = Field(default=True)

    @field_validator('monthly_cost_limit')
    @classmethod
    def validate_custom_limit(cls, v, info):
        """Ensure custom_limit type has a limit specified"""
        if info.data.get('override_type') == 'custom_limit' and v is None:
            raise ValueError("monthly_cost_limit required for custom_limit type")
        return v
