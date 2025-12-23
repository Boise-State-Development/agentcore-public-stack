"""Quota enforcement dependencies for FastAPI.

Provides singleton instances of quota management components and
FastAPI dependencies for checking user quotas before processing requests.
"""

import logging
import os
from typing import Optional
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict

from agents.strands_agent.quota.repository import QuotaRepository
from agents.strands_agent.quota.resolver import QuotaResolver
from agents.strands_agent.quota.checker import QuotaChecker
from agents.strands_agent.quota.event_recorder import QuotaEventRecorder
from agents.strands_agent.quota.models import QuotaCheckResult
from apis.app_api.costs.aggregator import CostAggregator

logger = logging.getLogger(__name__)

# Check if quota enforcement is enabled (defaults to false for backward compatibility)
ENABLE_QUOTA_ENFORCEMENT = os.environ.get('ENABLE_QUOTA_ENFORCEMENT', 'false').lower() == 'true'

# Singleton instances (lazy initialization)
_quota_repository: Optional[QuotaRepository] = None
_quota_resolver: Optional[QuotaResolver] = None
_cost_aggregator: Optional[CostAggregator] = None
_event_recorder: Optional[QuotaEventRecorder] = None
_quota_checker: Optional[QuotaChecker] = None


def get_quota_repository() -> QuotaRepository:
    """Get or create singleton QuotaRepository"""
    global _quota_repository
    if _quota_repository is None:
        _quota_repository = QuotaRepository()
        logger.info("QuotaRepository singleton initialized")
    return _quota_repository


def get_quota_resolver() -> QuotaResolver:
    """Get or create singleton QuotaResolver"""
    global _quota_resolver
    if _quota_resolver is None:
        repository = get_quota_repository()
        _quota_resolver = QuotaResolver(repository)
        logger.info("QuotaResolver singleton initialized")
    return _quota_resolver


def get_cost_aggregator() -> CostAggregator:
    """Get or create singleton CostAggregator"""
    global _cost_aggregator
    if _cost_aggregator is None:
        _cost_aggregator = CostAggregator()
        logger.info("CostAggregator singleton initialized")
    return _cost_aggregator


def get_event_recorder() -> QuotaEventRecorder:
    """Get or create singleton QuotaEventRecorder"""
    global _event_recorder
    if _event_recorder is None:
        repository = get_quota_repository()
        _event_recorder = QuotaEventRecorder(repository)
        logger.info("QuotaEventRecorder singleton initialized")
    return _event_recorder


def get_quota_checker() -> QuotaChecker:
    """Get or create singleton QuotaChecker"""
    global _quota_checker
    if _quota_checker is None:
        resolver = get_quota_resolver()
        cost_aggregator = get_cost_aggregator()
        event_recorder = get_event_recorder()
        _quota_checker = QuotaChecker(resolver, cost_aggregator, event_recorder)
        logger.info("QuotaChecker singleton initialized")
    return _quota_checker


def is_quota_enforcement_enabled() -> bool:
    """Check if quota enforcement is enabled"""
    return ENABLE_QUOTA_ENFORCEMENT


class QuotaExceededResponse(BaseModel):
    """Response model for 429 Too Many Requests when quota is exceeded"""
    model_config = ConfigDict(populate_by_name=True)

    error: str = "Quota exceeded"
    code: str = "rate_limit_exceeded"
    message: str = Field(..., description="User-friendly message about quota status")
    current_usage: float = Field(..., alias="currentUsage", description="Current usage in dollars")
    quota_limit: float = Field(..., alias="quotaLimit", description="Quota limit in dollars")
    percentage_used: float = Field(..., alias="percentageUsed", description="Percentage of quota used")
    period_type: str = Field(..., alias="periodType", description="Quota period (monthly/daily)")
    tier_name: Optional[str] = Field(None, alias="tierName", description="Name of the quota tier")
    reset_info: Optional[str] = Field(None, alias="resetInfo", description="When quota resets")


class QuotaWarningEvent(BaseModel):
    """SSE event for quota warnings (soft limit approaching)"""
    model_config = ConfigDict(populate_by_name=True)

    type: str = "quota_warning"
    warning_level: str = Field(..., alias="warningLevel", description="Warning threshold (80%, 90%)")
    current_usage: float = Field(..., alias="currentUsage")
    quota_limit: float = Field(..., alias="quotaLimit")
    percentage_used: float = Field(..., alias="percentageUsed")
    remaining: float = Field(..., description="Remaining quota in dollars")
    message: str = Field(..., description="User-friendly warning message")

    def to_sse_format(self) -> str:
        """Convert to SSE event format"""
        import json
        return f"event: quota_warning\ndata: {json.dumps(self.model_dump(by_alias=True, exclude_none=True))}\n\n"


