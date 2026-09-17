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

    # Bedrock's cache-write premium for a 1-hour TTL, as a multiple of the
    # model's base input rate (5m is 1.25x — that is what the catalog's
    # cacheWritePricePerMtok already carries). See the prompt-cache contract
    # in CLAUDE.md.
    LONG_TTL_CACHE_WRITE_MULTIPLIER = 2.0

    @staticmethod
    def calculate_message_cost(
        usage: Dict[str, int],
        pricing: Dict[str, float],
        *,
        long_ttl_static_prefix_tokens: Optional[int] = None,
    ) -> Tuple[float, CostBreakdown]:
        """
        Calculate cost for a single message

        Args:
            usage: Token usage dict with inputTokens, outputTokens, etc.
            pricing: Pricing dict with inputPricePerMtok, etc.
            long_ttl_static_prefix_tokens: When the turn ran with the 1h TTL on
                the static (tools + system) cachePoints, the size of that static
                segment. Cache writes that cover it are billed at the 1h premium
                (2x base) instead of the 5m one. Which part of the write was
                static is inferred from the read: a static segment that was
                read (cacheRead >= static) was not written, so the write is all
                history at 5m; otherwise the unread remainder of the static
                segment was written at 1h. ``None`` (the default) prices every
                cache write at the catalog's 5m rate — today's behavior.

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
        # Extract token counts (default to 0 if not present or None)
        input_tokens = usage.get("inputTokens") or 0
        output_tokens = usage.get("outputTokens") or 0
        cache_read_tokens = usage.get("cacheReadInputTokens") or 0
        cache_write_tokens = usage.get("cacheWriteInputTokens") or 0

        # Extract pricing (default to 0 if not present or None)
        # Note: dict.get() with default only handles missing keys, not None values
        input_price = pricing.get("inputPricePerMtok") or 0.0
        output_price = pricing.get("outputPricePerMtok") or 0.0
        cache_read_price = pricing.get("cacheReadPricePerMtok") or 0.0
        cache_write_price = pricing.get("cacheWritePricePerMtok") or 0.0

        # Important: When cache tokens are present, they are NOT included in inputTokens
        # The model returns:
        # - inputTokens: non-cached standard input tokens only
        # - cacheReadInputTokens: tokens read from cache
        # - cacheWriteInputTokens: tokens written to cache
        # Total input = inputTokens + cacheReadInputTokens + cacheWriteInputTokens
        #
        # That is the Bedrock Converse convention. The OpenAI family reports an
        # *inclusive* inputTokens instead, so its usage is rewritten to this
        # shape at the model seam — see apis/shared/models/usage_normalization.py.
        # Feeding raw OpenAI usage in here bills every cached token twice.

        # Calculate costs (per million tokens)
        input_cost = (input_tokens / 1_000_000) * input_price
        output_cost = (output_tokens / 1_000_000) * output_price
        cache_read_cost = (cache_read_tokens / 1_000_000) * cache_read_price

        long_ttl_written = 0
        if long_ttl_static_prefix_tokens and long_ttl_static_prefix_tokens > 0 and cache_write_tokens > 0:
            long_ttl_written = max(0, min(cache_write_tokens, int(long_ttl_static_prefix_tokens) - cache_read_tokens))
        long_ttl_write_price = input_price * CostCalculator.LONG_TTL_CACHE_WRITE_MULTIPLIER
        cache_write_cost = (
            (long_ttl_written / 1_000_000) * long_ttl_write_price
            + ((cache_write_tokens - long_ttl_written) / 1_000_000) * cache_write_price
        )

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
        # Handle None or zero values safely
        if not cache_read_tokens or cache_read_tokens == 0:
            return 0.0

        # Default None prices to 0 to prevent TypeError
        input_price = input_price or 0.0
        cache_read_price = cache_read_price or 0.0

        standard_cost = (cache_read_tokens / 1_000_000) * input_price
        cache_cost = (cache_read_tokens / 1_000_000) * cache_read_price

        return standard_cost - cache_cost

    @staticmethod
    def validate_pricing(pricing: Dict[str, float]) -> bool:
        """
        Validate that pricing dictionary contains required fields with non-None values

        Args:
            pricing: Pricing dictionary to validate

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["inputPricePerMtok", "outputPricePerMtok"]
        return all(
            field in pricing and pricing[field] is not None
            for field in required_fields
        )

    @staticmethod
    def validate_usage(usage: Dict[str, int]) -> bool:
        """
        Validate that usage dictionary contains required fields with non-None values

        Args:
            usage: Usage dictionary to validate

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["inputTokens", "outputTokens"]
        return all(
            field in usage and usage[field] is not None
            for field in required_fields
        )
