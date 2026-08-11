"""Session cost anatomy — the partial-miss split (#833 PR-1).

A ``partial_miss`` call read from cache *and* wasted money: a leading segment
(tools + system) hit while the rest of the prefix was re-written against a live
entry. It is not a hit and it is not a full miss, so the anatomy reports it as
its own count with its own dollars — a subset of ``wastedUsd``, on the same
split-never-deduct discipline as the agent-switch fields.
"""

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _row(*, status, wasted, cache_read=0, cache_write=5000, ts="2026-08-05T00:00:00Z"):
    return {
        "timestamp": ts,
        "cacheStatus": status,
        "wastedUsd": wasted,
        "tokenUsage": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadInputTokens": cache_read,
            "cacheWriteInputTokens": cache_write,
        },
        "modelInfo": {"modelId": "claude-sonnet-5"},
        "cost": {"total": 0.55},
    }


def _service(rows):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_session_cost_records = AsyncMock(return_value=rows)
    return service


@pytest.mark.asyncio
async def test_a_partial_miss_is_counted_and_its_dollars_stay_in_the_total():
    anatomy = await _service([
        _row(status="partial_miss", wasted=0.437, cache_read=11_278, cache_write=190_000)
    ]).get_session_cost_anatomy("sess-1")

    assert anatomy.partial_miss_count == 1
    assert anatomy.partial_miss_usd == 0.437
    # A split of the total, not a separate bucket beside it.
    assert anatomy.wasted_usd == 0.437
    # And it is not confused with a full miss.
    assert anatomy.avoidable_miss_count == 0


@pytest.mark.asyncio
async def test_the_two_miss_shapes_are_counted_separately():
    anatomy = await _service([
        _row(status="partial_miss", wasted=0.44, cache_read=11_278, cache_write=190_000),
        _row(status="partial_miss", wasted=0.43, cache_read=11_278, cache_write=188_000),
        _row(status="miss_avoidable", wasted=0.05, cache_read=0),
        _row(status="hit", wasted=0.0, cache_read=190_000, cache_write=4_000),
    ]).get_session_cost_anatomy("sess-1")

    assert anatomy.partial_miss_count == 2
    assert anatomy.avoidable_miss_count == 1
    assert anatomy.partial_miss_usd == 0.87
    assert anatomy.wasted_usd == 0.92


@pytest.mark.asyncio
async def test_the_status_reaches_the_row_so_the_page_can_flag_it():
    anatomy = await _service([
        _row(status="partial_miss", wasted=0.437, cache_read=11_278, cache_write=190_000)
    ]).get_session_cost_anatomy("sess-1")

    row = anatomy.calls[0]
    assert row.cache_status == "partial_miss"
    # The tell a reader looks for: a flat read against a prefix-sized write.
    assert row.cache_read_tokens == 11_278
    assert row.cache_write_tokens == 190_000
    assert row.wasted_usd == 0.437


@pytest.mark.asyncio
async def test_rows_written_before_this_shipped_contribute_nothing():
    """The incident's own 56 rows say `hit` — no backfill, so they read as hits."""
    anatomy = await _service([
        _row(status="hit", wasted=0.0, cache_read=11_278, cache_write=190_000)
    ]).get_session_cost_anatomy("sess-1")

    assert anatomy.partial_miss_count == 0
    assert anatomy.partial_miss_usd == 0.0
    assert anatomy.wasted_usd == 0.0
