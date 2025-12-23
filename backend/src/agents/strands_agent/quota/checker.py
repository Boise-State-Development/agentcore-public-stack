"""Quota checker for enforcing hard limits."""

from typing import Optional
from datetime import datetime
import logging
from apis.shared.auth.models import User
from apis.app_api.costs.aggregator import CostAggregator
from .models import QuotaTier, QuotaCheckResult
from .resolver import QuotaResolver
from .event_recorder import QuotaEventRecorder

logger = logging.getLogger(__name__)


class QuotaChecker:
    """Checks quota limits and enforces hard/soft limits"""

    def __init__(
        self,
        resolver: QuotaResolver,
        cost_aggregator: CostAggregator,
        event_recorder: QuotaEventRecorder
    ):
        self.resolver = resolver
        self.cost_aggregator = cost_aggregator
        self.event_recorder = event_recorder

    async def check_quota(
        self,
        user: User,
        session_id: Optional[str] = None
    ) -> QuotaCheckResult:
        """
        Check if user is within quota limits (soft + hard limits).

        Returns QuotaCheckResult with:
        - allowed: bool - whether request should proceed
        - message: str - explanation
        - tier: QuotaTier - applicable tier
        - current_usage, quota_limit, percentage_used, remaining
        - warning_level: "none", "80%", "90%"
        """
        # Resolve user's quota tier
        resolved = await self.resolver.resolve_user_quota(user)

        if not resolved:
            # No quota configured - allow by default
            logger.info(f"No quota configured for user {user.user_id}, allowing request")
            return QuotaCheckResult(
                allowed=True,
                message="No quota configured",
                current_usage=0.0,
                percentage_used=0.0
            )

        tier = resolved.tier

        # Handle unlimited tier (float('inf') support)
        if tier.monthly_cost_limit == float('inf') or tier.monthly_cost_limit >= 999999:
            return QuotaCheckResult(
                allowed=True,
                message="Unlimited quota",
                tier=tier,
                current_usage=0.0,
                quota_limit=tier.monthly_cost_limit,
                percentage_used=0.0,
                warning_level="none"
            )

        # Get current usage for the period
        period = self._get_current_period(tier.period_type)
        try:
            summary = await self.cost_aggregator.get_user_cost_summary(
                user_id=user.user_id,
                period=period
            )
            current_usage = summary.total_cost
        except Exception as e:
            logger.error(f"Error getting cost summary for user {user.user_id}: {e}")
            # On error, allow request but log warning
            return QuotaCheckResult(
                allowed=True,
                message="Error checking quota, allowing request",
                tier=tier,
                current_usage=0.0,
                percentage_used=0.0
            )

        # Determine limit based on period type
        # Convert to float for consistent arithmetic with current_usage
        if tier.period_type == "daily" and tier.daily_cost_limit is not None:
            limit = float(tier.daily_cost_limit)
        else:
            limit = float(tier.monthly_cost_limit)

        percentage_used = (current_usage / limit * 100) if limit > 0 else 0
        remaining = max(0.0, limit - current_usage)

        # Determine warning level
        warning_level = "none"
        soft_limit_percentage = float(tier.soft_limit_percentage)

        if percentage_used >= 90:
            warning_level = "90%"
        elif percentage_used >= soft_limit_percentage:
            warning_level = f"{int(soft_limit_percentage)}%"

        # Record warning events if thresholds crossed
        if warning_level != "none":
            await self.event_recorder.record_warning_if_needed(
                user=user,
                tier=tier,
                current_usage=current_usage,
                limit=limit,
                percentage_used=percentage_used,
                threshold=warning_level,
                session_id=session_id,
                assignment_id=resolved.assignment.assignment_id if resolved.assignment else None
            )

        # Check hard limit (block or warn based on tier config)
        if current_usage >= limit:
            if tier.action_on_limit == "block":
                # Record block event
                await self.event_recorder.record_block(
                    user=user,
                    tier=tier,
                    current_usage=current_usage,
                    limit=limit,
                    percentage_used=percentage_used,
                    session_id=session_id,
                    assignment_id=resolved.assignment.assignment_id if resolved.assignment else None
                )

                logger.warning(
                    f"Quota exceeded for user {user.user_id}: "
                    f"${current_usage:.2f} / ${limit:.2f} ({percentage_used:.1f}%)"
                )

                return QuotaCheckResult(
                    allowed=False,
                    message=f"Quota exceeded: ${current_usage:.2f} / ${limit:.2f}",
                    tier=tier,
                    current_usage=current_usage,
                    quota_limit=limit,
                    percentage_used=percentage_used,
                    remaining=0.0,
                    warning_level=warning_level
                )
            else:  # warn only
                logger.warning(
                    f"Quota limit reached for user {user.user_id} (warn-only): "
                    f"${current_usage:.2f} / ${limit:.2f} ({percentage_used:.1f}%)"
                )

                return QuotaCheckResult(
                    allowed=True,
                    message=f"Warning: Quota limit reached (${current_usage:.2f} / ${limit:.2f})",
                    tier=tier,
                    current_usage=current_usage,
                    quota_limit=limit,
                    percentage_used=percentage_used,
                    remaining=0.0,
                    warning_level=warning_level
                )

        # Within limits
        message = "Within quota"
        if warning_level != "none":
            message = f"Warning: {warning_level} quota used (${current_usage:.2f} / ${limit:.2f})"

        logger.debug(
            f"Quota check passed for user {user.user_id}: "
            f"${current_usage:.2f} / ${limit:.2f} ({percentage_used:.1f}%)"
        )

        return QuotaCheckResult(
            allowed=True,
            message=message,
            tier=tier,
            current_usage=current_usage,
            quota_limit=limit,
            percentage_used=percentage_used,
            remaining=remaining,
            warning_level=warning_level
        )

    def _get_current_period(self, period_type: str) -> str:
        """Get current period string for cost aggregation"""
        now = datetime.utcnow()

        if period_type == "monthly":
            return now.strftime("%Y-%m")
        elif period_type == "daily":
            return now.strftime("%Y-%m-%d")
        else:
            # Default to monthly
            return now.strftime("%Y-%m")
