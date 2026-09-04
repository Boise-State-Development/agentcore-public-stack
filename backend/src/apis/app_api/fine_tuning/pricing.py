"""SageMaker instance pricing and capability map for fine-tuning.

Rates are **us-west-2 on-demand USD/hour**, taken from the AWS Price List API
(``aws pricing get-products --service-code AmazonSageMaker``), filtered to the
``Training`` and ``BatchTransform`` components.  They are not from the pricing
web page, which rounds and lags.

Two things this module gets right that a single flat map could not:

1. **Training and Batch Transform are priced separately.**  They agree across
   the g5 family but diverge on g6e (e.g. ml.g6e.24xlarge is $18.83/hr to
   train and $18.8319875/hr to transform), and some instances are offered for
   one and not the other at all.
2. **Not every training instance can run Batch Transform.**  ml.p4d.24xlarge
   and ml.p5.48xlarge publish a Training rate and no BatchTransform rate — an
   inference job on one is rejected by AWS.  Absence from
   :data:`TRANSFORM_COST_PER_HOUR` is how we refuse it before billing.

``ml.p3.*`` is deliberately absent: the Price List API returns no on-demand
SageMaker rate for that family in us-west-2 at all, so the three entries this
map used to carry priced instances a job could never actually provision.
``ml.p4d.24xlarge`` and ``ml.p5.48xlarge`` are also left out — they train but
cannot Batch Transform, so offering them would let a researcher fine-tune a
model they then have no way to run inference on.  Add them only alongside a
non-Batch-Transform inference path.

Pricing accuracy is now load-bearing rather than cosmetic: the monthly quota
is denominated in dollars, so a wrong rate does not just misreport spend, it
mis-enforces the budget.  Re-run ``backend/scripts/refresh_instance_pricing.py`` to
resync after an AWS price change.
"""

from typing import Dict, Optional, Tuple


# =========================================================================
# Training rates (USD/hour, us-west-2 on-demand)
# =========================================================================

TRAINING_COST_PER_HOUR: Dict[str, float] = {
    # --- G5 (NVIDIA A10G, 24GB) ---
    "ml.g5.xlarge": 1.408,
    "ml.g5.2xlarge": 1.515,
    "ml.g5.4xlarge": 2.03,
    "ml.g5.8xlarge": 3.06,
    "ml.g5.12xlarge": 7.09,
    "ml.g5.16xlarge": 5.12,
    "ml.g5.24xlarge": 10.18,
    "ml.g5.48xlarge": 20.36,
    # --- G6 (NVIDIA L4, 24GB) — newer and cheaper than G5 at every size ---
    "ml.g6.xlarge": 1.127,
    "ml.g6.2xlarge": 1.222,
    "ml.g6.4xlarge": 1.654,
    "ml.g6.8xlarge": 2.518,
    "ml.g6.12xlarge": 5.752,
    "ml.g6.24xlarge": 8.344,
    "ml.g6.48xlarge": 16.688,
    # --- G6e (NVIDIA L40S, 48GB) — the memory headroom vision models want ---
    "ml.g6e.xlarge": 2.61,
    "ml.g6e.2xlarge": 2.8,
    "ml.g6e.4xlarge": 3.76,
    "ml.g6e.8xlarge": 5.66,
    "ml.g6e.12xlarge": 13.12,
    "ml.g6e.24xlarge": 18.83,
    "ml.g6e.48xlarge": 37.66,
}


# =========================================================================
# Batch Transform rates (USD/hour, us-west-2 on-demand)
# =========================================================================

