"""Cost aggregator service for user cost summaries and reporting"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
from decimal import Decimal

from .models import UserCostSummary, ModelCostSummary, CostBreakdown
from apis.app_api.storage.metadata_storage import get_metadata_storage

logger = logging.getLogger(__name__)


class CostAggregator:
    """Aggregate costs across sessions and time periods.

    Includes short-lived caching (30 seconds) for quota enforcement
    to reduce DynamoDB calls during burst usage patterns.
    """

    def __init__(self, cache_ttl_seconds: int = 30):
        self.storage = get_metadata_storage()
        self.cache_ttl = cache_ttl_seconds
        # Cache: {cache_key: (UserCostSummary, cached_at)}
        self._cache: Dict[str, Tuple[UserCostSummary, datetime]] = {}

    def _get_cache_key(self, user_id: str, period: str) -> str:
        """Generate cache key for user+period"""
        return f"{user_id}:{period}"

    def _get_cached(self, user_id: str, period: str) -> Optional[UserCostSummary]:
        """Get cached summary if valid"""
        cache_key = self._get_cache_key(user_id, period)
        if cache_key in self._cache:
            summary, cached_at = self._cache[cache_key]
            if datetime.utcnow() - cached_at < timedelta(seconds=self.cache_ttl):
                logger.debug(f"Cost summary cache hit for user {user_id}, period {period}")
                return summary
            else:
                # Expired, remove from cache
                del self._cache[cache_key]
        return None

    def _set_cached(self, user_id: str, period: str, summary: UserCostSummary) -> None:
        """Cache a summary"""
        cache_key = self._get_cache_key(user_id, period)
        self._cache[cache_key] = (summary, datetime.utcnow())
        logger.debug(f"Cost summary cached for user {user_id}, period {period}")

    def invalidate_cache(self, user_id: Optional[str] = None, period: Optional[str] = None) -> None:
        """Invalidate cache for specific user/period or all entries.

        Call this after costs are updated (e.g., after a message completes).
        """
        if user_id and period:
            cache_key = self._get_cache_key(user_id, period)
            if cache_key in self._cache:
                del self._cache[cache_key]
                logger.debug(f"Invalidated cost cache for {user_id}:{period}")
        elif user_id:
            # Remove all entries for this user
            keys_to_remove = [k for k in self._cache.keys() if k.startswith(f"{user_id}:")]
            for key in keys_to_remove:
                del self._cache[key]
            if keys_to_remove:
                logger.debug(f"Invalidated {len(keys_to_remove)} cost cache entries for user {user_id}")
        else:
            # Clear entire cache
            count = len(self._cache)
            self._cache.clear()
            if count:
                logger.debug(f"Invalidated entire cost cache ({count} entries)")

    async def get_user_cost_summary(
        self,
        user_id: str,
        period: str  # e.g., "2025-01" for monthly
    ) -> UserCostSummary:
        """
        Get aggregated cost summary for a user (fast path using pre-aggregated data)

        This method queries the UserCostSummary table for O(1) performance.
        Results are cached for 30 seconds to reduce DynamoDB calls during
        burst usage patterns (e.g., quota checks on every message).

        Args:
            user_id: User identifier
            period: Period identifier (YYYY-MM for monthly)

        Returns:
            UserCostSummary with pre-aggregated costs
        """
        # Check cache first
        cached = self._get_cached(user_id, period)
        if cached is not None:
            return cached

        # Get pre-aggregated summary from storage
        summary = await self.storage.get_user_cost_summary(user_id, period)

        if not summary:
            # No data for this period, return empty summary
            # Cache empty summaries too to avoid repeated DB lookups
            empty_summary = self._create_empty_summary(user_id, period)
            self._set_cached(user_id, period, empty_summary)
            return empty_summary

        # Extract cache token totals
        total_cache_read = summary.get("totalCacheReadTokens", 0)
        total_cache_write = summary.get("totalCacheWriteTokens", 0)

        # Get cache savings - either pre-calculated or compute from tokens
        cache_savings = float(summary.get("cacheSavings", 0.0))

        # Convert to UserCostSummary model
        result = UserCostSummary(
            userId=user_id,
            periodStart=summary["periodStart"],
            periodEnd=summary["periodEnd"],
            totalCost=float(summary["totalCost"]),
            models=self._build_model_summaries(summary.get("modelBreakdown", {})),
            totalRequests=summary["totalRequests"],
            totalInputTokens=summary["totalInputTokens"],
            totalOutputTokens=summary["totalOutputTokens"],
            totalCacheReadTokens=total_cache_read,
            totalCacheWriteTokens=total_cache_write,
            totalCacheSavings=cache_savings
        )

        # Cache the result
        self._set_cached(user_id, period, result)
        return result

    async def get_detailed_cost_report(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> UserCostSummary:
        """
        Get detailed cost report by querying message-level data

        This method queries the MessageMetadata table for detailed breakdowns.
        Use this for custom date ranges or when detailed per-message data is needed.

        Args:
            user_id: User identifier
            start_date: Start of period
            end_date: End of period

        Returns:
            UserCostSummary with detailed aggregations
        """
        # Query message metadata in date range
        messages = await self.storage.get_user_messages_in_range(
            user_id, start_date, end_date
        )

        # Aggregate from message-level data
        total_cost = 0.0
        total_requests = len(messages)
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read_tokens = 0
        total_cache_write_tokens = 0
        total_cache_savings = 0.0

        model_stats = {}

        for message in messages:
            # Extract cost and tokens
            cost = float(message.get("cost", 0.0))
            total_cost += cost

            input_tokens = message.get("inputTokens", 0)
            output_tokens = message.get("outputTokens", 0)
            cache_read_tokens = message.get("cacheReadTokens", 0)
            cache_write_tokens = message.get("cacheWriteTokens", 0)

            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            total_cache_read_tokens += cache_read_tokens
            total_cache_write_tokens += cache_write_tokens

            # Calculate cache savings
            if cache_read_tokens > 0:
                pricing = message.get("pricingSnapshot", {})
                standard_cost = (cache_read_tokens / 1_000_000) * pricing.get("inputPricePerMtok", 0)
                cache_cost = (cache_read_tokens / 1_000_000) * pricing.get("cacheReadPricePerMtok", 0)
                total_cache_savings += (standard_cost - cache_cost)

            # Aggregate per-model
            model_id = message.get("modelId", "unknown")
            if model_id not in model_stats:
                model_stats[model_id] = {
                    "modelName": message.get("modelName", "Unknown"),
                    "provider": message.get("provider", "unknown"),
                    "cost": 0.0,
                    "requests": 0,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0
                }

            stats = model_stats[model_id]
            stats["cost"] += cost
            stats["requests"] += 1
            stats["inputTokens"] += input_tokens
            stats["outputTokens"] += output_tokens
            stats["cacheReadTokens"] += cache_read_tokens
            stats["cacheWriteTokens"] += cache_write_tokens

        # Build model summaries
        models = []
        for model_id, stats in model_stats.items():
            breakdown = CostBreakdown(
                inputCost=0.0,  # TODO: Store breakdown in metadata
                outputCost=0.0,
                cacheReadCost=0.0,
                cacheWriteCost=0.0,
                totalCost=stats["cost"]
            )

            model_summary = ModelCostSummary(
                modelId=model_id,
                modelName=stats["modelName"],
                provider=stats["provider"],
                totalInputTokens=stats["inputTokens"],
                totalOutputTokens=stats["outputTokens"],
                totalCacheReadTokens=stats["cacheReadTokens"],
                totalCacheWriteTokens=stats["cacheWriteTokens"],
                costBreakdown=breakdown,
                requestCount=stats["requests"]
            )
            models.append(model_summary)

        return UserCostSummary(
            userId=user_id,
            periodStart=start_date.isoformat(),
            periodEnd=end_date.isoformat(),
            totalCost=total_cost,
            models=models,
            totalRequests=total_requests,
            totalInputTokens=total_input_tokens,
            totalOutputTokens=total_output_tokens,
            totalCacheReadTokens=total_cache_read_tokens,
            totalCacheWriteTokens=total_cache_write_tokens,
            totalCacheSavings=total_cache_savings
        )

    def _build_model_summaries(self, model_breakdown: dict) -> list:
        """Build ModelCostSummary objects from breakdown dict"""
        models = []
        for model_id, stats in model_breakdown.items():
            breakdown = CostBreakdown(
                inputCost=0.0,  # Stored in summary if needed
                outputCost=0.0,
                cacheReadCost=0.0,
                cacheWriteCost=0.0,
                totalCost=float(stats["cost"])
            )

            models.append(ModelCostSummary(
                modelId=model_id,
                modelName=stats.get("modelName", "Unknown"),
                provider=stats.get("provider", "unknown"),
                totalInputTokens=stats.get("inputTokens", 0),
                totalOutputTokens=stats.get("outputTokens", 0),
                totalCacheReadTokens=stats.get("cacheReadTokens", 0),
                totalCacheWriteTokens=stats.get("cacheWriteTokens", 0),
                costBreakdown=breakdown,
                requestCount=stats.get("requests", 0)
            ))

        return models

    def _create_empty_summary(self, user_id: str, period: str) -> UserCostSummary:
        """Create empty summary for period with no data"""
        # Parse period to get date range
        try:
            year, month = period.split('-')
            # Calculate last day of month
            if month == '12':
                next_month = 1
                next_year = int(year) + 1
            else:
                next_month = int(month) + 1
                next_year = int(year)

            # Get last day by going to first day of next month and subtracting a day
            from calendar import monthrange
            last_day = monthrange(int(year), int(month))[1]

            period_start = f"{year}-{month}-01T00:00:00Z"
            period_end = f"{year}-{month}-{last_day:02d}T23:59:59Z"
        except (ValueError, IndexError):
            # Fallback if period format is invalid
            period_start = f"{period}-01T00:00:00Z"
            period_end = f"{period}-31T23:59:59Z"

        return UserCostSummary(
            userId=user_id,
            periodStart=period_start,
            periodEnd=period_end,
            totalCost=0.0,
            models=[],
            totalRequests=0,
            totalInputTokens=0,
            totalOutputTokens=0,
            totalCacheReadTokens=0,
            totalCacheWriteTokens=0,
            totalCacheSavings=0.0
        )
