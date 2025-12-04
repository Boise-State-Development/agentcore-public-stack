"""Pricing configuration for AWS Bedrock models

This module provides pricing information for cost calculation.

TODO: Future Implementation
- Load pricing from configuration file or database
- Support multiple regions with different pricing
- Track pricing history for accurate historical cost calculation
- Add API to update pricing when AWS changes rates
"""

from typing import Dict, Optional
from datetime import datetime


# Placeholder pricing data (as of January 2025)
# Source: https://aws.amazon.com/bedrock/pricing/
# NOTE: These are example prices and should be verified with current AWS pricing

BEDROCK_PRICING = {
    # Claude 3.5 Sonnet (v2) - Most balanced model
    "claude-sonnet-4-5": {
        "input_price_per_mtok": 3.0,      # $3 per million input tokens
        "output_price_per_mtok": 15.0,    # $15 per million output tokens
        "cache_write_price_per_mtok": 3.75,  # 25% markup for cache writes
        "cache_read_price_per_mtok": 0.30,   # 90% discount for cache reads
    },
    # Claude 3.5 Sonnet (original)
    "claude-3-5-sonnet": {
        "input_price_per_mtok": 3.0,
        "output_price_per_mtok": 15.0,
        "cache_write_price_per_mtok": 3.75,
        "cache_read_price_per_mtok": 0.30,
    },
    # Claude Opus 4 - Most powerful model
    "claude-opus-4": {
        "input_price_per_mtok": 15.0,     # $15 per million input tokens
        "output_price_per_mtok": 75.0,    # $75 per million output tokens
        "cache_write_price_per_mtok": 18.75,  # 25% markup
        "cache_read_price_per_mtok": 1.50,    # 90% discount
    },
    # Claude 3 Opus (previous generation)
    "claude-3-opus": {
        "input_price_per_mtok": 15.0,
        "output_price_per_mtok": 75.0,
        "cache_write_price_per_mtok": 18.75,
        "cache_read_price_per_mtok": 1.50,
    },
    # Claude Haiku 4.5 - Fastest, most cost-effective
    "claude-haiku-4-5": {
        "input_price_per_mtok": 1.0,      # $1 per million input tokens
        "output_price_per_mtok": 5.0,     # $5 per million output tokens
        "cache_write_price_per_mtok": 1.25,   # 25% markup
        "cache_read_price_per_mtok": 0.10,    # 90% discount
    },
    # Claude 3 Haiku (previous generation)
    "claude-3-haiku": {
        "input_price_per_mtok": 0.25,
        "output_price_per_mtok": 1.25,
        "cache_write_price_per_mtok": 0.30,
        "cache_read_price_per_mtok": 0.03,
    },
}


def get_model_pricing(model_id: str) -> Optional[Dict[str, float]]:
    """
    Get pricing information for a model

    Args:
        model_id: Full model identifier (e.g., "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    Returns:
        Dict with pricing info or None if model not found

    Example:
        ```python
        pricing = get_model_pricing("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        # Returns: {
        #     "input_price_per_mtok": 3.0,
        #     "output_price_per_mtok": 15.0,
        #     "cache_write_price_per_mtok": 3.75,
        #     "cache_read_price_per_mtok": 0.30
        # }
        ```
    """
    # Extract model key from full ID
    for key, pricing in BEDROCK_PRICING.items():
        if key in model_id:
            return pricing

    # Return None if model not found (caller should handle)
    return None


def create_pricing_snapshot(model_id: str) -> Optional[Dict[str, any]]:
    """
    Create a pricing snapshot for a model at the current time

    This captures pricing at request time for historical accuracy.
    When AWS changes pricing, historical costs remain accurate.

    Args:
        model_id: Full model identifier

    Returns:
        Dict with pricing snapshot or None if model not found

    Example:
        ```python
        snapshot = create_pricing_snapshot("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        # Returns: {
        #     "input_price_per_mtok": 3.0,
        #     "output_price_per_mtok": 15.0,
        #     "cache_write_price_per_mtok": 3.75,
        #     "cache_read_price_per_mtok": 0.30,
        #     "currency": "USD",
        #     "snapshot_at": "2025-01-15T10:30:00Z"
        # }
        ```
    """
    pricing = get_model_pricing(model_id)
    if not pricing:
        return None

    return {
        **pricing,
        "currency": "USD",
        "snapshot_at": datetime.utcnow().isoformat() + "Z"
    }


# TODO: Future enhancements
# - Load pricing from database or configuration file
# - Support regional pricing (us-east-1 vs eu-west-1)
# - Track pricing history (effective_from, effective_to dates)
# - Add API endpoint to update pricing
# - Support custom pricing for enterprise customers
# - Add pricing for other Bedrock models (Titan, Jurassic, etc.)
