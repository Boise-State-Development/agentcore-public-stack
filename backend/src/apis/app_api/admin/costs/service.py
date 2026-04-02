"""Admin cost dashboard service.

Provides methods for retrieving system-wide cost metrics, top users by cost,
model usage breakdowns, and cost trends for the admin dashboard.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from apis.app_api.storage.dynamodb_storage import DynamoDBStorage
from .models import (
    TopUserCost,
    SystemCostSummary,
    ModelUsageSummary,
    TierUsageSummary,
    CostTrend,
    AdminCostDashboard,
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
        _period = period or self._get_current_period()  # TODO: use once tier aggregation is implemented
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
