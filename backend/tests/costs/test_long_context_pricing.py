"""Long-context pricing: a prompt over a model's threshold bills every bucket on
its long-context row.

Verified 2026-11-20 against the Price List API (`AmazonBedrockFoundationModels`,
us-west-2) and the account's own Cost Explorer: Claude Haiku 5.5 is 5x on input,
output, cache write and cache read above 100,000 prompt tokens, where the prompt
is input + cache read + cache write. Prod's November long-context bill for
`global.anthropic.claude-haiku-5-5` ($2.244) equals our over-threshold rows
re-priced this way, to the cent.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.costs.calculator import CostCalculator
from apis.shared.costs.pricing_config import get_model_pricing
from apis.shared.sessions import metadata as md
from apis.shared.sessions.models import MessageMetadata, ModelInfo, PricingSnapshot, TokenUsage

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc).isoformat()

#: Haiku 5.5, Regional (`us.`) rates and its long-context row.
HAIKU_55 = {
    "inputPricePerMtok": 0.11,
    "outputPricePerMtok": 0.55,
    "cacheWritePricePerMtok": 0.1375,
    "cacheReadPricePerMtok": 0.011,
    "longContextThresholdTokens": 100_000,
    "longContextInputMultiplier": 5.0,
    "longContextOutputMultiplier": 5.0,
}

#: GPT-6 Sol, US Geo CRIS: 2x input and cache, 1.5x output above 272K.
GPT_6_SOL = {
    "inputPricePerMtok": 2.2,
    "outputPricePerMtok": 11.0,
    "cacheWritePricePerMtok": 2.75,
    "cacheReadPricePerMtok": 0.22,
    "longContextThresholdTokens": 272_000,
    "longContextInputMultiplier": 2.0,
    "longContextOutputMultiplier": 1.5,
}


def _usage(input_t=0, output_t=0, cache_read=0, cache_write=0):
    return {
        "inputTokens": input_t,
        "outputTokens": output_t,
        "cacheReadInputTokens": cache_read,
        "cacheWriteInputTokens": cache_write,
    }


def _flat(pricing):
    return {k: v for k, v in pricing.items() if not k.startswith("longContext")}


# ── The threshold ────────────────────────────────────────────────────────────


class TestThreshold:
    def test_prompt_counts_input_and_both_cache_buckets_not_output(self):
        usage = _usage(input_t=8, output_t=50_000, cache_read=90_000, cache_write=10_000)
        assert CostCalculator.prompt_tokens(usage) == 100_008

    def test_exactly_at_the_line_is_standard(self):
        assert not CostCalculator.is_long_context(_usage(input_t=100_000), HAIKU_55)

    def test_one_token_over_the_line_is_long_context(self):
        assert CostCalculator.is_long_context(_usage(input_t=100_001), HAIKU_55)

    def test_cache_reads_alone_cross_the_line(self):
        # Dev's bill: 8-token-input calls landed on the long-context row when
        # their cache reads took the prompt past 100K.
        assert CostCalculator.is_long_context(_usage(input_t=8, cache_read=104_000), HAIKU_55)

    def test_output_never_crosses_the_line(self):
        assert not CostCalculator.is_long_context(_usage(input_t=1_000, output_t=200_000), HAIKU_55)

    def test_no_threshold_is_never_long_context(self):
        assert not CostCalculator.is_long_context(_usage(input_t=900_000), _flat(HAIKU_55))

    @pytest.mark.parametrize("threshold", [None, 0, -1])
    def test_an_empty_threshold_is_ignored(self, threshold):
        pricing = {**HAIKU_55, "longContextThresholdTokens": threshold}
        assert not CostCalculator.is_long_context(_usage(input_t=900_000), pricing)


# ── The rates ────────────────────────────────────────────────────────────────


class TestRatesForCall:
    def test_under_the_line_returns_the_pricing_unchanged(self):
        assert CostCalculator.rates_for_call(_usage(input_t=50_000), HAIKU_55) is HAIKU_55

    def test_haiku_over_the_line_is_five_times_every_bucket(self):
        rates = CostCalculator.rates_for_call(_usage(cache_read=120_000), HAIKU_55)
        # The Price List API's `USW2_*_long_ctx_standard` rows.
        assert rates["inputPricePerMtok"] == pytest.approx(0.55)
        assert rates["outputPricePerMtok"] == pytest.approx(2.75)
        assert rates["cacheWritePricePerMtok"] == pytest.approx(0.6875)
        assert rates["cacheReadPricePerMtok"] == pytest.approx(0.055)

    def test_openai_over_the_line_is_two_times_input_and_one_and_a_half_output(self):
        rates = CostCalculator.rates_for_call(_usage(input_t=300_000), GPT_6_SOL)
        assert rates["inputPricePerMtok"] == pytest.approx(4.4)
        assert rates["cacheWritePricePerMtok"] == pytest.approx(5.5)
        assert rates["cacheReadPricePerMtok"] == pytest.approx(0.44)
        assert rates["outputPricePerMtok"] == pytest.approx(16.5)

    def test_does_not_mutate_the_snapshot(self):
        pricing = dict(HAIKU_55)
        CostCalculator.rates_for_call(_usage(input_t=200_000), pricing)
        assert pricing == HAIKU_55

    def test_a_missing_multiplier_leaves_that_bucket_flat(self):
        pricing = {**HAIKU_55, "longContextOutputMultiplier": None}
        rates = CostCalculator.rates_for_call(_usage(input_t=200_000), pricing)
        assert rates["outputPricePerMtok"] == pytest.approx(0.55)
        assert rates["inputPricePerMtok"] == pytest.approx(0.55)

    def test_absent_cache_rates_stay_absent(self):
        pricing = {k: v for k, v in HAIKU_55.items() if not k.startswith("cache")}
        rates = CostCalculator.rates_for_call(_usage(input_t=200_000), pricing)
        assert "cacheReadPricePerMtok" not in rates
        assert "cacheWritePricePerMtok" not in rates


# ── The cost ─────────────────────────────────────────────────────────────────


class TestCalculateMessageCost:
    def test_under_the_line_matches_a_model_with_no_long_context_row(self):
        usage = _usage(input_t=2_000, output_t=1_000, cache_read=60_000, cache_write=5_000)
        with_row, b1 = CostCalculator.calculate_message_cost(usage, HAIKU_55)
        flat, b2 = CostCalculator.calculate_message_cost(usage, _flat(HAIKU_55))
        assert with_row == pytest.approx(flat)
        assert b1.long_context is False and b2.long_context is False

    def test_over_the_line_is_five_times_the_flat_cost(self):
        usage = _usage(input_t=36, output_t=7_122, cache_read=711_690 // 7, cache_write=238_981 // 7)
        long_cost, breakdown = CostCalculator.calculate_message_cost(usage, HAIKU_55)
        flat_cost, _ = CostCalculator.calculate_message_cost(usage, _flat(HAIKU_55))
        assert breakdown.long_context is True
        assert long_cost == pytest.approx(flat_cost * 5)
        assert breakdown.total_cost == pytest.approx(
            breakdown.input_cost + breakdown.output_cost + breakdown.cache_read_cost + breakdown.cache_write_cost
        )

    def test_a_worked_haiku_call(self):
        # 8 uncached + 104,000 read + 2,000 written, 1,500 out: a typical
        # connector-heavy turn on dev.
        usage = _usage(input_t=8, output_t=1_500, cache_read=104_000, cache_write=2_000)
        total, b = CostCalculator.calculate_message_cost(usage, HAIKU_55)
        assert b.input_cost == pytest.approx(8 / 1e6 * 0.55)
        assert b.cache_read_cost == pytest.approx(104_000 / 1e6 * 0.055)
        assert b.cache_write_cost == pytest.approx(2_000 / 1e6 * 0.6875)
        assert b.output_cost == pytest.approx(1_500 / 1e6 * 2.75)
        assert total == pytest.approx(0.0112244, rel=1e-6)

    def test_openai_output_uses_its_own_multiplier(self):
        usage = _usage(input_t=280_000, output_t=10_000)
        _, b = CostCalculator.calculate_message_cost(usage, GPT_6_SOL)
        assert b.input_cost == pytest.approx(280_000 / 1e6 * 4.4)
        assert b.output_cost == pytest.approx(10_000 / 1e6 * 16.5)

    def test_one_hour_static_writes_follow_the_long_context_input_rate(self):
        # The 1h write is 2x the (now long-context) input rate: $1.10, the
        # Price List's `cache_write_tokens_1h_long_ctx_standard` row.
        usage = _usage(input_t=10, cache_write=120_000)
        _, b = CostCalculator.calculate_message_cost(
            usage, HAIKU_55, long_ttl_static_prefix_tokens=120_000
        )
        assert b.cache_write_cost == pytest.approx(120_000 / 1e6 * 1.10)

    def test_a_model_with_no_row_is_priced_exactly_as_before(self):
        pricing = {
            "inputPricePerMtok": 3.0,
            "outputPricePerMtok": 15.0,
            "cacheReadPricePerMtok": 0.30,
            "cacheWritePricePerMtok": 3.75,
        }
        usage = _usage(input_t=1_000, output_t=500, cache_read=900_000, cache_write=100)
        total, b = CostCalculator.calculate_message_cost(usage, pricing)
        assert total == pytest.approx(0.003 + 0.0075 + 0.27 + 0.000375)
        assert b.long_context is False


# ── The catalog row reaches the snapshot ─────────────────────────────────────


def _row(**overrides):
    row = MagicMock()
    row.model_id = "us.anthropic.claude-haiku-5-5"
    row.input_price_per_million_tokens = 0.11
    row.output_price_per_million_tokens = 0.55
    row.cache_write_price_per_million_tokens = 0.1375
    row.cache_read_price_per_million_tokens = 0.011
    row.speech_input_price_per_million_tokens = None
    row.speech_output_price_per_million_tokens = None
    row.long_context_threshold_tokens = 100_000
    row.long_context_input_multiplier = 5.0
    row.long_context_output_multiplier = 5.0
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestPricingConfig:
    @pytest.mark.asyncio
    async def test_a_row_with_a_threshold_carries_it_and_both_multipliers(self):
        with patch(
            "apis.shared.costs.pricing_config.list_managed_models", new_callable=AsyncMock
        ) as listed:
            listed.return_value = [_row()]
            pricing = await get_model_pricing("us.anthropic.claude-haiku-5-5")
        assert pricing["longContextThresholdTokens"] == 100_000
        assert pricing["longContextInputMultiplier"] == 5.0
        assert pricing["longContextOutputMultiplier"] == 5.0

    @pytest.mark.asyncio
    async def test_a_row_without_one_keeps_the_dict_it_always_had(self):
        with patch(
            "apis.shared.costs.pricing_config.list_managed_models", new_callable=AsyncMock
        ) as listed:
            listed.return_value = [_row(long_context_threshold_tokens=None)]
            pricing = await get_model_pricing("us.anthropic.claude-haiku-5-5")
        assert not any(k.startswith("longContext") for k in pricing)

    def test_the_snapshot_model_keeps_the_fields(self):
        snapshot = PricingSnapshot.model_validate({**HAIKU_55, "snapshotAt": NOW})
        dumped = snapshot.model_dump(by_alias=True)
        assert dumped["longContextThresholdTokens"] == 100_000
        assert dumped["longContextInputMultiplier"] == 5.0
        assert dumped["longContextOutputMultiplier"] == 5.0

    def test_a_snapshot_without_them_prices_flat(self):
        snapshot = PricingSnapshot.model_validate({**_flat(HAIKU_55), "snapshotAt": NOW})
        rates = CostCalculator.rates_for_call(_usage(input_t=500_000), snapshot.model_dump(by_alias=True))
        assert rates["inputPricePerMtok"] == pytest.approx(0.11)


# ── Everything that prices a call reads the same rates ───────────────────────


def _metadata(input_t, cache_read, cache_write, output_t=100, pricing=HAIKU_55, **extra):
    return MessageMetadata(
        **extra,
        token_usage=TokenUsage(
            input_tokens=input_t,
            output_tokens=output_t,
            total_tokens=input_t + output_t,
            cache_read_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
        ),
        model_info=ModelInfo(
            model_id="us.anthropic.claude-haiku-5-5",
            model_name="Claude Haiku 5.5",
            pricing_snapshot=PricingSnapshot.model_validate({**pricing, "snapshotAt": NOW}),
        ),
    )


class TestWastedUsdRates:
    def test_a_long_context_call_prices_its_waste_at_long_context_rates(self):
        rates = md._extract_pricing_dict(_metadata(10, 1_000, 120_000))
        assert rates["cacheWritePricePerMtok"] == pytest.approx(0.6875)
        assert rates["cacheReadPricePerMtok"] == pytest.approx(0.055)

    def test_a_standard_call_keeps_the_snapshot_rates(self):
        rates = md._extract_pricing_dict(_metadata(10, 1_000, 20_000))
        assert rates["cacheWritePricePerMtok"] == pytest.approx(0.1375)


class _FakeTable:
    def __init__(self):
        self.update_kwargs = None

    def update_item(self, **kwargs):
        self.update_kwargs = kwargs
        return {"Attributes": {}}


class TestSessionRowThreshold:
    @pytest.fixture(autouse=True)
    def session_lookup(self, monkeypatch):
        async def _fake_get_session(session_id, user_id, table):
            return {"SK": f"S#{session_id}"}

        monkeypatch.setattr(md, "_get_session_by_gsi", _fake_get_session)

    async def _bump(self, meta):
        table = _FakeTable()
        await md._bump_session_aggregates(
            session_id="sess-1",
            user_id="user-1",
            message_metadata=meta,
            table=table,
            cache_observability={},
        )
        return table.update_kwargs

    @pytest.mark.asyncio
    async def test_a_turn_on_a_model_with_a_line_sets_it_beside_the_window(self):
        kwargs = await self._bump(
            _metadata(10, 90_000, 1_000, contextWindow=100_000, longContextThreshold=100_000)
        )
        assert "longContextThreshold = :lct" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][":lct"] == 100_000

    @pytest.mark.asyncio
    async def test_a_turn_on_a_model_without_one_removes_it(self):
        kwargs = await self._bump(_metadata(10, 90_000, 1_000, contextWindow=1_000_000))
        assert "REMOVE longContextThreshold" in kwargs["UpdateExpression"]
        assert ":lct" not in kwargs["ExpressionAttributeValues"]


class TestStreamCoordinatorFlag:
    def test_flags_a_call_over_the_line_from_a_snapshot_model(self):
        from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

        snapshot = PricingSnapshot.model_validate({**HAIKU_55, "snapshotAt": NOW})
        assert StreamCoordinator._is_long_context_call(_usage(cache_read=150_000), snapshot)
        assert not StreamCoordinator._is_long_context_call(_usage(cache_read=50_000), snapshot)

    def test_never_raises(self):
        from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

        assert StreamCoordinator._is_long_context_call(None, SimpleNamespace()) is False