TRANSFORM_COST_PER_HOUR: Dict[str, float] = {
    "ml.g5.xlarge": 1.408,
    "ml.g5.2xlarge": 1.515,
    "ml.g5.4xlarge": 2.03,
    "ml.g5.8xlarge": 3.06,
    "ml.g5.12xlarge": 7.09,
    "ml.g5.16xlarge": 5.12,
    "ml.g5.24xlarge": 10.18,
    "ml.g5.48xlarge": 20.36,
    "ml.g6.xlarge": 1.1267,
    "ml.g6.2xlarge": 1.222,
    "ml.g6.4xlarge": 1.654,
    "ml.g6.8xlarge": 2.518,
    "ml.g6.12xlarge": 5.752,
    "ml.g6.24xlarge": 8.344,
    "ml.g6.48xlarge": 16.688,
    "ml.g6e.xlarge": 2.6054,
    "ml.g6e.2xlarge": 2.8026,
    "ml.g6e.4xlarge": 3.7553,
    "ml.g6e.8xlarge": 5.6607,
    "ml.g6e.12xlarge": 13.1158,
    "ml.g6e.24xlarge": 18.8319875,
    "ml.g6e.48xlarge": 37.663975,
}


# =========================================================================
# Accelerator memory (GB of GPU VRAM per instance, summed across GPUs)
# =========================================================================

# Used to warn a user before they submit a model that cannot fit, rather than
# letting them discover it as a CUDA OOM several billed minutes in.
ACCELERATOR_MEMORY_GB: Dict[str, int] = {
    "ml.g5.xlarge": 24, "ml.g5.2xlarge": 24, "ml.g5.4xlarge": 24,
    "ml.g5.8xlarge": 24, "ml.g5.16xlarge": 24,
    "ml.g5.12xlarge": 96, "ml.g5.24xlarge": 96, "ml.g5.48xlarge": 192,
    "ml.g6.xlarge": 24, "ml.g6.2xlarge": 24, "ml.g6.4xlarge": 24,
    "ml.g6.8xlarge": 24, "ml.g6.16xlarge": 24,
    "ml.g6.12xlarge": 96, "ml.g6.24xlarge": 96, "ml.g6.48xlarge": 192,
    "ml.g6e.xlarge": 48, "ml.g6e.2xlarge": 48, "ml.g6e.4xlarge": 48,
    "ml.g6e.8xlarge": 48,
    "ml.g6e.12xlarge": 192, "ml.g6e.24xlarge": 192, "ml.g6e.48xlarge": 384,
}


# =========================================================================
# Lookups
# =========================================================================

def training_rate(instance_type: str) -> Optional[float]:
    """USD/hour to train on ``instance_type``, or None if we have no rate."""
    return TRAINING_COST_PER_HOUR.get(instance_type)


def transform_rate(instance_type: str) -> Optional[float]:
    """USD/hour to Batch Transform on ``instance_type``, or None if unsupported."""
    return TRANSFORM_COST_PER_HOUR.get(instance_type)


def calculate_cost(
    instance_type: str, billable_seconds: int, *, transform: bool = False
) -> float:
    """Cost in USD for ``billable_seconds`` on ``instance_type``.

    Returns 0.0 for an instance we have no rate for.  Callers must not rely on
    that to mean "free" — validate the instance up front instead; a silent
    0.0 is exactly the blind spot that lets unpriced GPU time go unbilled.
    """
    rate = transform_rate(instance_type) if transform else training_rate(instance_type)
    return round((rate or 0.0) * (billable_seconds / 3600), 4)


def estimate_max_cost(
    instance_type: str, max_runtime_seconds: int, *, transform: bool = False
) -> float:
    """Worst-case cost if a job runs to its full ``max_runtime_seconds``.

    This is what the dollar quota reserves against at submission time: the
    actual bill is only known when the job stops, so admitting a job on its
    *current* spend would let a single long run overshoot the budget.
    """
    return calculate_cost(instance_type, max_runtime_seconds, transform=transform)


def supported_training_instances() -> Tuple[str, ...]:
    """Instance types we can price for training, cheapest first."""
    return tuple(sorted(TRAINING_COST_PER_HOUR, key=lambda i: TRAINING_COST_PER_HOUR[i]))


def supported_transform_instances() -> Tuple[str, ...]:
    """Instance types we can price for Batch Transform, cheapest first."""
    return tuple(sorted(TRANSFORM_COST_PER_HOUR, key=lambda i: TRANSFORM_COST_PER_HOUR[i]))
