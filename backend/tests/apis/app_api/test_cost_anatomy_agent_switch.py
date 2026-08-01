"""Session cost anatomy — the agent-switch split (#756).

An `@`-mention hands one turn to a different Agent (Marketplace D11), which genuinely
re-writes the prompt-cache prefix. The turn classifies as ``miss_avoidable`` and that is
*correct* — the tokens really were spent at the write premium.

What these pin is that the anatomy **splits** rather than **deducts**. The totals keep
every dollar, because hiding the cost of mentions would understate a feature we want to
measure on purpose; the split is what lets a reader subtract the explained part and see
whether unexplained waste moved, which is the signal the fingerprints exist for.
"""

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _row(*, status, wasted, agent_id=None, switched=False, ts="2026-07-26T00:00:00Z"):
    return {
        "timestamp": ts,
        "cacheStatus": status,
        "wastedUsd": wasted,
        "turnAgentId": agent_id,
        "agentSwitched": switched,
        "tokenUsage": {"inputTokens": 10, "outputTokens": 5, "cacheWriteInputTokens": 5000},
        "modelInfo": {"modelId": "claude-sonnet-5"},
        "cost": {"total": 0.02},
    }


def _service(rows):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_session_cost_records = AsyncMock(return_value=rows)
    return service


@pytest.mark.asyncio
async def test_an_explained_miss_is_counted_in_both_the_total_and_the_split():
    anatomy = await _service(
        [_row(status="miss_avoidable", wasted=0.006, agent_id="ast-x", switched=True)]
    ).get_session_cost_anatomy("sess-1")

    assert anatomy.avoidable_miss_count == 1
    assert anatomy.wasted_usd == 0.006
    assert anatomy.agent_switch_miss_count == 1
    assert anatomy.agent_switch_usd == 0.006


@pytest.mark.asyncio
async def test_an_unexplained_miss_is_absent_from_the_split():
    anatomy = await _service(
        [_row(status="miss_avoidable", wasted=0.02, agent_id="ast-x", switched=False)]
    ).get_session_cost_anatomy("sess-1")

    assert anatomy.avoidable_miss_count == 1
    assert anatomy.wasted_usd == 0.02
    assert anatomy.agent_switch_miss_count == 0
    assert anatomy.agent_switch_usd == 0.0


@pytest.mark.asyncio
async def test_unexplained_waste_is_the_total_minus_the_split():
    """The number a prefix-stability regression actually moves."""
    anatomy = await _service([
        _row(status="miss_avoidable", wasted=0.006, agent_id="ast-x", switched=True),
        _row(status="miss_avoidable", wasted=0.05, agent_id="ast-x", switched=False),
        _row(status="hit", wasted=0.0, agent_id="ast-x"),
    ]).get_session_cost_anatomy("sess-1")

    assert anatomy.avoidable_miss_count == 2
    assert anatomy.agent_switch_miss_count == 1
    assert round(anatomy.wasted_usd - anatomy.agent_switch_usd, 6) == 0.05


@pytest.mark.asyncio
async def test_a_switch_that_hit_the_cache_contributes_no_explained_waste():
    """The flag describes the turn; only a miss can be waste."""
    anatomy = await _service(
        [_row(status="hit", wasted=0.0, agent_id="ast-x", switched=True)]
    ).get_session_cost_anatomy("sess-1")

    assert anatomy.avoidable_miss_count == 0
    assert anatomy.agent_switch_miss_count == 0
    assert anatomy.agent_switch_usd == 0.0


@pytest.mark.asyncio
async def test_the_row_carries_the_agent_so_a_reader_can_see_which_one():
    anatomy = await _service(
        [_row(status="miss_avoidable", wasted=0.006, agent_id="ast-canvas", switched=True)]
    ).get_session_cost_anatomy("sess-1")

    assert anatomy.calls[0].turn_agent_id == "ast-canvas"
    assert anatomy.calls[0].agent_switched is True


@pytest.mark.asyncio
async def test_rows_written_before_this_shipped_read_as_unswitched():
    """No backfill — an older row simply has neither attribute."""
    anatomy = await _service([
        {
            "timestamp": "2026-07-01T00:00:00Z",
            "cacheStatus": "miss_avoidable",
            "wastedUsd": 0.01,
            "tokenUsage": {"inputTokens": 10, "outputTokens": 5},
            "modelInfo": {"modelId": "claude-sonnet-5"},
            "cost": {"total": 0.02},
        }
    ]).get_session_cost_anatomy("sess-1")

    assert anatomy.calls[0].turn_agent_id is None
    assert anatomy.calls[0].agent_switched is False
    assert anatomy.agent_switch_miss_count == 0
