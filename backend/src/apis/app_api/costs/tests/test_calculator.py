"""Unit tests for cost calculator service"""

import pytest
from apis.app_api.costs.calculator import CostCalculator
from apis.app_api.costs.models import CostBreakdown


class TestCostCalculator:
    """Test suite for CostCalculator"""

    def test_calculate_message_cost_basic(self):
        """Test basic cost calculation without caching"""
        usage = {
            "inputTokens": 1000,
            "outputTokens": 500,
            "totalTokens": 1500
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Expected: (1000/1M * $3) + (500/1M * $15) = $0.003 + $0.0075 = $0.0105
        assert total_cost == pytest.approx(0.0105, rel=1e-6)
        assert breakdown.input_cost == pytest.approx(0.003, rel=1e-6)
        assert breakdown.output_cost == pytest.approx(0.0075, rel=1e-6)
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0
        assert breakdown.total_cost == pytest.approx(0.0105, rel=1e-6)

    def test_calculate_message_cost_with_cache_reads(self):
        """Test cost calculation with cache reads (Bedrock)"""
        usage = {
            "inputTokens": 700,           # Non-cached input
            "outputTokens": 500,
            "cacheReadInputTokens": 200,  # Cached reads (90% discount)
            "totalTokens": 1400
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0,
            "cacheReadPricePerMtok": 0.30  # 90% discount
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Expected:
        # Input: 700/1M * $3 = $0.0021
        # Output: 500/1M * $15 = $0.0075
        # Cache read: 200/1M * $0.30 = $0.00006
        # Total: $0.00966
        assert total_cost == pytest.approx(0.00966, rel=1e-6)
        assert breakdown.input_cost == pytest.approx(0.0021, rel=1e-6)
        assert breakdown.output_cost == pytest.approx(0.0075, rel=1e-6)
        assert breakdown.cache_read_cost == pytest.approx(0.00006, rel=1e-6)
        assert breakdown.cache_write_cost == 0.0

    def test_calculate_message_cost_with_cache_writes(self):
        """Test cost calculation with cache writes (Bedrock)"""
        usage = {
            "inputTokens": 700,
            "outputTokens": 500,
            "cacheWriteInputTokens": 100,  # Cache writes (25% markup)
            "totalTokens": 1300
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0,
            "cacheWritePricePerMtok": 3.75  # 25% markup
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Expected:
        # Input: 700/1M * $3 = $0.0021
        # Output: 500/1M * $15 = $0.0075
        # Cache write: 100/1M * $3.75 = $0.000375
        # Total: $0.009975
        assert total_cost == pytest.approx(0.009975, rel=1e-6)
        assert breakdown.input_cost == pytest.approx(0.0021, rel=1e-6)
        assert breakdown.output_cost == pytest.approx(0.0075, rel=1e-6)
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == pytest.approx(0.000375, rel=1e-6)

    def test_calculate_message_cost_with_full_caching(self):
        """Test cost calculation with both cache reads and writes"""
        usage = {
            "inputTokens": 700,
            "outputTokens": 500,
            "cacheReadInputTokens": 200,
            "cacheWriteInputTokens": 100,
            "totalTokens": 1500
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0,
            "cacheReadPricePerMtok": 0.30,
            "cacheWritePricePerMtok": 3.75
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Expected:
        # Input: 700/1M * $3 = $0.0021
        # Output: 500/1M * $15 = $0.0075
        # Cache read: 200/1M * $0.30 = $0.00006
        # Cache write: 100/1M * $3.75 = $0.000375
        # Total: $0.010035
        assert total_cost == pytest.approx(0.010035, rel=1e-6)
        assert breakdown.input_cost == pytest.approx(0.0021, rel=1e-6)
        assert breakdown.output_cost == pytest.approx(0.0075, rel=1e-6)
        assert breakdown.cache_read_cost == pytest.approx(0.00006, rel=1e-6)
        assert breakdown.cache_write_cost == pytest.approx(0.000375, rel=1e-6)
        assert breakdown.total_cost == pytest.approx(0.010035, rel=1e-6)

    def test_calculate_message_cost_zero_tokens(self):
        """Test cost calculation with zero tokens"""
        usage = {
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        assert total_cost == 0.0
        assert breakdown.input_cost == 0.0
        assert breakdown.output_cost == 0.0
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0

    def test_calculate_message_cost_missing_optional_fields(self):
        """Test cost calculation with missing optional cache fields"""
        usage = {
            "inputTokens": 1000,
            "outputTokens": 500
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Should work without cache fields (OpenAI, Gemini)
        assert total_cost == pytest.approx(0.0105, rel=1e-6)
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0

    def test_calculate_cache_savings(self):
        """Test cache savings calculation"""
        # 200 tokens read from cache
        # Standard: $3.00/M, Cache: $0.30/M
        savings = CostCalculator.calculate_cache_savings(
            cache_read_tokens=200,
            input_price=3.0,
            cache_read_price=0.30
        )

        # Expected: (200/1M * $3) - (200/1M * $0.30) = $0.0006 - $0.00006 = $0.00054
        assert savings == pytest.approx(0.00054, rel=1e-6)

    def test_calculate_cache_savings_zero_tokens(self):
        """Test cache savings with zero cache tokens"""
        savings = CostCalculator.calculate_cache_savings(
            cache_read_tokens=0,
            input_price=3.0,
            cache_read_price=0.30
        )

        assert savings == 0.0

    def test_calculate_cache_savings_large_numbers(self):
        """Test cache savings with large token counts"""
        # 1 million tokens (should show significant savings)
        savings = CostCalculator.calculate_cache_savings(
            cache_read_tokens=1_000_000,
            input_price=3.0,
            cache_read_price=0.30
        )

        # Expected: $3 - $0.30 = $2.70
        assert savings == pytest.approx(2.70, rel=1e-6)

    def test_validate_pricing_valid(self):
        """Test pricing validation with valid data"""
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0
        }

        assert CostCalculator.validate_pricing(pricing) is True

    def test_validate_pricing_invalid(self):
        """Test pricing validation with invalid data"""
        pricing = {
            "inputPricePerMtok": 3.0
            # Missing outputPricePerMtok
        }

        assert CostCalculator.validate_pricing(pricing) is False

    def test_validate_usage_valid(self):
        """Test usage validation with valid data"""
        usage = {
            "inputTokens": 1000,
            "outputTokens": 500
        }

        assert CostCalculator.validate_usage(usage) is True

    def test_validate_usage_invalid(self):
        """Test usage validation with invalid data"""
        usage = {
            "inputTokens": 1000
            # Missing outputTokens
        }

        assert CostCalculator.validate_usage(usage) is False

    def test_claude_sonnet_realistic_scenario(self):
        """Test realistic scenario: Claude Sonnet 4.5 with prompt caching"""
        # Scenario: User sends follow-up message with cached system prompt
        usage = {
            "inputTokens": 500,            # New user message
            "outputTokens": 1000,          # Assistant response
            "cacheReadInputTokens": 5000,  # System prompt from cache
            "cacheWriteInputTokens": 0     # No new cache writes
        }
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0,
            "cacheReadPricePerMtok": 0.30,
            "cacheWritePricePerMtok": 3.75
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Expected:
        # Input: 500/1M * $3 = $0.0015
        # Output: 1000/1M * $15 = $0.015
        # Cache read: 5000/1M * $0.30 = $0.0015
        # Total: $0.018
        assert total_cost == pytest.approx(0.018, rel=1e-6)

        # Calculate savings from caching
        savings = CostCalculator.calculate_cache_savings(
            cache_read_tokens=5000,
            input_price=3.0,
            cache_read_price=0.30
        )

        # Without cache, would have cost: 5000/1M * $3 = $0.015
        # With cache: $0.0015
        # Savings: $0.0135
        assert savings == pytest.approx(0.0135, rel=1e-6)

    def test_openai_gpt4_scenario(self):
        """Test realistic scenario: OpenAI GPT-4 (no caching)"""
        # OpenAI doesn't support prompt caching
        usage = {
            "inputTokens": 1500,
            "outputTokens": 800
        }
        pricing = {
            "inputPricePerMtok": 30.0,   # GPT-4 pricing (example)
            "outputPricePerMtok": 60.0
        }

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        # Expected:
        # Input: 1500/1M * $30 = $0.045
        # Output: 800/1M * $60 = $0.048
        # Total: $0.093
        assert total_cost == pytest.approx(0.093, rel=1e-6)
        assert breakdown.cache_read_cost == 0.0
        assert breakdown.cache_write_cost == 0.0
