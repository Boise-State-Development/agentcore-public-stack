"""Pricing service for retrieving model pricing from managed models database.

This module provides pricing information for cost calculation by reading from
the managed models database instead of hardcoded values.

Architecture:
- Reads pricing from managed models (local files or DynamoDB)
- Creates pricing snapshots for historical accuracy
- Supports multi-provider pricing (Bedrock, OpenAI, Gemini)
"""

import logging
from typing import Dict, Optional
from datetime import datetime, timezone

from apis.app_api.admin.services.managed_models import list_managed_models

logger = logging.getLogger(__name__)


async def get_model_by_model_id(model_id: str) -> Optional[Dict[str, any]]:
    """
    Get a managed model by its model_id

    Args:
        model_id: Full model identifier (e.g., "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    Returns:
        ManagedModel if found, None otherwise
    """
    # Get all managed models (no role filtering, we want admin view)
    models = await list_managed_models(user_roles=None)

    # Find model with matching model_id
    for model in models:
        if model.model_id == model_id:
            return model

    logger.warning(f"No managed model found for model_id: {model_id}")
    return None


async def get_model_pricing(model_id: str) -> Optional[Dict[str, float]]:
    """
    Get pricing information for a model from managed models database

    Args:
        model_id: Full model identifier (e.g., "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    Returns:
        Dict with pricing info or None if model not found

    Example:
        ```python
        pricing = await get_model_pricing("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        # Returns: {
        #     "inputPricePerMtok": 3.0,
        #     "outputPricePerMtok": 15.0,
        #     "cacheWritePricePerMtok": 3.75,
        #     "cacheReadPricePerMtok": 0.30
        # }
        ```
    """
    model = await get_model_by_model_id(model_id)
    if not model:
        return None

    # Build pricing dict from model
    pricing = {
        "inputPricePerMtok": model.input_price_per_million_tokens,
        "outputPricePerMtok": model.output_price_per_million_tokens,
    }

    # Add cache pricing if available (Bedrock only)
    if model.cache_write_price_per_million_tokens is not None:
        pricing["cacheWritePricePerMtok"] = model.cache_write_price_per_million_tokens
    if model.cache_read_price_per_million_tokens is not None:
        pricing["cacheReadPricePerMtok"] = model.cache_read_price_per_million_tokens

    return pricing


async def create_pricing_snapshot(model_id: str) -> Optional[Dict[str, any]]:
    """
    Create a pricing snapshot for a model at the current time

    This captures pricing at request time for historical accuracy.
    When pricing changes, historical costs remain accurate.

    Args:
        model_id: Full model identifier

    Returns:
        Dict with pricing snapshot or None if model not found

    Example:
        ```python
        snapshot = await create_pricing_snapshot("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        # Returns: {
        #     "inputPricePerMtok": 3.0,
        #     "outputPricePerMtok": 15.0,
        #     "cacheWritePricePerMtok": 3.75,
        #     "cacheReadPricePerMtok": 0.30,
        #     "currency": "USD",
        #     "snapshotAt": "2025-01-15T10:30:00Z"
        # }
        ```
    """
    pricing = await get_model_pricing(model_id)
    if not pricing:
        return None

    return {
        **pricing,
        "currency": "USD",
        "snapshotAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }
