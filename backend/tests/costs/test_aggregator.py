"""Comprehensive tests for the CostAggregator class."""

import time
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from apis.shared.costs.aggregator import CostAggregator
from apis.shared.costs.models import UserCostSummary, ModelCostSummary, CostBreakdown


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def aggregator(mock_storage):
    """Create a CostAggregator with mocked storage and fast cache expiry."""
    with patch(
        "apis.shared.costs.aggregator.get_metadata_storage",
        return_value=mock_storage,
    ):
        agg = CostAggregator(cache_ttl_seconds=1)
        yield agg


# ── Cache behaviour ──────────────────────────────────────────────────────────


class TestCacheBehaviour:
    @pytest.mark.asyncio
    async def test_cache_miss_calls_storage(self, aggregator, mock_storage, sample_cost_summary_item):
        """First call should hit storage."""
        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item()
        result = await aggregator.get_user_cost_summary("user-1", "2025-01")
        mock_storage.get_user_cost_summary.assert_awaited_once_with("user-1", "2025-01")
        assert result.user_id == "user-1"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_storage(self, aggregator, mock_storage, sample_cost_summary_item):
        """Second call within TTL should return cached result without calling storage again."""
        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item()
        first = await aggregator.get_user_cost_summary("user-1", "2025-01")
        second = await aggregator.get_user_cost_summary("user-1", "2025-01")
        assert mock_storage.get_user_cost_summary.await_count == 1
        assert first.total_cost == second.total_cost

    @pytest.mark.asyncio
    async def test_cache_expires_and_refetches(self, aggregator, mock_storage, sample_cost_summary_item):
        """After TTL expires, storage should be queried again."""
        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item(total_cost=10.0)
        await aggregator.get_user_cost_summary("user-1", "2025-01")
        assert mock_storage.get_user_cost_summary.await_count == 1

        time.sleep(1.1)  # TTL is 1 second

        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item(total_cost=20.0)
        result = await aggregator.get_user_cost_summary("user-1", "2025-01")
        assert mock_storage.get_user_cost_summary.await_count == 2
        assert result.total_cost == 20.0

    @pytest.mark.asyncio
    async def test_empty_summary_is_cached(self, aggregator, mock_storage):
        """When storage returns None the empty summary should be cached."""
        mock_storage.get_user_cost_summary.return_value = None
        first = await aggregator.get_user_cost_summary("user-1", "2025-06")
        second = await aggregator.get_user_cost_summary("user-1", "2025-06")
        assert mock_storage.get_user_cost_summary.await_count == 1
        assert first.total_cost == 0.0
        assert second.total_cost == 0.0


class TestInvalidateCache:
    @pytest.mark.asyncio
    async def test_invalidate_specific_entry(self, aggregator, mock_storage, sample_cost_summary_item):
        """invalidate_cache(user, period) removes only that entry."""
        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item()
        await aggregator.get_user_cost_summary("user-1", "2025-01")
        await aggregator.get_user_cost_summary("user-1", "2025-02")
        assert mock_storage.get_user_cost_summary.await_count == 2

        aggregator.invalidate_cache(user_id="user-1", period="2025-01")

        # 2025-01 should refetch, 2025-02 should still be cached
        await aggregator.get_user_cost_summary("user-1", "2025-01")
        await aggregator.get_user_cost_summary("user-1", "2025-02")
        assert mock_storage.get_user_cost_summary.await_count == 3  # only one new call

    @pytest.mark.asyncio
    async def test_invalidate_all_periods_for_user(self, aggregator, mock_storage, sample_cost_summary_item):
        """invalidate_cache(user) removes all entries for that user."""
        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item()
        await aggregator.get_user_cost_summary("user-1", "2025-01")
        await aggregator.get_user_cost_summary("user-1", "2025-02")

        aggregator.invalidate_cache(user_id="user-1")

        await aggregator.get_user_cost_summary("user-1", "2025-01")
        await aggregator.get_user_cost_summary("user-1", "2025-02")
        # 2 original + 2 refetches = 4
        assert mock_storage.get_user_cost_summary.await_count == 4

    @pytest.mark.asyncio
    async def test_invalidate_entire_cache(self, aggregator, mock_storage, sample_cost_summary_item):
        """invalidate_cache() with no args clears everything."""
        mock_storage.get_user_cost_summary.return_value = sample_cost_summary_item()
        await aggregator.get_user_cost_summary("user-1", "2025-01")
        await aggregator.get_user_cost_summary("user-2", "2025-01")

        aggregator.invalidate_cache()

        await aggregator.get_user_cost_summary("user-1", "2025-01")
        await aggregator.get_user_cost_summary("user-2", "2025-01")
        assert mock_storage.get_user_cost_summary.await_count == 4

    def test_invalidate_nonexistent_entry_is_noop(self, aggregator):
        """Invalidating a key that doesn't exist should not raise."""
        aggregator.invalidate_cache(user_id="ghost", period="2099-12")


