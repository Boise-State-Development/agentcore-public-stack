"""Admin cost dashboard service.

Provides methods for retrieving system-wide cost metrics, top users by cost,
model usage breakdowns, and cost trends for the admin dashboard.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from apis.shared.storage.dynamodb_storage import DynamoDBStorage
from .models import (
    TopUserCost,
    TopSessionCost,
    TopSessionsResponse,
    SystemCostSummary,
    ModelUsageSummary,
    TierUsageSummary,
    CostTrend,
    AdminCostDashboard,
    PrefixFingerprints,
    SessionCallRow,
    SessionCostAnatomy,
)

logger = logging.getLogger(__name__)


class AdminCostService:
    """Service for admin cost dashboard operations."""

    def __init__(self, storage: Optional[DynamoDBStorage] = None):
        """
        Initialize the admin cost service.

        Args:
            storage: Optional DynamoDB storage instance. If not provided,
                     a new instance will be created.
        """
        self.storage = storage or DynamoDBStorage()

    def _get_current_period(self) -> str:
        """Get the current month period in YYYY-MM format."""
        now = datetime.now(timezone.utc)
        return f"{now.year}-{now.month:02d}"

    def _get_current_date(self) -> str:
        """Get the current date in YYYY-MM-DD format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_period_date_range(self, period: str) -> tuple[str, str]:
        """
        Get the start and end dates for a monthly period.

        Args:
            period: Period in YYYY-MM format

        Returns:
            Tuple of (start_date, end_date) in YYYY-MM-DD format
        """
        year, month = map(int, period.split("-"))

        # First day of month
        start_date = f"{year}-{month:02d}-01"

        # Last day of month
        if month == 12:
            next_month_first = datetime(year + 1, 1, 1)
        else:
            next_month_first = datetime(year, month + 1, 1)

        last_day = next_month_first - timedelta(days=1)
        end_date = last_day.strftime("%Y-%m-%d")

        return start_date, end_date

    async def get_top_users(
        self,
        period: Optional[str] = None,
        limit: int = 100,
        min_cost: Optional[float] = None,
        tier_id: Optional[str] = None
    ) -> List[TopUserCost]:
        """
        Get top users by cost for a period.

        Uses the PeriodCostIndex GSI for efficient sorted queries.

        Args:
            period: The billing period (YYYY-MM format). Defaults to current month.
            limit: Maximum number of users to return (1-1000, default 100).
            min_cost: Optional minimum cost threshold in dollars.
            tier_id: Optional tier ID filter (not yet implemented).

        Returns:
            List of TopUserCost sorted by cost descending.
        """
        period = period or self._get_current_period()
        logger.info("Getting top users by cost for period")

        try:
            users_data = await self.storage.get_top_users_by_cost(
                period=period,
                limit=min(limit, 1000),
                min_cost=min_cost
            )

            result = []
            for user_data in users_data:
                result.append(TopUserCost(
                    user_id=user_data.get("userId", ""),
                    total_cost=user_data.get("totalCost", 0.0),
                    total_requests=user_data.get("totalRequests", 0),
                    last_updated=user_data.get("lastUpdated", ""),
                    # TODO: Enrich with email, tier info from user service
                    email=None,
                    tier_name=None,
                    quota_limit=None,
                    quota_percentage=None
                ))

            logger.info("Retrieved top users for period")
            return result

        except Exception as e:
            logger.error(f"Error getting top users: {e}")
            raise

    async def get_system_summary(
        self,
        period: Optional[str] = None,
        period_type: str = "monthly"
    ) -> SystemCostSummary:
        """
        Get system-wide cost summary for a period.

        Uses pre-aggregated rollups from the SystemCostRollup table.

        Args:
            period: The period (YYYY-MM for monthly, YYYY-MM-DD for daily).
                   Defaults to current month/day based on period_type.
            period_type: Either "daily" or "monthly".

        Returns:
            SystemCostSummary with aggregated metrics.
        """
        if period_type == "daily":
            period = period or self._get_current_date()
        else:
            period = period or self._get_current_period()

        logger.info("Getting system summary for period")

        try:
            summary_data = await self.storage.get_system_summary(
                period=period,
                period_type=period_type
            )

            if not summary_data:
                # Return empty summary if no data exists
                logger.warning("No system summary found for period")
                return SystemCostSummary(
                    period=period,
                    period_type=period_type,
                    total_cost=0.0,
                    total_requests=0,
                    active_users=0,
                    total_input_tokens=0,
                    total_output_tokens=0,
                    total_cache_savings=0.0,
                    model_breakdown=None,
                    last_updated=datetime.now(timezone.utc).isoformat()
                )

            return SystemCostSummary(
                period=period,
                period_type=period_type,
                total_cost=summary_data.get("totalCost", 0.0),
                total_requests=summary_data.get("totalRequests", 0),
                active_users=summary_data.get("activeUsers", 0),
                total_input_tokens=summary_data.get("totalInputTokens", 0),
                total_output_tokens=summary_data.get("totalOutputTokens", 0),
                total_cache_savings=summary_data.get("totalCacheSavings", 0.0),
                model_breakdown=summary_data.get("modelBreakdown"),
                last_updated=summary_data.get("lastUpdated", "")
            )

        except Exception as e:
            logger.error(f"Error getting system summary: {e}")
            raise

    async def get_usage_by_model(
        self,
        period: Optional[str] = None
    ) -> List[ModelUsageSummary]:
        """
        Get cost breakdown by model for a period.

        Uses ROLLUP#MODEL items from the SystemCostRollup table.

        Args:
            period: The period (YYYY-MM format). Defaults to current month.

        Returns:
            List of ModelUsageSummary sorted by cost descending.
        """
        period = period or self._get_current_period()
        logger.info("Getting model usage for period")

        try:
            model_data = await self.storage.get_model_usage(period=period)

            result = []
            for model in model_data:
                total_requests = model.get("totalRequests", 0)
                total_cost = model.get("totalCost", 0.0)

                result.append(ModelUsageSummary(
                    model_id=model.get("modelId", ""),
                    model_name=model.get("modelName", ""),
                    provider=model.get("provider", "unknown"),
                    total_cost=total_cost,
                    total_requests=total_requests,
                    unique_users=model.get("uniqueUsers", 0),
                    avg_cost_per_request=(
                        total_cost / total_requests if total_requests > 0 else 0.0
                    ),
                    total_input_tokens=model.get("totalInputTokens", 0),
                    total_output_tokens=model.get("totalOutputTokens", 0)
                ))

            logger.info(f"Retrieved usage for {len(result)} models")
            return result

        except Exception as e:
            logger.error(f"Error getting model usage: {e}")
            raise

    async def get_usage_by_tier(
        self,
        period: Optional[str] = None
    ) -> List[TierUsageSummary]:
        """
        Get cost breakdown by quota tier for a period.

        Note: This is a placeholder for future implementation.
        Tier usage statistics require integration with the quota system.

        Args:
            period: The period (YYYY-MM format). Defaults to current month.

        Returns:
            List of TierUsageSummary (currently empty, placeholder).
        """
        _ = period or self._get_current_period()  # TODO: use once tier aggregation is implemented
        logger.info("Getting tier usage for period")

        # TODO: Implement tier usage aggregation
        # This requires:
        # 1. ROLLUP#TIER items in SystemCostRollup table
        # 2. Integration with QuotaRepository to get tier definitions
        # 3. Aggregating user costs by their assigned tiers

        return []

    async def get_daily_trends(
        self,
        start_date: str,
        end_date: str
    ) -> List[CostTrend]:
        """
        Get daily cost trends for a date range.

        Uses ROLLUP#DAILY items from the SystemCostRollup table.

        Args:
            start_date: Start date (YYYY-MM-DD format).
            end_date: End date (YYYY-MM-DD format).
                     Max range: 90 days.

        Returns:
            List of CostTrend sorted by date ascending.
        """
        logger.info("Getting daily trends for date range")

        # Validate date range (max 90 days)
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            if (end - start).days > 90:
                logger.warning("Date range exceeds 90 days, limiting to 90 days")
                end = start + timedelta(days=90)
                end_date = end.strftime("%Y-%m-%d")
        except ValueError as e:
            logger.error(f"Invalid date format: {e}")
            raise ValueError("Dates must be in YYYY-MM-DD format")

        try:
            trends_data = await self.storage.get_daily_trends(
                start_date=start_date,
                end_date=end_date
            )

            result = []
            for trend in trends_data:
                result.append(CostTrend(
                    date=trend.get("date", ""),
                    total_cost=trend.get("totalCost", 0.0),
                    total_requests=trend.get("totalRequests", 0),
                    active_users=trend.get("activeUsers", 0)
                ))

            logger.info(f"Retrieved {len(result)} daily trend data points")
            return result

        except Exception as e:
            logger.error(f"Error getting daily trends: {e}")
            raise

    async def get_top_sessions(
        self,
        period: Optional[str] = None,
        limit: int = 25,
        users_to_scan: int = 50,
        min_cost: Optional[float] = None,
    ) -> TopSessionsResponse:
        """
        Get the most expensive conversations for a period.

        Support's counterpart to the per-session notice the user now sees:
        spot a runaway conversation before the user calls (#833 PR-5).

        **How it is assembled, and why not a scan.** There is no index on
        session cost, and adding a GSI to sessions-metadata is a deploy
        hazard for one admin view. Instead this walks the period's top-cost
        users (``PeriodCostIndex``, already sorted) and queries each one's
        session rows — bounded, index-backed, and correct for the question
        being asked: a session can only be expensive if its owner is. The
        response says how many users were scanned and whether more had cost,
        so a truncated list never reads as "these are all of them".

        Args:
            period: Billing period (YYYY-MM). Defaults to current month.
            limit: Maximum sessions to return.
            users_to_scan: How many top-cost users to fan out over.
            min_cost: Optional floor on a session's lifetime cost.
        """
        period = period or self._get_current_period()
        period_start, _ = self._get_period_date_range(period)

        top_users = await self.storage.get_top_users_by_cost(
            period=period,
            limit=users_to_scan + 1,
        )
        truncated = len(top_users) > users_to_scan
        top_users = top_users[:users_to_scan]

        rows: List[TopSessionCost] = []
        for user_data in top_users:
            user_id = user_data.get("userId")
            if not user_id:
                continue
            user_period_cost = float(user_data.get("totalCost") or 0.0)

            try:
                sessions = await self.storage.get_user_session_costs(
                    user_id=user_id,
                    active_since=period_start,
                )
            except Exception as e:
                # One unreadable user must not empty the whole list.
                logger.warning(f"Skipping sessions for a user in top-sessions: {e}")
                continue

            for session in sessions:
                total_cost = session.get("totalCost")
                if total_cost is None:
                    # Legacy row whose aggregates have never been backfilled —
                    # it is not zero-cost, it is unknown, so say nothing.
                    continue
                total_cost = float(total_cost)
                if min_cost is not None and total_cost < min_cost:
                    continue

                partial_usd = session.get("partialMissUsd")
                rows.append(TopSessionCost(
                    session_id=session.get("sessionId", ""),
                    user_id=user_id,
                    title=session.get("title"),
                    total_cost=round(total_cost, 6),
                    last_message_at=session.get("lastMessageAt"),
                    created_at=session.get("createdAt"),
                    message_count=(
                        int(session["messageCount"])
                        if session.get("messageCount") is not None else None
                    ),
                    last_context_tokens=(
                        int(session["lastContextTokens"])
                        if session.get("lastContextTokens") is not None else None
                    ),
                    partial_miss_count=(
                        int(session["partialMissCount"])
                        if session.get("partialMissCount") is not None else None
                    ),
                    partial_miss_usd=(
                        round(float(partial_usd), 6) if partial_usd is not None else None
                    ),
                    user_period_cost=round(user_period_cost, 6),
                    share_of_user_period=(
                        round(total_cost / user_period_cost * 100, 2)
                        if user_period_cost > 0 else None
                    ),
                ))

        rows.sort(key=lambda r: r.total_cost, reverse=True)
        logger.info(
            f"Top sessions for period: {len(rows)} candidates across "
            f"{len(top_users)} users, returning {min(limit, len(rows))}"
        )

        return TopSessionsResponse(
            period=period,
            sessions=rows[:limit],
            users_scanned=len(top_users),
            truncated=truncated,
        )

    async def get_session_cost_anatomy(self, session_id: str) -> SessionCostAnatomy:
        """
        Get the per-model-call cost anatomy for one session.

        Reads every C# cost record for the session (chronological) and maps
        each to a SessionCallRow with token splits, cost, derived cacheStatus
        (including `partial_miss` — a call that read a leading segment and
        re-wrote the rest of the prefix, which costs like a miss and used to
        be reported as a hit),
        and the prompt-cache prefix fingerprints — the data needed to see
        where a session's spend went and which prefix component broke the
        cache on a miss. Rows written before this feature shipped simply lack
        cacheStatus/fingerprints and render as nulls.

        Args:
            session_id: Session identifier (any user's — admin scope).

        Returns:
            SessionCostAnatomy with per-call rows and session-level rollups.
        """
        records = await self.storage.get_session_cost_records(session_id)

        calls: List[SessionCallRow] = []
        total_cost = 0.0
        total_cache_read = 0
        total_cache_write = 0
        avoidable_misses = 0
        partial_misses = 0
        partial_miss_usd = 0.0
        wasted_usd = 0.0
        agent_switch_misses = 0
        agent_switch_usd = 0.0

        for record in records:
            token_usage = record.get("tokenUsage") or {}
            model_info = record.get("modelInfo") or {}
            fingerprints_raw = record.get("prefixFingerprints")

            # cost is a breakdown dict ({"total": ...}) on the streaming path
            # or a bare float on the legacy path.
            cost_raw = record.get("cost")
            if isinstance(cost_raw, dict):
                cost_raw = cost_raw.get("total")
            try:
                cost = float(cost_raw) if cost_raw is not None else 0.0
            except (TypeError, ValueError):
                cost = 0.0

            cache_read = int(token_usage.get("cacheReadInputTokens") or 0)
            cache_write = int(token_usage.get("cacheWriteInputTokens") or 0)
            cache_status = record.get("cacheStatus")
            row_wasted = float(record.get("wastedUsd") or 0.0)
            # #756 — derived at write time, where the predecessor row was already
            # in hand; read here as a plain projection.
            agent_switched = bool(record.get("agentSwitched"))

            total_cost += cost
            total_cache_read += cache_read
            total_cache_write += cache_write
            if cache_status == "miss_avoidable":
                avoidable_misses += 1
                # A split of the totals, never a deduction from them.
                if agent_switched:
                    agent_switch_misses += 1
                    agent_switch_usd += row_wasted
            elif cache_status == "partial_miss":
                partial_misses += 1
                partial_miss_usd += row_wasted
            wasted_usd += row_wasted

            gap_raw = record.get("cacheGapSeconds")
            prefix_gap_raw = record.get("cachePrefixGapSeconds")
            calls.append(SessionCallRow(
                timestamp=record.get("timestamp", ""),
                message_id=record.get("messageId"),
                model_id=model_info.get("modelId"),
                input_tokens=int(token_usage.get("inputTokens") or 0),
                output_tokens=int(token_usage.get("outputTokens") or 0),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost=cost,
                cache_status=cache_status,
                cache_gap_seconds=int(gap_raw) if gap_raw is not None else None,
                cache_prefix_gap_seconds=(
                    int(prefix_gap_raw) if prefix_gap_raw is not None else None
                ),
                wasted_usd=row_wasted,
                turn_agent_id=record.get("turnAgentId"),
                agent_switched=agent_switched,
                prefix_fingerprints=(
                    PrefixFingerprints(**fingerprints_raw)
                    if isinstance(fingerprints_raw, dict) else None
                ),
            ))

        cache_traffic = total_cache_read + total_cache_write
        cache_efficiency = (
            total_cache_read / cache_traffic if cache_traffic > 0 else None
        )

        logger.info(
            f"Session cost anatomy: {len(calls)} calls, "
            f"{avoidable_misses} avoidable misses, {partial_misses} partial misses, "
            f"wasted=${wasted_usd:.4f}"
        )

        return SessionCostAnatomy(
            session_id=session_id,
            calls=calls,
            total_cost=round(total_cost, 6),
            total_cache_read_tokens=total_cache_read,
            total_cache_write_tokens=total_cache_write,
            avoidable_miss_count=avoidable_misses,
            partial_miss_count=partial_misses,
            partial_miss_usd=round(partial_miss_usd, 6),
            wasted_usd=round(wasted_usd, 6),
            agent_switch_miss_count=agent_switch_misses,
            agent_switch_usd=round(agent_switch_usd, 6),
            cache_efficiency=cache_efficiency,
        )

    async def get_dashboard(
        self,
        period: Optional[str] = None,
        top_users_limit: int = 100,
        include_trends: bool = True
    ) -> AdminCostDashboard:
        """
        Get complete admin cost dashboard with all metrics.

        This is the main entry point for the dashboard, combining:
        - System-wide cost summary
        - Top N users by cost
        - Model usage breakdown
        - Daily trends (optional)

        Args:
            period: The billing period (YYYY-MM format). Defaults to current month.
            top_users_limit: Number of top users to include (1-1000, default 100).
            include_trends: Whether to include daily trends for the period.

        Returns:
            AdminCostDashboard with all dashboard components.
        """
        period = period or self._get_current_period()
        logger.info(
            "Building admin cost dashboard for period"
        )

        # Get system summary
        current_period = await self.get_system_summary(
            period=period,
            period_type="monthly"
        )

        # Get top users
        top_users = await self.get_top_users(
            period=period,
            limit=top_users_limit
        )

        # Get model usage
        model_usage = await self.get_usage_by_model(period=period)

        # Get daily trends if requested
        daily_trends = None
        if include_trends:
            start_date, end_date = self._get_period_date_range(period)
            # Limit end_date to today if period is current month
            today = self._get_current_date()
            if end_date > today:
                end_date = today
            daily_trends = await self.get_daily_trends(start_date, end_date)

        # TODO: Get tier usage when implemented
        tier_usage = None

        return AdminCostDashboard(
            current_period=current_period,
            top_users=top_users,
            model_usage=model_usage,
            tier_usage=tier_usage,
            daily_trends=daily_trends
        )
