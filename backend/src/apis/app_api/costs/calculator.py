"""Cost calculation service for computing message costs from token usage and pricing.

This module handles:
- Cost calculation from token usage and pricing data
- Cache savings calculations (Bedrock only)
- Multi-provider cost support (Bedrock, OpenAI, Gemini)
"""

from typing import Dict, Optional, Tuple
from .models import CostBreakdown


class CostCalculator:
    """Calculate costs from token usage and pricing"""

    @staticmethod
    def calculate_message_cost(
        usage: Dict[str, int],
        pricing: Dict[str, float]
    ) -> Tuple[float, CostBreakdown]:
        """
        Calculate cost for a single message

        Args:
            usage: Token usage dict with inputTokens, outputTokens, etc.
            pricing: Pricing dict with inputPricePerMtok, etc.

        Returns:
            Tuple of (total_cost, cost_breakdown)

        Example:
            ```python
            usage = {
                "inputTokens": 1000,
                "outputTokens": 500,
                "cacheReadInputTokens": 200,
                "cacheWriteInputTokens": 100
            }
            pricing = {
                "inputPricePerMtok": 3.0,
                "outputPricePerMtok": 15.0,
                "cacheReadPricePerMtok": 0.30,
                "cacheWritePricePerMtok": 3.75
            }
            total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)
            # total_cost = 0.010035
            # breakdown.inputCost = 0.0021
            # breakdown.outputCost = 0.0075
            # breakdown.cacheReadCost = 0.00006
            # breakdown.cacheWriteCost = 0.000375
            ```
        """
        # Extract token counts (default to 0 if not present)
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        cache_read_tokens = usage.get("cacheReadInputTokens", 0)
        cache_write_tokens = usage.get("cacheWriteInputTokens", 0)

        # Extract pricing (default to 0 if not present)
        input_price = pricing.get("inputPricePerMtok", 0.0)
        output_price = pricing.get("outputPricePerMtok", 0.0)
        cache_read_price = pricing.get("cacheReadPricePerMtok", 0.0)
        cache_write_price = pricing.get("cacheWritePricePerMtok", 0.0)

        # Important: When cache tokens are present, they are NOT included in inputTokens
        # The model returns:
        # - inputTokens: non-cached standard input tokens only
        # - cacheReadInputTokens: tokens read from cache
        # - cacheWriteInputTokens: tokens written to cache
        # Total input = inputTokens + cacheReadInputTokens + cacheWriteInputTokens

        # Calculate costs (per million tokens)
        input_cost = (input_tokens / 1_000_000) * input_price
        output_cost = (output_tokens / 1_000_000) * output_price
        cache_read_cost = (cache_read_tokens / 1_000_000) * cache_read_price
        cache_write_cost = (cache_write_tokens / 1_000_000) * cache_write_price

        total_cost = input_cost + output_cost + cache_read_cost + cache_write_cost

        breakdown = CostBreakdown(
            inputCost=input_cost,
            outputCost=output_cost,
            cacheReadCost=cache_read_cost,
            cacheWriteCost=cache_write_cost,
            totalCost=total_cost
        )

        return total_cost, breakdown

    @staticmethod
    def calculate_cache_savings(
        cache_read_tokens: int,
        input_price: float,
        cache_read_price: float
    ) -> float:
        """
        Calculate cost savings from cache hits

        Without cache, these tokens would have been charged at input_price.
        With cache, they're charged at cache_read_price.

        Args:
            cache_read_tokens: Number of tokens read from cache
            input_price: Standard input price per million tokens
            cache_read_price: Cache read price per million tokens

        Returns:
            Cost savings in USD

        Example:
            ```python
            # 200 tokens read from cache
            # Standard: $3.00/M, Cache: $0.30/M
            savings = CostCalculator.calculate_cache_savings(200, 3.0, 0.30)
            # savings = 0.00054 (200/1M * (3.0 - 0.30))
            ```
        """
        if cache_read_tokens == 0:
            return 0.0

        standard_cost = (cache_read_tokens / 1_000_000) * input_price
        cache_cost = (cache_read_tokens / 1_000_000) * cache_read_price

        return standard_cost - cache_cost

    @staticmethod
    def validate_pricing(pricing: Dict[str, float]) -> bool:
        """
        Validate that pricing dictionary contains required fields

        Args:
            pricing: Pricing dictionary to validate

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["inputPricePerMtok", "outputPricePerMtok"]
        return all(field in pricing for field in required_fields)

    @staticmethod
    def validate_usage(usage: Dict[str, int]) -> bool:
        """
        Validate that usage dictionary contains required fields

        Args:
            usage: Usage dictionary to validate

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["inputTokens", "outputTokens"]
        return all(field in usage for field in required_fields)