# ── get_user_cost_summary with data ─────────────────────────────────────────


class TestGetUserCostSummary:
    @pytest.mark.asyncio
    async def test_returns_correct_fields(self, aggregator, mock_storage, sample_cost_summary_item):
        data = sample_cost_summary_item(
            user_id="u1", period="2025-03", total_cost=99.0,
            total_requests=200, input_tokens=5000, output_tokens=3000,
            cache_read_tokens=1000, cache_write_tokens=500, cache_savings=1.25,
        )
        mock_storage.get_user_cost_summary.return_value = data
        result = await aggregator.get_user_cost_summary("u1", "2025-03")

        assert result.user_id == "u1"
        assert result.period_start == "2025-03-01T00:00:00Z"
        assert result.total_cost == 99.0
        assert result.total_requests == 200
        assert result.total_input_tokens == 5000
        assert result.total_output_tokens == 3000
        assert result.total_cache_read_tokens == 1000
        assert result.total_cache_write_tokens == 500
        assert result.total_cache_savings == 1.25

    @pytest.mark.asyncio
    async def test_builds_model_summaries(self, aggregator, mock_storage, sample_cost_summary_item):
        data = sample_cost_summary_item()
        mock_storage.get_user_cost_summary.return_value = data
        result = await aggregator.get_user_cost_summary("test-user", "2025-01")

        assert len(result.models) == 2
        model_ids = {m.model_id for m in result.models}
        assert "us_anthropic_claude_sonnet_4_5" in model_ids
        assert "gpt_4o" in model_ids

    @pytest.mark.asyncio
    async def test_handles_missing_optional_fields(self, aggregator, mock_storage):
        """Storage dict missing cache token & savings keys should default to 0."""
        data = {
            "periodStart": "2025-04-01T00:00:00Z",
            "periodEnd": "2025-04-30T23:59:59Z",
            "totalCost": 5.0,
            "totalRequests": 10,
            "totalInputTokens": 2000,
            "totalOutputTokens": 1000,
            # no totalCacheReadTokens, totalCacheWriteTokens, cacheSavings, modelBreakdown
        }
        mock_storage.get_user_cost_summary.return_value = data
        result = await aggregator.get_user_cost_summary("u2", "2025-04")

        assert result.total_cache_read_tokens == 0
        assert result.total_cache_write_tokens == 0
        assert result.total_cache_savings == 0.0
        assert result.models == []


# ── get_detailed_cost_report ─────────────────────────────────────────────────


