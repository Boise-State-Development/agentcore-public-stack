"""Quota checker for enforcing hard limits."""

from typing import Optional
from datetime import datetime, timezone
import logging
from apis.shared.auth.models import User
from apis.shared.costs.aggregator import CostAggregator
from .models import QuotaCheckResult, QuotaTier
from .resolver import QuotaResolver
from .event_recorder import QuotaEventRecorder
from .thresholds import (
    resolve_session_notice_percentage,
    resolve_warning_thresholds,
    select_warning_level,
    session_notice_threshold_usd,
)

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
        - warning_level: highest crossed rung of the tier's ladder
          ("50%", "75%", the tier's soft limit, "90%") or "none"
        - session_cost / session_percentage_of_limit / session_notice_threshold:
          set only when this session alone has crossed the tier's
          session-notice share of the monthly limit
        """
        # Resolve user's quota tier
        resolved = await self.resolver.resolve_user_quota(user)

        if not resolved:
            # No quota configured - block request (fail-closed)
            logger.warning(f"No quota tier configured for user {user.user_id}, blocking request")
            return QuotaCheckResult(
                allowed=False,
                message="No quota tier configured. Please contact your administrator.",
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

        # Determine warning level from the tier's ladder. Pre-#833 this was a
        # hardcoded 90 / soft-limit pair; the ladder still contains both, so
        # the only change for an unconfigured tier is that 50% and 75% now
        # also fire — days earlier, which is the whole point (D5).
        thresholds = resolve_warning_thresholds(tier)
        warning_level = select_warning_level(percentage_used, thresholds)

        assignment_id = resolved.assignment.assignment_id if resolved.assignment else None

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
                assignment_id=assignment_id
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
                    assignment_id=assignment_id
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
        # Per-session runway, independent of where the user sits on the ladder
        # above: one conversation can be most of a month's budget while the
        # user-level percentage is still unremarkable. Skipped on the blocked
        # path above — a blocked turn has nothing to warn about.
        session_fields = await self._resolve_session_notice(
            user=user,
            tier=tier,
            limit=limit,
            session_id=session_id,
            assignment_id=assignment_id,
        )

        if current_usage >= limit:  # warn only
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
                warning_level=warning_level,
                **session_fields
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
            warning_level=warning_level,
            **session_fields
        )

    async def _resolve_session_notice(
        self,
        user: User,
        tier: QuotaTier,
        limit: float,
        session_id: Optional[str],
        assignment_id: Optional[str],
    ) -> dict:
        """Return the session-notice fields when *session_id* is a heavy one.

        Reads the denormalized ``totalCost`` that ``_bump_session_aggregates``
        already maintains on the session row — no new aggregation, one
        ``SessionLookupIndex`` GSI query, and only when the tier actually has
        a notice share configured.

        The cost read is the session's **lifetime** total, deliberately: the
        incident conversation opened on 2026-07-30 and had already spent 20%
        of a monthly budget before the August period even started. Scoping the
        number to the calendar period would have hidden exactly the
        conversation the notice exists to surface, and would have moved its
        first firing a full day later (2026-08-02 instead of 2026-08-01).

        Never raises: a missing session row, a legacy row without
        ``totalCost``, or a DynamoDB error just means no notice this turn.
        """
        threshold_usd = session_notice_threshold_usd(limit, tier)
        if not session_id or threshold_usd is None:
            return {}

        try:
            from apis.shared.sessions.metadata import get_session_metadata

            metadata = await get_session_metadata(session_id, user.user_id)
        except Exception as e:
            logger.debug("Session notice skipped (metadata read failed): %s", e)
            return {}

        session_cost = getattr(metadata, "total_cost", None) if metadata else None
        if session_cost is None:
            return {}

        session_cost = float(session_cost)
        if session_cost < threshold_usd:
            return {}

        session_share = (session_cost / limit * 100) if limit > 0 else 0.0

        try:
            await self.event_recorder.record_session_notice_if_needed(
                user=user,
                tier=tier,
                session_id=session_id,
                session_cost=session_cost,
                limit=limit,
                session_percentage=session_share,
                assignment_id=assignment_id,
            )
        except Exception as e:
            # The durable breadcrumb is for support; losing it must not cost
            # the user the live notice it accompanies.
            logger.warning("Session notice event not recorded: %s", e)

        logger.info(
            "Session %s has reached %.1f%% of the monthly limit for user %s",
            session_id, session_share, user.user_id,
        )

        return {
            "session_id": session_id,
            "session_cost": session_cost,
            "session_percentage_of_limit": session_share,
            "session_notice_threshold": resolve_session_notice_percentage(tier),
        }

    def _get_current_period(self, period_type: str) -> str:
        """Get current period string for cost aggregation"""
        now = datetime.now(timezone.utc)

        if period_type == "monthly":
            return now.strftime("%Y-%m")
        elif period_type == "daily":
            return now.strftime("%Y-%m-%d")
        else:
            # Default to monthly
            return now.strftime("%Y-%m")
