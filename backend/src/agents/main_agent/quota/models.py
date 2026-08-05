"""Core domain models for quota management system."""

from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, Literal, Dict, Any, List
from enum import Enum
from decimal import Decimal


class QuotaAssignmentType(str, Enum):
    """How a quota is assigned to users.

    Priority order (highest to lowest):
    1. DIRECT_USER - Direct assignment to a specific user
    2. APP_ROLE - Assignment via application role (AppRole system)
    3. JWT_ROLE - Assignment via JWT role from identity provider
    4. EMAIL_DOMAIN - Assignment via email domain pattern
    5. DEFAULT_TIER - Fallback for unmatched users
    """
    DIRECT_USER = "direct_user"
    APP_ROLE = "app_role"
    JWT_ROLE = "jwt_role"
    EMAIL_DOMAIN = "email_domain"
    DEFAULT_TIER = "default_tier"


class QuotaTier(BaseModel):
    """A quota tier configuration"""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId")
    tier_name: str = Field(..., alias="tierName")
    description: Optional[str] = None

    # Quota limits - stored as Decimal for DynamoDB compatibility
    monthly_cost_limit: Decimal = Field(..., alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[Decimal] = Field(None, alias="dailyCostLimit", gt=0)
    period_type: Literal["daily", "monthly"] = Field(default="monthly", alias="periodType")

    # Soft limit configuration
    soft_limit_percentage: Decimal = Field(
        default=Decimal("80.0"),
        alias="softLimitPercentage",
        ge=0,
        le=100,
        description="Percentage at which warnings start"
    )

    # Rungs *below* the soft limit, so a user burning a month-long budget in
    # days hears about it days ahead instead of hours ahead (#833 PR-5).
    # None → the platform default (50% and 75%); an explicit empty list opts
    # the tier out and leaves only softLimitPercentage + 90%.
    early_warning_percentages: Optional[List[Decimal]] = Field(
        default=None,
        alias="earlyWarningPercentages",
        description="Extra warning percentages below the soft limit (default 50 and 75); [] disables them"
    )

    # Share of the monthly limit at which a *single conversation* is called
    # out to the user ("this conversation has used $X of your $Y"). 0 disables.
    session_notice_percentage: Decimal = Field(
        default=Decimal("25.0"),
        alias="sessionNoticePercentage",
        ge=0,
        le=100,
        description="Percentage of the monthly limit one session must reach to trigger a session notice (0 disables)"
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

    @field_validator(
        'monthly_cost_limit',
        'daily_cost_limit',
        'soft_limit_percentage',
        'session_notice_percentage',
        mode='before',
    )
    @classmethod
    def convert_to_decimal(cls, v):
        """Convert float/int to Decimal for DynamoDB compatibility"""
        if v is None:
            return v
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        if isinstance(v, str):
            return Decimal(v)
        return v

    @field_validator('early_warning_percentages', mode='before')
    @classmethod
    def convert_percentage_list(cls, v):
        """Same Decimal coercion, elementwise. `[]` is meaningful (opt-out)."""
        if v is None:
            return v
        if isinstance(v, (list, tuple)):
            return [
                Decimal(str(item)) if isinstance(item, (int, float, str)) else item
                for item in v
            ]
        return v


class QuotaAssignment(BaseModel):
    """Assignment of a quota tier to users"""
    model_config = ConfigDict(populate_by_name=True)

    assignment_id: str = Field(..., alias="assignmentId")
    tier_id: str = Field(..., alias="tierId")
    assignment_type: QuotaAssignmentType = Field(..., alias="assignmentType")

    # Assignment criteria (one populated based on type)
    user_id: Optional[str] = Field(None, alias="userId")
    app_role_id: Optional[str] = Field(None, alias="appRoleId")
    jwt_role: Optional[str] = Field(None, alias="jwtRole")
    email_domain: Optional[str] = Field(None, alias="emailDomain")

    # Priority (higher = more specific, evaluated first)
    # Default priorities by type:
    # - DIRECT_USER: 300
    # - APP_ROLE: 250
    # - JWT_ROLE: 200
    # - EMAIL_DOMAIN: 150
    # - DEFAULT_TIER: 100
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

    @field_validator('user_id', 'app_role_id', 'jwt_role', 'email_domain')
    @classmethod
    def validate_criteria_match(cls, v, info):
        """Ensure criteria matches assignment type"""
        assignment_type = info.data.get('assignment_type')
        field_name = info.field_name

        if assignment_type == QuotaAssignmentType.DIRECT_USER and field_name == 'user_id':
            if not v:
                raise ValueError("user_id required for direct_user assignment")
        elif assignment_type == QuotaAssignmentType.APP_ROLE and field_name == 'app_role_id':
            if not v:
                raise ValueError("app_role_id required for app_role assignment")
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
    event_type: Literal[
        "warning", "block", "reset", "override_applied", "session_notice"
    ] = Field(
        ...,
        alias="eventType"
    )

    # Context - using Decimal for DynamoDB compatibility
    current_usage: Decimal = Field(..., alias="currentUsage")
    quota_limit: Decimal = Field(..., alias="quotaLimit")
    percentage_used: Decimal = Field(..., alias="percentageUsed")

    timestamp: str
    metadata: Optional[Dict[str, Any]] = None

    @field_validator('current_usage', 'quota_limit', 'percentage_used', mode='before')
    @classmethod
    def convert_to_decimal(cls, v):
        """Convert float/int to Decimal for DynamoDB compatibility"""
        if v is None:
            return v
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        if isinstance(v, str):
            return Decimal(v)
        return v


class QuotaCheckResult(BaseModel):
    """Result of quota check"""
    model_config = ConfigDict(populate_by_name=True)

    allowed: bool
    message: str
    tier: Optional[QuotaTier] = None
    current_usage: Decimal = Field(default=Decimal("0.0"), alias="currentUsage")
    quota_limit: Optional[Decimal] = Field(None, alias="quotaLimit")
    percentage_used: Decimal = Field(default=Decimal("0.0"), alias="percentageUsed")
    remaining: Optional[Decimal] = None
    # Free-form since #833 PR-5: the ladder is tier-configurable, so the set
    # of labels is open ("50%", "75%", the tier's own soft limit, "90%").
    warning_level: Optional[str] = Field(
        default="none",
        alias="warningLevel"
    )

    # Per-session runway. Populated only when this turn's session has itself
    # reached the tier's session-notice share of the monthly limit — the
    # signal the incident session never produced (it spent 90% of a month's
    # budget while every per-user warning stayed quiet until the last day).
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    session_cost: Optional[Decimal] = Field(default=None, alias="sessionCost")
    session_percentage_of_limit: Optional[Decimal] = Field(
        default=None,
        alias="sessionPercentageOfLimit",
        description="The session's lifetime cost as a percentage of the monthly limit",
    )
    session_notice_threshold: Optional[Decimal] = Field(
        default=None,
        alias="sessionNoticeThreshold",
        description="The configured share (percent) that this session crossed",
    )

    @field_validator(
        'current_usage',
        'quota_limit',
        'percentage_used',
        'remaining',
        'session_cost',
        'session_percentage_of_limit',
        'session_notice_threshold',
        mode='before',
    )
    @classmethod
    def convert_to_decimal(cls, v):
        """Convert float/int to Decimal for DynamoDB compatibility"""
        if v is None:
            return v
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        if isinstance(v, str):
            return Decimal(v)
        return v


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

    # Custom limits (required if override_type == "custom_limit") - using Decimal for DynamoDB
    monthly_cost_limit: Optional[Decimal] = Field(None, alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[Decimal] = Field(None, alias="dailyCostLimit", gt=0)

    # Temporal bounds
    valid_from: str = Field(..., alias="validFrom")
    valid_until: str = Field(..., alias="validUntil")

    # Metadata
    reason: str = Field(..., description="Justification for override")
    created_by: str = Field(..., alias="createdBy")
    created_at: str = Field(..., alias="createdAt")
    enabled: bool = Field(default=True)

    @field_validator('monthly_cost_limit', 'daily_cost_limit', mode='before')
    @classmethod
    def convert_to_decimal(cls, v):
        """Convert float/int to Decimal for DynamoDB compatibility"""
        if v is None:
            return v
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        if isinstance(v, str):
            return Decimal(v)
        return v

    @field_validator('monthly_cost_limit', mode='after')
    @classmethod
    def validate_custom_limit(cls, v, info):
        """Ensure custom_limit type has a limit specified"""
        if info.data.get('override_type') == 'custom_limit' and v is None:
            raise ValueError("monthly_cost_limit required for custom_limit type")
        return v