class TestGetDetailedCostReport:
    @pytest.mark.asyncio
    async def test_aggregates_multiple_messages(self, aggregator, mock_storage):
        messages = [
            {"cost": 1.5, "inputTokens": 100, "outputTokens": 50,
             "cacheReadTokens": 0, "cacheWriteTokens": 0,
             "modelId": "m1", "modelName": "Model1", "provider": "p1"},
            {"cost": 2.5, "inputTokens": 200, "outputTokens": 100,
             "cacheReadTokens": 0, "cacheWriteTokens": 0,
             "modelId": "m1", "modelName": "Model1", "provider": "p1"},
        ]
        mock_storage.get_user_messages_in_range.return_value = messages
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)

        assert result.total_cost == pytest.approx(4.0)
        assert result.total_requests == 2
        assert result.total_input_tokens == 300
        assert result.total_output_tokens == 150

    @pytest.mark.asyncio
    async def test_per_model_breakdown(self, aggregator, mock_storage):
        messages = [
            {"cost": 1.0, "inputTokens": 100, "outputTokens": 50,
             "cacheReadTokens": 0, "cacheWriteTokens": 0,
             "modelId": "m1", "modelName": "Model1", "provider": "p1"},
            {"cost": 3.0, "inputTokens": 300, "outputTokens": 150,
             "cacheReadTokens": 0, "cacheWriteTokens": 0,
             "modelId": "m2", "modelName": "Model2", "provider": "p2"},
            {"cost": 2.0, "inputTokens": 200, "outputTokens": 100,
             "cacheReadTokens": 0, "cacheWriteTokens": 0,
             "modelId": "m1", "modelName": "Model1", "provider": "p1"},
        ]
        mock_storage.get_user_messages_in_range.return_value = messages
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)

        assert len(result.models) == 2
        by_id = {m.model_id: m for m in result.models}
        assert by_id["m1"].cost_breakdown.total_cost == pytest.approx(3.0)
        assert by_id["m1"].request_count == 2
        assert by_id["m1"].total_input_tokens == 300
        assert by_id["m2"].cost_breakdown.total_cost == pytest.approx(3.0)
        assert by_id["m2"].request_count == 1

    @pytest.mark.asyncio
    async def test_cache_savings_calculation(self, aggregator, mock_storage):
        """cache_savings = (cache_read / 1M) * (input_price - cache_read_price)."""
        messages = [
            {
                "cost": 0.5,
                "inputTokens": 1000,
                "outputTokens": 500,
                "cacheReadTokens": 500_000,
                "cacheWriteTokens": 0,
                "modelId": "m1",
                "modelName": "Model1",
                "provider": "p1",
                "pricingSnapshot": {
                    "inputPricePerMtok": 3.0,
                    "cacheReadPricePerMtok": 0.3,
                },
            },
        ]
        mock_storage.get_user_messages_in_range.return_value = messages
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)

        # (500_000 / 1_000_000) * 3.0 = 1.5  (standard cost)
        # (500_000 / 1_000_000) * 0.3 = 0.15 (cache cost)
        # savings = 1.5 - 0.15 = 1.35
        assert result.total_cache_savings == pytest.approx(1.35)
        assert result.total_cache_read_tokens == 500_000

    @pytest.mark.asyncio
    async def test_empty_messages_returns_zero_summary(self, aggregator, mock_storage):
        mock_storage.get_user_messages_in_range.return_value = []
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)

        assert result.total_cost == 0.0
        assert result.total_requests == 0
        assert result.total_input_tokens == 0
        assert result.total_output_tokens == 0
        assert result.total_cache_savings == 0.0
        assert result.models == []

    @pytest.mark.asyncio
    async def test_multiple_models_tracked_separately(self, aggregator, mock_storage):
        messages = [
            {"cost": 1.0, "inputTokens": 100, "outputTokens": 50,
             "cacheReadTokens": 10, "cacheWriteTokens": 5,
             "modelId": "alpha", "modelName": "Alpha", "provider": "provA"},
            {"cost": 2.0, "inputTokens": 200, "outputTokens": 100,
             "cacheReadTokens": 20, "cacheWriteTokens": 10,
             "modelId": "beta", "modelName": "Beta", "provider": "provB"},
            {"cost": 3.0, "inputTokens": 300, "outputTokens": 150,
             "cacheReadTokens": 30, "cacheWriteTokens": 15,
             "modelId": "gamma", "modelName": "Gamma", "provider": "provC"},
        ]
        mock_storage.get_user_messages_in_range.return_value = messages
        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 30, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)

        assert len(result.models) == 3
        ids = {m.model_id for m in result.models}
        assert ids == {"alpha", "beta", "gamma"}

    @pytest.mark.asyncio
    async def test_cache_write_tokens_accumulated(self, aggregator, mock_storage):
        messages = [
            {"cost": 0.1, "inputTokens": 10, "outputTokens": 5,
             "cacheReadTokens": 0, "cacheWriteTokens": 100,
             "modelId": "m1", "modelName": "M", "provider": "p"},
            {"cost": 0.2, "inputTokens": 20, "outputTokens": 10,
             "cacheReadTokens": 0, "cacheWriteTokens": 200,
             "modelId": "m1", "modelName": "M", "provider": "p"},
        ]
        mock_storage.get_user_messages_in_range.return_value = messages
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)

        assert result.total_cache_write_tokens == 300
        assert result.models[0].total_cache_write_tokens == 300

    @pytest.mark.asyncio
    async def test_missing_pricing_snapshot_no_crash(self, aggregator, mock_storage):
        """Messages with cache_read_tokens but no pricingSnapshot should not raise."""
        messages = [
            {"cost": 0.5, "inputTokens": 100, "outputTokens": 50,
             "cacheReadTokens": 1000, "cacheWriteTokens": 0,
             "modelId": "m1", "modelName": "M", "provider": "p"},
        ]
        mock_storage.get_user_messages_in_range.return_value = messages
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)
        # savings = (1000/1M) * (0 - 0) = 0
        assert result.total_cache_savings == 0.0

    @pytest.mark.asyncio
    async def test_period_start_end_set_from_args(self, aggregator, mock_storage):
        mock_storage.get_user_messages_in_range.return_value = []
        start = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 4, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = await aggregator.get_detailed_cost_report("u1", start, end)
        assert result.period_start == start.isoformat()
        assert result.period_end == end.isoformat()


