"""Core domain models for quota management system."""

from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, Literal, Dict, Any
from enum import Enum


class QuotaAssignmentType(str, Enum):
    """How a quota is assigned to users (Phase 1)"""
    DIRECT_USER = "direct_user"
    JWT_ROLE = "jwt_role"
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

    # Hard limit behavior (Phase 1: block only)
    action_on_limit: Literal["block"] = Field(
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

    @field_validator('user_id', 'jwt_role')
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

        return v


class QuotaEvent(BaseModel):
    """Track quota enforcement events (Phase 1: blocks only)"""
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(..., alias="eventId")
    user_id: str = Field(..., alias="userId")
    tier_id: str = Field(..., alias="tierId")
    event_type: Literal["block"] = Field(..., alias="eventType")  # Phase 1: blocks only

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


class ResolvedQuota(BaseModel):
    """Resolved quota information for a user"""
    model_config = ConfigDict(populate_by_name=True)
    
    user_id: str = Field(..., alias="userId")
    tier: QuotaTier
    matched_by: str = Field(
        ...,
        alias="matchedBy",
        description="How quota was resolved (e.g., 'direct_user', 'jwt_role:Faculty')"
    )
    assignment: QuotaAssignment
