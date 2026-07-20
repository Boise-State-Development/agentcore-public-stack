"""Admin cost dashboard Pydantic models.

These models define the API response schemas for the admin cost dashboard,
enabling administrators to view system-wide usage metrics, top users by cost,
and cost trends.
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict


class TopUserCost(BaseModel):
    """User cost summary for admin dashboard top users list."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    last_updated: str = Field(..., alias="lastUpdated")

    # Optional enrichment fields
    email: Optional[str] = None
    tier_name: Optional[str] = Field(None, alias="tierName")
    quota_limit: Optional[float] = Field(None, alias="quotaLimit")
    quota_percentage: Optional[float] = Field(None, alias="quotaPercentage")


class ModelBreakdownItem(BaseModel):
    """Model breakdown item within system cost summary."""
    model_config = ConfigDict(populate_by_name=True)

    cost: float
    requests: int


class SystemCostSummary(BaseModel):
    """System-wide cost summary for a period."""
    model_config = ConfigDict(populate_by_name=True)

    period: str  # "2025-01" or "2025-01-15"
    period_type: str = Field(..., alias="periodType")  # "daily" or "monthly"

    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    active_users: int = Field(..., alias="activeUsers")

    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    total_cache_savings: float = Field(0.0, alias="totalCacheSavings")

    model_breakdown: Optional[Dict[str, ModelBreakdownItem]] = Field(
        None,
        alias="modelBreakdown"
    )
    last_updated: str = Field(..., alias="lastUpdated")


class ModelUsageSummary(BaseModel):
    """Per-model usage summary for analytics."""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str

    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    unique_users: int = Field(..., alias="uniqueUsers")
    avg_cost_per_request: float = Field(..., alias="avgCostPerRequest")

    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")


class TierUsageSummary(BaseModel):
    """Per-tier usage summary for quota tier analytics."""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId")
    tier_name: str = Field(..., alias="tierName")

    total_cost: float = Field(..., alias="totalCost")
    total_users: int = Field(..., alias="totalUsers")
    users_at_limit: int = Field(..., alias="usersAtLimit")
    users_warned: int = Field(..., alias="usersWarned")
    avg_utilization: float = Field(..., alias="avgUtilization")


class CostTrend(BaseModel):
    """Cost trend data point for time-series charts."""
    model_config = ConfigDict(populate_by_name=True)

    date: str
    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    active_users: int = Field(..., alias="activeUsers")


class PrefixFingerprints(BaseModel):
    """Prompt-cache prefix hashes for one model call (see PrefixFingerprintHook)."""
    model_config = ConfigDict(populate_by_name=True)

    tool_config_hash: Optional[str] = Field(None, alias="toolConfigHash")
    system_prompt_hash: Optional[str] = Field(None, alias="systemPromptHash")
    history_hash: Optional[str] = Field(None, alias="historyHash")
    message_count: Optional[int] = Field(None, alias="messageCount")


class SessionCallRow(BaseModel):
    """One model call within a session's cost anatomy."""
    model_config = ConfigDict(populate_by_name=True)

    timestamp: str
    message_id: Optional[int] = Field(None, alias="messageId")
    model_id: Optional[str] = Field(None, alias="modelId")

    input_tokens: int = Field(0, alias="inputTokens")
    output_tokens: int = Field(0, alias="outputTokens")
    cache_read_tokens: int = Field(0, alias="cacheReadTokens")
    cache_write_tokens: int = Field(0, alias="cacheWriteTokens")

    cost: float = 0.0
    cache_status: Optional[str] = Field(None, alias="cacheStatus")
    cache_gap_seconds: Optional[int] = Field(None, alias="cacheGapSeconds")
    wasted_usd: float = Field(0.0, alias="wastedUsd")
    prefix_fingerprints: Optional[PrefixFingerprints] = Field(
        None, alias="prefixFingerprints"
    )


class SessionCostAnatomy(BaseModel):
    """Per-call cost anatomy for one session (admin cache-miss forensics)."""
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    calls: List[SessionCallRow]

    total_cost: float = Field(0.0, alias="totalCost")
    total_cache_read_tokens: int = Field(0, alias="totalCacheReadTokens")
    total_cache_write_tokens: int = Field(0, alias="totalCacheWriteTokens")
    avoidable_miss_count: int = Field(0, alias="avoidableMissCount")
    wasted_usd: float = Field(0.0, alias="wastedUsd")
    # cacheRead / (cacheRead + cacheWrite) over the session; None until
    # there has been any cache activity.
    cache_efficiency: Optional[float] = Field(None, alias="cacheEfficiency")


class AdminCostDashboard(BaseModel):
    """Complete admin cost dashboard response combining all metrics."""
    model_config = ConfigDict(populate_by_name=True)

    # Current period summary
    current_period: SystemCostSummary = Field(..., alias="currentPeriod")

    # Top users (configurable limit, default 100)
    top_users: List[TopUserCost] = Field(..., alias="topUsers")

    # Model breakdown
    model_usage: List[ModelUsageSummary] = Field(..., alias="modelUsage")

    # Tier breakdown (optional, if quota system enabled)
    tier_usage: Optional[List[TierUsageSummary]] = Field(None, alias="tierUsage")

    # Historical daily trends (optional)
    daily_trends: Optional[List[CostTrend]] = Field(None, alias="dailyTrends")
