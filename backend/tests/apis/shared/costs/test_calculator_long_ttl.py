"""1h static-prefix cache writes are billed at 2x base (thresholds spec §3.6, PR-5)."""

import pytest

from apis.shared.costs.calculator import CostCalculator

PRICING = {
    "inputPricePerMtok": 2.0,       # base
    "outputPricePerMtok": 10.0,
    "cacheReadPricePerMtok": 0.2,   # 0.1x
    "cacheWritePricePerMtok": 2.5,  # 1.25x (5m)
}


def _write_cost(read, write, static=None):
    usage = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": read, "cacheWriteInputTokens": write}
    _, b = CostCalculator.calculate_message_cost(usage, PRICING, long_ttl_static_prefix_tokens=static)
    return round(b.cache_write_cost, 9)


def test_multiplier_is_bedrocks_1h_premium():
    assert CostCalculator.LONG_TTL_CACHE_WRITE_MULTIPLIER == 2.0


def test_default_prices_every_write_at_5m():
    assert _write_cost(read=0, write=30_000) == pytest.approx(30_000 / 1e6 * 2.5)
    assert _write_cost(read=0, write=30_000, static=None) == pytest.approx(30_000 / 1e6 * 2.5)


def test_cold_everything_bills_static_at_2x_and_history_at_5m():
    # 10k static + 20k history all written, nothing read.
    expected = 10_000 / 1e6 * 4.0 + 20_000 / 1e6 * 2.5
    assert _write_cost(read=0, write=30_000, static=10_000) == pytest.approx(expected)


def test_static_read_means_history_only_write_at_5m():
    # Static segment was read (1h entry alive); only history re-wrote.
    assert _write_cost(read=10_000, write=20_000, static=10_000) == pytest.approx(20_000 / 1e6 * 2.5)
    assert _write_cost(read=12_000, write=20_000, static=10_000) == pytest.approx(20_000 / 1e6 * 2.5)


def test_partially_read_static_bills_the_unread_remainder_at_2x():
    # Read 4k of a 10k static segment (tools hit, system missed) → 6k at 2x.
    expected = 6_000 / 1e6 * 4.0 + 14_000 / 1e6 * 2.5
    assert _write_cost(read=4_000, write=20_000, static=10_000) == pytest.approx(expected)


def test_write_smaller_than_static_is_capped_by_the_write():
    assert _write_cost(read=0, write=3_000, static=10_000) == pytest.approx(3_000 / 1e6 * 4.0)


def test_no_write_no_cost():
    assert _write_cost(read=10_000, write=0, static=10_000) == 0.0
