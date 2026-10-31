"""Quota checker for enforcing hard limits."""

from dataclasses import dataclass
from typing import Dict, List, Optional, Union
from datetime import datetime, timezone
import logging
from apis.shared.auth.models import User
from apis.shared.costs.aggregator import CostAggregator
from apis.shared.timestamps import utc_now_iso
from .extensions import (
    active_extensions,
    build_funding_notice,
    effective_limit,
    is_unlimited,
    period_base_limit,
)
from .models import QuotaCheckResult, QuotaExtension, QuotaTier, ResolvedQuota
from .resolver import QuotaResolver
from .event_recorder import QuotaEventRecorder
from .thresholds import (
    resolve_session_notice_percentage,
    resolve_warning_thresholds,
    select_warning_level,
    session_notice_threshold_usd,
)

logger = logging.getLogger(__name__)


@dataclass
class _Measurement:
    """What one resolution says about this turn, before any event is recorded."""

    resolved: ResolvedQuota
    tier: QuotaTier
    period: str
    current_usage: float
    base_limit: float
    extensions: List[QuotaExtension]
    # Effective limit: base + every active extension; None when uncapped.
    limit: Optional[float]
    would_block: bool


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
        session_id: Optional[str] = None,
        session_total_cost: Optional[float] = None,
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

        ``session_total_cost`` lets a caller that has already read the session
        row this turn supply its ``totalCost`` instead of making this method
        read it again — the inference-api preamble does, where that read was a
        measured 62ms (docs/specs/turn-latency-preamble.md). ``None`` means
        "not known", and the read happens exactly as before; it is NOT a way to
        say "zero".
        """
        # Resolve user's quota tier
        resolved = await self.resolver.resolve_user_quota(user)
        measured = await self._measure(user, resolved, usage_by_period={})
        if isinstance(measured, QuotaCheckResult):
            return measured

        # Fresh-on-block (quota-requests spec §8.3). The resolution above may
        # be up to five minutes old, and the one user that staleness hurts is
        # the blocked one: approved a minute ago, still refused. A turn that is
        # about to be refused re-resolves once, bypassing the cache, and is
        # decided on the fresh result. Every other turn keeps the cached path.
        if measured.would_block:
            usage_by_period = {measured.period: measured.current_usage}
            fresh = await self.resolver.resolve_user_quota(user, bypass_cache=True)
            measured = await self._measure(user, fresh, usage_by_period=usage_by_period)
            if isinstance(measured, QuotaCheckResult):
                return measured

        resolved = measured.resolved
        tier = measured.tier
        current_usage = measured.current_usage
        base_limit = measured.base_limit
        extensions = measured.extensions
        limit = measured.limit

        if limit is None:
            return await self._check_uncapped(
                user=user,
                resolved=resolved,
                tier=tier,
                current_usage=current_usage,
                base_limit=base_limit,
                extensions=extensions,
                session_id=session_id,
                session_total_cost=session_total_cost,
            )

        funding_notice = build_funding_notice(current_usage, base_limit, extensions)

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
                    base_limit=base_limit,
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
            session_total_cost=session_total_cost,
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
                base_limit=base_limit,
                percentage_used=percentage_used,
                remaining=0.0,
                warning_level=warning_level,
                funding_notice=funding_notice,
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
            base_limit=base_limit,
            percentage_used=percentage_used,
            remaining=remaining,
            warning_level=warning_level,
            funding_notice=funding_notice,
            **session_fields
        )

    async def _measure(
        self,
        user: User,
        resolved: Optional[ResolvedQuota],
        usage_by_period: Dict[str, float],
    ) -> Union[QuotaCheckResult, "_Measurement"]:
        """Resolve the limit and read the period's spend.

        Returns a finished ``QuotaCheckResult`` for the cases that need no
        arithmetic (no tier, unlimited, a failed spend read), else the numbers
        the rest of the check runs on. ``usage_by_period`` lets the
        fresh-on-block pass reuse the spend it already read.
        """
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

        # Unlimited tier or override. Short-circuits before extensions, which
        # are inert on top of it (spec §4.1).
        if is_unlimited(tier):
            return QuotaCheckResult(
                allowed=True,
                message="Unlimited quota",
                tier=tier,
                current_usage=0.0,
                quota_limit=tier.monthly_cost_limit,
                percentage_used=0.0,
                warning_level="none"
            )

        period = self._get_current_period(tier.period_type)
        if period in usage_by_period:
            current_usage = usage_by_period[period]
        else:
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

        # Convert to float for consistent arithmetic with current_usage
        base_limit = period_base_limit(tier)
        extensions = active_extensions(resolved.extensions, utc_now_iso())
        limit = effective_limit(base_limit, extensions)

        return _Measurement(
            resolved=resolved,
            tier=tier,
            period=period,
            current_usage=current_usage,
            base_limit=base_limit,
            extensions=extensions,
            limit=limit,
            would_block=(
                limit is not None
                and current_usage >= limit
                and tier.action_on_limit == "block"
            ),
        )

    async def _check_uncapped(
        self,
        *,
        user: User,
        resolved: ResolvedQuota,
        tier: QuotaTier,
        current_usage: float,
        base_limit: float,
        extensions: List[QuotaExtension],
        session_id: Optional[str],
        session_total_cost: Optional[float],
    ) -> QuotaCheckResult:
        """An uncapped standing extension: never block, keep measuring (§8.2).

        Not the unlimited short-circuit. The spend is still read, the
        per-session notice still fires (scaled to the base limit, the only
        denominator there is), and the funding notice names the fund once the
        central allowance is used. There is no warning ladder: with no cap
        there is nothing to approach.
        """
        assignment_id = resolved.assignment.assignment_id if resolved.assignment else None
        session_fields = await self._resolve_session_notice(
            user=user,
            tier=tier,
            limit=base_limit,
            session_id=session_id,
            assignment_id=assignment_id,
            session_total_cost=session_total_cost,
        )
        return QuotaCheckResult(
            allowed=True,
            message="Within quota (standing chargeback, no cap)",
            tier=tier,
            current_usage=current_usage,
            quota_limit=None,
            base_limit=base_limit,
            percentage_used=(current_usage / base_limit * 100) if base_limit > 0 else 0.0,
            remaining=None,
            warning_level="none",
            funding_notice=build_funding_notice(current_usage, base_limit, extensions),
            **session_fields
        )

    async def _resolve_session_notice(
        self,
        user: User,
        tier: QuotaTier,
        limit: float,
        session_id: Optional[str],
        assignment_id: Optional[str],
        session_total_cost: Optional[float] = None,
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

        # A caller that already read the session row this turn can hand us its
        # `totalCost` and save a 62ms GSI query (the preamble does).
        #
        # It supplies the value ONLY when the row actually carries the
        # attribute, and `None` here means "not known" — never "zero". That
        # distinction is the whole safety of this path: `get_session_metadata`
        # lazily **backfills** the cost aggregates for legacy rows written
        # before write-time aggregation, so treating a missing `totalCost` as
        # 0.0 would skip the backfill AND silently stop producing the notice
        # for exactly the long-lived conversations it exists to catch. On
        # `None` we take the original read, backfill and all.
        session_cost: Optional[float] = session_total_cost
        if session_cost is None:
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