class QuotaExceededEvent(BaseModel):
    """SSE event for quota exceeded (hard limit reached).

    This is sent as a stream response instead of a 429 HTTP error to provide
    a better user experience - the message appears in the chat as an assistant
    response and is persisted to the session history.
    """
    model_config = ConfigDict(populate_by_name=True)

    type: str = "quota_exceeded"
    current_usage: float = Field(..., alias="currentUsage", description="Current usage in dollars")
    quota_limit: float = Field(..., alias="quotaLimit", description="Quota limit in dollars")
    percentage_used: float = Field(..., alias="percentageUsed", description="Percentage of quota used")
    period_type: str = Field(..., alias="periodType", description="Quota period (monthly/daily)")
    tier_name: Optional[str] = Field(None, alias="tierName", description="Name of the quota tier")
    reset_info: str = Field(..., alias="resetInfo", description="When quota resets")
    message: str = Field(..., description="User-friendly message to display as assistant response")

    def to_sse_format(self) -> str:
        """Convert to SSE event format"""
        import json
        return f"event: quota_exceeded\ndata: {json.dumps(self.model_dump(by_alias=True, exclude_none=True))}\n\n"


def build_quota_exceeded_response(result: QuotaCheckResult) -> QuotaExceededResponse:
    """Build a 429 response from a QuotaCheckResult"""
    from datetime import datetime

    # Calculate reset info
    now = datetime.utcnow()
    period_type = result.tier.period_type if result.tier else "monthly"

    if period_type == "daily":
        reset_info = "Quota resets at midnight UTC"
    else:
        # Monthly - calculate days until end of month
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1)
        else:
            next_month = datetime(now.year, now.month + 1, 1)
        days_remaining = (next_month - now).days
        reset_info = f"Quota resets in {days_remaining} day(s)"

    return QuotaExceededResponse(
        message=result.message,
        currentUsage=float(result.current_usage),
        quotaLimit=float(result.quota_limit) if result.quota_limit else 0.0,
        percentageUsed=float(result.percentage_used),
        periodType=period_type,
        tierName=result.tier.tier_name if result.tier else None,
        resetInfo=reset_info
    )


def build_quota_warning_event(result: QuotaCheckResult) -> Optional[QuotaWarningEvent]:
    """Build a quota warning SSE event if warning level is set"""
    if result.warning_level == "none" or result.warning_level is None:
        return None

    return QuotaWarningEvent(
        warningLevel=result.warning_level,
        currentUsage=float(result.current_usage),
        quotaLimit=float(result.quota_limit) if result.quota_limit else 0.0,
        percentageUsed=float(result.percentage_used),
        remaining=float(result.remaining) if result.remaining else 0.0,
        message=f"You have used {result.warning_level} of your quota (${float(result.current_usage):.2f} / ${float(result.quota_limit):.2f})"
    )


def build_quota_exceeded_event(result: QuotaCheckResult) -> QuotaExceededEvent:
    """Build a quota exceeded SSE event from a QuotaCheckResult.

    This creates an event that will be streamed to the client and displayed
    as an assistant message in the chat, providing a better UX than a 429 error.
    """
    from datetime import datetime

    # Calculate reset info
    now = datetime.utcnow()
    period_type = result.tier.period_type if result.tier else "monthly"

    if period_type == "daily":
        reset_info = "Your quota resets at midnight UTC."
    else:
        # Monthly - calculate days until end of month
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1)
        else:
            next_month = datetime(now.year, now.month + 1, 1)
        days_remaining = (next_month - now).days
        reset_info = f"Your quota resets in {days_remaining} day(s)."

    # Build user-friendly message with markdown styling
    current = float(result.current_usage)
    limit = float(result.quota_limit) if result.quota_limit else 0.0
    tier_name = result.tier.tier_name if result.tier else "your plan"
    percentage = int(result.percentage_used)

    message = f"""I apologize, but you've reached your usage limit for **{tier_name}**.

**Current Usage**
| Metric | Value |
|--------|-------|
| Used | **${current:.2f}** |
| Limit | ${limit:.2f} |
| Usage | {percentage}% |

**What's Next?**
- {reset_info}
- If you need additional capacity, please contact your administrator.

I'm here to help once your quota resets!"""

    return QuotaExceededEvent(
        currentUsage=current,
        quotaLimit=limit,
        percentageUsed=float(result.percentage_used),
        periodType=period_type,
        tierName=result.tier.tier_name if result.tier else None,
        resetInfo=reset_info,
        message=message
    )
