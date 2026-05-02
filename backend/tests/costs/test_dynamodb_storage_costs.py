"""Tests for DynamoDB cost summary operations.

Covers:
- get_user_cost_summary
- update_user_cost_summary
- _update_model_breakdown
- _update_cost_sort_key
- get_top_users_by_cost
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError


PERIOD = "2025-01"
TIMESTAMP = "2025-01-15T12:00:00Z"


# ── helpers ──────────────────────────────────────────────────────────────────

def _raw_item(table, user_id, period):
    """Read item straight from DynamoDB (Decimals intact)."""
    resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"PERIOD#{period}"})
    return resp.get("Item")


# ── get_user_cost_summary ────────────────────────────────────────────────────

class TestGetUserCostSummary:

    @pytest.mark.asyncio
    async def test_returns_none_when_no_item(self, storage):
        result = await storage.get_user_cost_summary("nonexistent", PERIOD)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_data_when_exists(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.50, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result is not None
        assert result["userId"] == "u1"
        assert result["totalCost"] == pytest.approx(1.50)
        assert result["totalRequests"] == 1

    @pytest.mark.asyncio
    async def test_decimal_to_float_conversion(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=0.123456, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert isinstance(result["totalCost"], float)
        assert isinstance(result["cacheSavings"], float)
        assert result["totalCost"] == pytest.approx(0.123456)

    @pytest.mark.asyncio
    async def test_removes_dynamodb_keys(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        for key in ("PK", "SK", "GSI2PK", "GSI2SK"):
            assert key not in result


# ── update_user_cost_summary ─────────────────────────────────────────────────

class TestUpdateUserCostSummary:

    @pytest.mark.asyncio
    async def test_creates_new_record(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=2.50, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(2.50)
        assert result["totalRequests"] == 1
        assert result["totalInputTokens"] == 1000
        assert result["totalOutputTokens"] == 500
        assert result["totalCacheReadTokens"] == 200
        assert result["totalCacheWriteTokens"] == 100

    @pytest.mark.asyncio
    async def test_atomic_increment(self, storage, sample_usage_delta):
        """Calling twice should ADD values, not overwrite."""
        for _ in range(2):
            await storage.update_user_cost_summary(
                user_id="u1", period=PERIOD,
                cost_delta=1.25, usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
            )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(2.50)
        assert result["totalRequests"] == 2
        assert result["totalInputTokens"] == 2000
        assert result["totalOutputTokens"] == 1000

    @pytest.mark.asyncio
    async def test_period_start_end_set_once(self, storage, sample_usage_delta):
        """periodStart and periodEnd should be set on first call only (if_not_exists)."""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp="2025-01-01T00:00:00Z",
        )
        result1 = await storage.get_user_cost_summary("u1", PERIOD)
        original_start = result1["periodStart"]
        original_end = result1["periodEnd"]

        # Second call should NOT overwrite periodStart/periodEnd
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp="2025-01-20T00:00:00Z",
        )
        result2 = await storage.get_user_cost_summary("u1", PERIOD)
        assert result2["periodStart"] == original_start
        assert result2["periodEnd"] == original_end

    @pytest.mark.asyncio
    async def test_last_updated_changes(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp="2025-01-01T00:00:00Z",
        )
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp="2025-01-20T12:00:00Z",
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["lastUpdated"] == "2025-01-20T12:00:00Z"

    @pytest.mark.asyncio
    async def test_sets_gsi2pk(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        raw = _raw_item(storage.cost_summary_table, "u1", PERIOD)
        assert raw["GSI2PK"] == f"PERIOD#{PERIOD}"

    @pytest.mark.asyncio
    async def test_cache_savings_accumulated(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP, cache_savings_delta=0.35,
        )
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP, cache_savings_delta=0.15,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["cacheSavings"] == pytest.approx(0.50)

    @pytest.mark.asyncio
    async def test_with_model_triggers_breakdown(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=5.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
            model_id="gpt-4o", model_name="GPT-4o", provider="openai",
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert "modelBreakdown" in result
        assert "gpt_4o" in result["modelBreakdown"]
        model = result["modelBreakdown"]["gpt_4o"]
        assert model["modelName"] == "GPT-4o"
        assert model["cost"] == pytest.approx(5.0)
        assert model["requests"] == 1

    @pytest.mark.asyncio
    async def test_multiple_models_accumulate_separately(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=3.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
            model_id="gpt-4o", model_name="GPT-4o", provider="openai",
        )
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=7.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
            model_id="claude-sonnet", model_name="Claude Sonnet", provider="bedrock",
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        bd = result["modelBreakdown"]
        assert bd["gpt_4o"]["cost"] == pytest.approx(3.0)
        assert bd["gpt_4o"]["requests"] == 1
        assert bd["claude_sonnet"]["cost"] == pytest.approx(7.0)
        assert bd["claude_sonnet"]["requests"] == 1


# ── defensive cost_delta coercion ────────────────────────────────────────────


class TestUpdateUserCostSummaryDefensiveCoercion:
    """Regression tests for the Decimal(str(cost_delta)) ConversionSyntax crash.

    Upstream callers occasionally hand the summary writer a dict (the new
    streaming-cost breakdown), None (uncomputed cost), or a non-finite float
    (from None pricing fields). All of these used to raise InvalidOperation
    and abort the rollup write. They must now degrade to a 0 contribution.
    """

    @pytest.mark.asyncio
    async def test_none_cost_delta_does_not_raise(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=None, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(0.0)
        assert result["totalRequests"] == 1

    @pytest.mark.asyncio
    async def test_dict_cost_delta_does_not_raise(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta={"total": 0.0, "inputCost": 0.0, "outputCost": 0.0},
            usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(0.0)
        assert result["totalRequests"] == 1

    @pytest.mark.asyncio
    async def test_nan_cost_delta_does_not_raise(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=float("nan"), usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_inf_cost_delta_does_not_raise(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=float("inf"), usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_none_cache_savings_does_not_raise(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP, cache_savings_delta=None,
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert result["totalCost"] == pytest.approx(1.0)
        assert result["cacheSavings"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_dict_cost_delta_with_model_id_does_not_raise(self, storage, sample_usage_delta):
        """The model-breakdown sub-write also coerces cost_delta safely."""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta={"total": 0.0}, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
            model_id="gpt-4o", model_name="GPT-4o", provider="openai",
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        assert "gpt_4o" in result["modelBreakdown"]
        assert result["modelBreakdown"]["gpt_4o"]["cost"] == pytest.approx(0.0)


# ── _update_model_breakdown ──────────────────────────────────────────────────

class TestUpdateModelBreakdown:

    @pytest.mark.asyncio
    async def test_sanitizes_complex_model_id(self, storage, sample_usage_delta):
        """Dots, colons, and hyphens replaced with underscores."""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
            model_id="us.anthropic.claude-sonnet-4-5:v1",
            model_name="Claude Sonnet 4.5",
            provider="bedrock",
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        expected_key = "us_anthropic_claude_sonnet_4_5_v1"
        assert expected_key in result["modelBreakdown"]

    @pytest.mark.asyncio
    async def test_creates_initial_model_entry(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=2.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
            model_id="gpt-4o", model_name="GPT-4o", provider="openai",
        )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        model = result["modelBreakdown"]["gpt_4o"]
        assert model["modelName"] == "GPT-4o"
        assert model["provider"] == "openai"
        assert model["cost"] == pytest.approx(2.0)
        assert model["requests"] == 1
        assert model["inputTokens"] == 1000
        assert model["outputTokens"] == 500
        assert model["cacheReadTokens"] == 200
        assert model["cacheWriteTokens"] == 100

    @pytest.mark.asyncio
    async def test_increments_existing_model_entry(self, storage, sample_usage_delta):
        for _ in range(3):
            await storage.update_user_cost_summary(
                user_id="u1", period=PERIOD,
                cost_delta=1.0, usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
                model_id="gpt-4o", model_name="GPT-4o", provider="openai",
            )
        result = await storage.get_user_cost_summary("u1", PERIOD)
        model = result["modelBreakdown"]["gpt_4o"]
        assert model["cost"] == pytest.approx(3.0)
        assert model["requests"] == 3
        assert model["inputTokens"] == 3000
        assert model["outputTokens"] == 1500

    @pytest.mark.asyncio
    async def test_breakdown_error_does_not_raise(self, storage, sample_usage_delta):
        """If the final step of _update_model_breakdown fails, no exception propagates."""
        # First create the record so the main update succeeds
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )

        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        original_update = storage.cost_summary_table.update_item

        call_count = 0

        def failing_update(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Fail only the breakdown calls (calls after the main update_item)
            # The main update is call 1, sort key is call 2,
            # breakdown step 1 is call 3, step 2 is call 4, step 3 fails
            if call_count >= 5:
                raise ClientError(error_response, "UpdateItem")
            return original_update(*args, **kwargs)

        with patch.object(storage.cost_summary_table, "update_item", side_effect=failing_update):
            # Should not raise even though breakdown step 3 fails
            await storage.update_user_cost_summary(
                user_id="u1", period=PERIOD,
                cost_delta=1.0, usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
                model_id="gpt-4o", model_name="GPT-4o", provider="openai",
            )


# ── _update_cost_sort_key ────────────────────────────────────────────────────

class TestUpdateCostSortKey:

    @pytest.mark.asyncio
    async def test_zero_cost(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=0.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        raw = _raw_item(storage.cost_summary_table, "u1", PERIOD)
        assert raw["GSI2SK"] == "COST#000000000000000"

    @pytest.mark.asyncio
    async def test_formats_125_50(self, storage, sample_usage_delta):
        """$125.50 → COST#000000000012550"""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=125.50, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        raw = _raw_item(storage.cost_summary_table, "u1", PERIOD)
        assert raw["GSI2SK"] == "COST#000000000012550"

    @pytest.mark.asyncio
    async def test_small_cost(self, storage, sample_usage_delta):
        """$0.01 → COST#000000000000001"""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=0.01, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        raw = _raw_item(storage.cost_summary_table, "u1", PERIOD)
        assert raw["GSI2SK"] == "COST#000000000000001"

    @pytest.mark.asyncio
    async def test_large_cost(self, storage, sample_usage_delta):
        """$10,000.00 → COST#000000001000000"""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=10000.00, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        raw = _raw_item(storage.cost_summary_table, "u1", PERIOD)
        assert raw["GSI2SK"] == "COST#000000001000000"

    @pytest.mark.asyncio
    async def test_sort_key_error_does_not_raise(self, storage, sample_usage_delta):
        """_update_cost_sort_key logs but doesn't raise on failure."""
        # Create the item first via a normal update
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )

        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        original_update = storage.cost_summary_table.update_item

        call_count = 0

        def failing_on_sort_key(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # The main update is call 1, the sort key update is call 2
            if call_count == 2:
                raise ClientError(error_response, "UpdateItem")
            return original_update(*args, **kwargs)

        with patch.object(storage.cost_summary_table, "update_item", side_effect=failing_on_sort_key):
            # Should not raise
            await storage.update_user_cost_summary(
                user_id="u1", period=PERIOD,
                cost_delta=1.0, usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
            )


# ── get_top_users_by_cost ────────────────────────────────────────────────────

class TestGetTopUsersByCost:

    @pytest.mark.asyncio
    async def test_empty_period_returns_empty(self, storage):
        result = await storage.get_top_users_by_cost("2099-12")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_users_sorted_desc(self, storage, sample_usage_delta):
        costs = [("alice", 10.0), ("bob", 50.0), ("carol", 25.0)]
        for uid, cost in costs:
            await storage.update_user_cost_summary(
                user_id=uid, period=PERIOD,
                cost_delta=cost, usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
            )
        results = await storage.get_top_users_by_cost(PERIOD)
        assert len(results) == 3
        assert results[0]["userId"] == "bob"
        assert results[0]["totalCost"] == pytest.approx(50.0)
        assert results[1]["userId"] == "carol"
        assert results[1]["totalCost"] == pytest.approx(25.0)
        assert results[2]["userId"] == "alice"
        assert results[2]["totalCost"] == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_respects_limit(self, storage, sample_usage_delta):
        for i in range(5):
            await storage.update_user_cost_summary(
                user_id=f"user-{i}", period=PERIOD,
                cost_delta=float(i + 1), usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
            )
        results = await storage.get_top_users_by_cost(PERIOD, limit=2)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_min_cost_filter(self, storage, sample_usage_delta):
        costs = [("alice", 1.0), ("bob", 10.0), ("carol", 50.0)]
        for uid, cost in costs:
            await storage.update_user_cost_summary(
                user_id=uid, period=PERIOD,
                cost_delta=cost, usage_delta=sample_usage_delta,
                timestamp=TIMESTAMP,
            )
        results = await storage.get_top_users_by_cost(PERIOD, min_cost=5.0)
        user_ids = {r["userId"] for r in results}
        assert "alice" not in user_ids
        assert "bob" in user_ids
        assert "carol" in user_ids

    @pytest.mark.asyncio
    async def test_removes_dynamodb_keys(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=5.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        results = await storage.get_top_users_by_cost(PERIOD)
        for item in results:
            for key in ("PK", "SK", "GSI2PK", "GSI2SK"):
                assert key not in item

    @pytest.mark.asyncio
    async def test_decimal_to_float_in_results(self, storage, sample_usage_delta):
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=3.14, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        results = await storage.get_top_users_by_cost(PERIOD)
        assert len(results) == 1
        assert isinstance(results[0]["totalCost"], float)

    @pytest.mark.asyncio
    async def test_limit_capped_at_1000(self, storage, sample_usage_delta):
        """Requesting limit > 1000 is clamped to 1000."""
        await storage.update_user_cost_summary(
            user_id="u1", period=PERIOD,
            cost_delta=1.0, usage_delta=sample_usage_delta,
            timestamp=TIMESTAMP,
        )
        # Should not error even with very large limit
        results = await storage.get_top_users_by_cost(PERIOD, limit=5000)
        assert len(results) >= 1
