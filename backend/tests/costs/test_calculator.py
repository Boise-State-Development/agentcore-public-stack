"""Unit tests for CostCalculator — the source-of-truth for all USD math.

These tests pin the per-bucket pricing formula, the cache-savings derivation,
and the input-validation predicates. The aggregator and storage tests cover
this code transitively, but only through mocks; this module is the only
place the math itself is asserted directly.

Conventions for cases:
  - "Sonnet 4.5 pricing" reflects Bedrock's published rates so a regression
    in the formula would be visible in dollar terms a reader can sanity-check.
  - Floats are compared with ``pytest.approx`` to avoid 1e-15 drift.
"""

import pytest

from apis.shared.costs.calculator import CostCalculator
from apis.shared.costs.models import CostBreakdown


# Bedrock rates for Claude Sonnet 4.5 ($/Mtok). Used as the "realistic"
# baseline so dollar amounts in tests can be compared to a published source.
SONNET_45_PRICING = {
    "inputPricePerMtok": 3.0,
    "outputPricePerMtok": 15.0,
    "cacheWritePricePerMtok": 3.75,
    "cacheReadPricePerMtok": 0.30,
}


class TestCalculateMessageCostBasic:
    """Core formula: per-bucket pricing summed into total."""

    def test_input_only(self):
        usage = {"inputTokens": 1_000_000, "outputTokens": 0}
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        assert total == pytest.approx(3.0)
        assert breakdown.input_cost == pytest.approx(3.0)
        assert breakdown.output_cost == 0.0
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0

    def test_output_only(self):
        usage = {"inputTokens": 0, "outputTokens": 1_000_000}
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        assert total == pytest.approx(15.0)
        assert breakdown.output_cost == pytest.approx(15.0)
        assert breakdown.input_cost == 0.0

    def test_input_and_output_no_cache(self):
        """Realistic short turn: 1k input + 500 output on Sonnet 4.5."""
        usage = {"inputTokens": 1_000, "outputTokens": 500}
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        # 1000/1M * 3.00 + 500/1M * 15.00 = 0.003 + 0.0075 = 0.0105
        assert total == pytest.approx(0.0105)
        assert breakdown.input_cost == pytest.approx(0.003)
        assert breakdown.output_cost == pytest.approx(0.0075)

    def test_breakdown_components_sum_to_total(self):
        """The total in the breakdown must equal the sum of its parts."""
        usage = {
            "inputTokens": 1_234,
            "outputTokens": 567,
            "cacheReadInputTokens": 8_910,
            "cacheWriteInputTokens": 2_345,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        component_sum = (
            breakdown.input_cost
            + breakdown.output_cost
            + breakdown.cache_read_cost
            + breakdown.cache_write_cost
        )
        assert breakdown.total_cost == pytest.approx(component_sum)
        assert total == pytest.approx(component_sum)


class TestCalculateMessageCostWithCache:
    """Cache buckets price separately and add to the total."""

    def test_cache_read_only(self):
        """A subsequent turn hitting the prompt cache."""
        usage = {
            "inputTokens": 100,            # uncached suffix
            "outputTokens": 200,
            "cacheReadInputTokens": 5_000, # cached prefix
            "cacheWriteInputTokens": 0,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        # input: 100/1M * 3 = 0.0003
        # output: 200/1M * 15 = 0.003
        # cache_read: 5000/1M * 0.30 = 0.0015
        assert breakdown.input_cost == pytest.approx(0.0003)
        assert breakdown.output_cost == pytest.approx(0.003)
        assert breakdown.cache_read_cost == pytest.approx(0.0015)
        assert breakdown.cache_write_cost == 0.0
        assert total == pytest.approx(0.0048)

    def test_cache_write_only(self):
        """The first turn that establishes the cache pays the write premium."""
        usage = {
            "inputTokens": 0,
            "outputTokens": 100,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 5_000,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        # cache_write: 5000/1M * 3.75 = 0.01875
        # output: 100/1M * 15 = 0.0015
        assert breakdown.cache_write_cost == pytest.approx(0.01875)
        assert breakdown.output_cost == pytest.approx(0.0015)
        assert breakdown.cache_read_cost == 0.0
        assert total == pytest.approx(0.02025)

    def test_cache_read_and_write_mixed(self):
        """A turn that hits part of the cache and writes a new section."""
        usage = {
            "inputTokens": 200,
            "outputTokens": 300,
            "cacheReadInputTokens": 10_000,
            "cacheWriteInputTokens": 2_000,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        assert breakdown.input_cost == pytest.approx(200 / 1_000_000 * 3.0)
        assert breakdown.output_cost == pytest.approx(300 / 1_000_000 * 15.0)
        assert breakdown.cache_read_cost == pytest.approx(10_000 / 1_000_000 * 0.30)
        assert breakdown.cache_write_cost == pytest.approx(2_000 / 1_000_000 * 3.75)
        assert total == pytest.approx(
            breakdown.input_cost
            + breakdown.output_cost
            + breakdown.cache_read_cost
            + breakdown.cache_write_cost
        )

    def test_docstring_example_holds(self):
        """The docstring example must match the implementation."""
        usage = {
            "inputTokens": 1_000,
            "outputTokens": 500,
            "cacheReadInputTokens": 200,
            "cacheWriteInputTokens": 100,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        assert breakdown.input_cost == pytest.approx(0.003)
        assert breakdown.output_cost == pytest.approx(0.0075)
        assert breakdown.cache_read_cost == pytest.approx(0.00006)
        assert breakdown.cache_write_cost == pytest.approx(0.000375)
        assert total == pytest.approx(0.010935)


class TestCalculateMessageCostDefensive:
    """Missing or None fields should degrade to 0, never raise."""

    def test_missing_pricing_fields_default_to_zero(self):
        """Cache prices may be absent for non-Bedrock providers."""
        pricing = {"inputPricePerMtok": 1.0, "outputPricePerMtok": 2.0}
        usage = {
            "inputTokens": 1_000_000,
            "outputTokens": 1_000_000,
            "cacheReadInputTokens": 1_000_000,
            "cacheWriteInputTokens": 1_000_000,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, pricing)
        assert breakdown.input_cost == pytest.approx(1.0)
        assert breakdown.output_cost == pytest.approx(2.0)
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0
        assert total == pytest.approx(3.0)

    def test_none_pricing_values_default_to_zero(self):
        """A managed-model row with explicit None for cache prices must not raise."""
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0,
            "cacheReadPricePerMtok": None,
            "cacheWritePricePerMtok": None,
        }
        usage = {
            "inputTokens": 1_000,
            "outputTokens": 500,
            "cacheReadInputTokens": 1_000,
            "cacheWriteInputTokens": 1_000,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, pricing)
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0

    def test_none_usage_values_default_to_zero(self):
        usage = {
            "inputTokens": None,
            "outputTokens": None,
            "cacheReadInputTokens": None,
            "cacheWriteInputTokens": None,
        }
        total, breakdown = CostCalculator.calculate_message_cost(usage, SONNET_45_PRICING)
        assert total == 0.0
        assert breakdown.input_cost == 0.0
        assert breakdown.output_cost == 0.0
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0

    def test_empty_usage_and_pricing(self):
        total, breakdown = CostCalculator.calculate_message_cost({}, {})
        assert total == 0.0
        assert isinstance(breakdown, CostBreakdown)


class TestCalculateCacheSavings:
    """Cache savings = (input_price - cache_read_price) * read_tokens / 1M."""

    def test_typical_savings(self):
        """200 read tokens at Sonnet 4.5 rates."""
        savings = CostCalculator.calculate_cache_savings(200, 3.0, 0.30)
        # standard: 200/1M * 3 = 0.0006; cached: 200/1M * 0.30 = 0.00006
        assert savings == pytest.approx(0.00054)

    def test_zero_reads_returns_zero(self):
        assert CostCalculator.calculate_cache_savings(0, 3.0, 0.30) == 0.0

    def test_none_reads_returns_zero(self):
        """``None`` is the realistic shape from a model that didn't hit cache."""
        assert CostCalculator.calculate_cache_savings(None, 3.0, 0.30) == 0.0

    def test_none_prices_default_to_zero(self):
        """None prices must not raise — the formula collapses cleanly to 0."""
        assert CostCalculator.calculate_cache_savings(1_000, None, None) == 0.0

    def test_savings_equals_full_input_cost_when_cache_is_free(self):
        """If cache reads are priced at 0, savings is the full input cost."""
        savings = CostCalculator.calculate_cache_savings(1_000_000, 3.0, 0.0)
        assert savings == pytest.approx(3.0)


class TestValidatePricing:
    """validate_pricing requires inputPricePerMtok and outputPricePerMtok with non-None values."""

    def test_complete_pricing_is_valid(self):
        assert CostCalculator.validate_pricing(SONNET_45_PRICING) is True

    def test_minimal_pricing_is_valid(self):
        """Cache fields are not required."""
        assert CostCalculator.validate_pricing({
            "inputPricePerMtok": 1.0,
            "outputPricePerMtok": 2.0,
        }) is True

    def test_missing_input_price_is_invalid(self):
        assert CostCalculator.validate_pricing({"outputPricePerMtok": 2.0}) is False

    def test_missing_output_price_is_invalid(self):
        assert CostCalculator.validate_pricing({"inputPricePerMtok": 1.0}) is False

    def test_none_value_is_invalid(self):
        assert CostCalculator.validate_pricing({
            "inputPricePerMtok": None,
            "outputPricePerMtok": 2.0,
        }) is False


class TestValidateUsage:
    """validate_usage requires inputTokens and outputTokens with non-None values."""

    def test_complete_usage_is_valid(self):
        assert CostCalculator.validate_usage({
            "inputTokens": 100,
            "outputTokens": 50,
        }) is True

    def test_zero_values_are_valid(self):
        """Zero is a real measurement, not an absence."""
        assert CostCalculator.validate_usage({
            "inputTokens": 0,
            "outputTokens": 0,
        }) is True

    def test_missing_input_tokens_is_invalid(self):
        assert CostCalculator.validate_usage({"outputTokens": 50}) is False

    def test_none_value_is_invalid(self):
        assert CostCalculator.validate_usage({
            "inputTokens": None,
            "outputTokens": 50,
        }) is False
