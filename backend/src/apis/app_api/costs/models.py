"""Cost tracking data models for user cost aggregation and reporting."""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional


class CostBreakdown(BaseModel):
    """Detailed cost breakdown by token type"""
    model_config = ConfigDict(populate_by_name=True)

    input_cost: float = Field(..., alias="inputCost", description="Cost from input tokens")
    output_cost: float = Field(..., alias="outputCost", description="Cost from output tokens")
    cache_write_cost: float = Field(0.0, alias="cacheWriteCost", description="Cost from cache writes")
    cache_read_cost: float = Field(0.0, alias="cacheReadCost", description="Cost from cache reads")
    total_cost: float = Field(..., alias="totalCost", description="Total cost (sum of all)")


class ModelCostSummary(BaseModel):
    """Cost summary for a specific model"""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str

    # Token usage
    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    total_cache_read_tokens: int = Field(0, alias="totalCacheReadTokens")
    total_cache_write_tokens: int = Field(0, alias="totalCacheWriteTokens")

    # Cost
    cost_breakdown: CostBreakdown = Field(..., alias="costBreakdown")

    # Stats
    request_count: int = Field(..., alias="requestCount", description="Number of requests using this model")


class UserCostSummary(BaseModel):
    """Aggregated cost summary for a user"""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")

    # Time range
    period_start: str = Field(..., alias="periodStart", description="ISO timestamp of period start")
    period_end: str = Field(..., alias="periodEnd", description="ISO timestamp of period end")

    # Aggregate costs
    total_cost: float = Field(..., alias="totalCost", description="Total cost across all models")

    # Per-model breakdown
    models: list[ModelCostSummary] = Field(
        default_factory=list,
        description="Cost breakdown by model"
    )

    # Overall token usage
    total_requests: int = Field(..., alias="totalRequests")
    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    total_cache_savings: float = Field(
        0.0,
        alias="totalCacheSavings",
        description="Total cost saved from cache hits"
    )
