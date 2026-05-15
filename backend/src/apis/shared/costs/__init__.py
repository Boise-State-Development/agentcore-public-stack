"""Shared cost tracking and calculation services.

This module handles:
- Cost calculation from token usage and model info
- Pricing configuration management
- Cost data models

Note: pricing_config functions are not eagerly imported here because
they trigger boto3 resource initialization at import time. Import them
directly when needed:
    from apis.shared.costs.pricing_config import get_model_pricing, create_pricing_snapshot
"""

from .models import CostBreakdown, ModelCostSummary, UserCostSummary
from .calculator import CostCalculator

__all__ = [
    "CostBreakdown",
    "ModelCostSummary",
    "UserCostSummary",
    "CostCalculator",
]