# ── _create_empty_summary ────────────────────────────────────────────────────


class TestCreateEmptySummary:
    def test_january(self, aggregator):
        s = aggregator._create_empty_summary("u1", "2025-01")
        assert s.period_start == "2025-01-01T00:00:00Z"
        assert s.period_end == "2025-01-31T23:59:59Z"
        assert s.total_cost == 0.0
        assert s.models == []

    def test_february_non_leap(self, aggregator):
        s = aggregator._create_empty_summary("u1", "2025-02")
        assert s.period_end == "2025-02-28T23:59:59Z"

    def test_february_leap_year(self, aggregator):
        s = aggregator._create_empty_summary("u1", "2024-02")
        assert s.period_end == "2024-02-29T23:59:59Z"

    def test_december(self, aggregator):
        s = aggregator._create_empty_summary("u1", "2025-12")
        assert s.period_start == "2025-12-01T00:00:00Z"
        assert s.period_end == "2025-12-31T23:59:59Z"

    def test_april_30_days(self, aggregator):
        s = aggregator._create_empty_summary("u1", "2025-04")
        assert s.period_end == "2025-04-30T23:59:59Z"

    def test_invalid_format_fallback(self, aggregator):
        s = aggregator._create_empty_summary("u1", "bad-format")
        assert s.period_start == "bad-format-01T00:00:00Z"
        assert s.period_end == "bad-format-31T23:59:59Z"


# ── _build_model_summaries ───────────────────────────────────────────────────


class TestBuildModelSummaries:
    def test_empty_dict_returns_empty_list(self, aggregator):
        assert aggregator._build_model_summaries({}) == []

    def test_single_model(self, aggregator):
        breakdown = {
            "m1": {
                "modelName": "Model1",
                "provider": "prov1",
                "cost": 10.5,
                "requests": 5,
                "inputTokens": 1000,
                "outputTokens": 500,
                "cacheReadTokens": 100,
                "cacheWriteTokens": 50,
            }
        }
        result = aggregator._build_model_summaries(breakdown)
        assert len(result) == 1
        m = result[0]
        assert isinstance(m, ModelCostSummary)
        assert m.model_id == "m1"
        assert m.model_name == "Model1"
        assert m.provider == "prov1"
        assert m.total_input_tokens == 1000
        assert m.total_output_tokens == 500
        assert m.total_cache_read_tokens == 100
        assert m.total_cache_write_tokens == 50
        assert m.cost_breakdown.total_cost == 10.5
        assert m.request_count == 5

    def test_multiple_models(self, aggregator):
        breakdown = {
            "m1": {"modelName": "A", "provider": "p", "cost": 1.0,
                    "requests": 1, "inputTokens": 10, "outputTokens": 5},
            "m2": {"modelName": "B", "provider": "p", "cost": 2.0,
                    "requests": 2, "inputTokens": 20, "outputTokens": 10},
            "m3": {"modelName": "C", "provider": "p", "cost": 3.0,
                    "requests": 3, "inputTokens": 30, "outputTokens": 15},
        }
        result = aggregator._build_model_summaries(breakdown)
        assert len(result) == 3
        ids = {m.model_id for m in result}
        assert ids == {"m1", "m2", "m3"}

    def test_missing_optional_fields_default(self, aggregator):
        """Model entry with only required fields should default optional ones."""
        breakdown = {
            "m1": {"cost": 5.0},
        }
        result = aggregator._build_model_summaries(breakdown)
        m = result[0]
        assert m.model_name == "Unknown"
        assert m.provider == "unknown"
        assert m.total_cache_read_tokens == 0
        assert m.total_cache_write_tokens == 0
        assert m.request_count == 0
