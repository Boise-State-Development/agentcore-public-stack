"""`AdminCostService.get_user_sessions` — the per-user conversation list.

Storage is stubbed with the exact dict shape the content-free reader returns,
so these tests pin the *mapping* and the *sorting contract*; the reader's own
content-freedom is proven separately against moto.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _row(session_id="s1", **overrides):
    row = {
        "sessionId": session_id,
        "userId": "u1",
        "status": "active",
        "createdAt": "2026-09-01T00:00:00Z",
        "lastMessageAt": "2026-09-02T00:00:00Z",
        "messageCount": 6,
        "totalCost": 0.5,
        "lastContextTokens": 20_000,
        "contextWindow": 200_000,
        "totalCacheReadTokens": 100_000,
        "totalCacheWriteTokens": 10_000,
        "partialMissCount": 0,
        "partialMissUsd": 0.0,
        "wastedUsd": 0.0,
        "preferences": {
            "lastModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "enabledTools": ["calculator", "browse_web"],
        },
        "compaction": {"checkpoint": 0, "truncationAnchor": 0, "totalSummarizedTurns": 0, "summaryChars": 400},
    }
    row.update(overrides)
    return row


def _service(rows, period_cost=None):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_user_session_diagnostics = AsyncMock(return_value=rows)
    service.storage.get_user_cost_summary = AsyncMock(
        return_value={"totalCost": period_cost} if period_cost is not None else None
    )
    return service


@pytest.mark.asyncio
async def test_a_row_maps_to_a_content_free_summary():
    resp = await _service([_row()], period_cost=2.0).get_user_sessions("u1", period="2026-09")
    s = resp.sessions[0]
    assert s.session_id == "s1"
    assert s.model_id.startswith("us.anthropic")
    assert s.enabled_tool_count == 2 and s.agent_bound is False
    assert s.context_share == 0.1
    assert s.cost_known and s.total_cost == 0.5
    assert s.share_of_user_period == 25.0
    assert s.cache_efficiency == pytest.approx(100_000 / 110_000, abs=1e-4)
    assert s.summary_approx_tokens == 100
    assert s.tool_call_count is None and s.compaction_count is None  # not tracked
    assert resp.user_period_cost == 2.0 and resp.period == "2026-09"
    # Nothing content-bearing exists on the model to leak; a dump proves the shape.
    assert "title" not in s.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_unknown_cost_rows_are_listed_flagged_and_trail_under_cost_sort():
    rows = [
        _row("cheap", totalCost=0.1),
        _row("unknown", totalCost=None, lastMessageAt="2026-09-09T00:00:00Z"),
        _row("pricey", totalCost=3.0),
    ]
    resp = await _service(rows).get_user_sessions("u1", sort="cost")
    assert [s.session_id for s in resp.sessions] == ["pricey", "cheap", "unknown"]
    unknown = resp.sessions[-1]
    assert unknown.cost_known is False and unknown.total_cost is None
    assert unknown.share_of_user_period is None
    assert resp.unknown_cost_count == 1 and resp.total == 3
    # The list carries counts and a top severity, not codes (those are on the
    # profile). An otherwise-healthy row with no cost fires exactly COST_UNKNOWN.
    assert unknown.diagnosis_count == 1 and unknown.top_diagnosis_severity == "info"
    assert resp.sessions[0].diagnosis_count == 0


@pytest.mark.asyncio
async def test_sort_modes():
    rows = [
        _row("a", lastMessageAt="2026-09-01T00:00:00Z", lastContextTokens=5, messageCount=1),
        _row("b", lastMessageAt="2026-09-03T00:00:00Z", lastContextTokens=50, messageCount=9),
        _row("c", lastMessageAt="2026-09-02T00:00:00Z", lastContextTokens=500, messageCount=3),
    ]
    assert [s.session_id for s in (await _service(rows).get_user_sessions("u1", sort="recent")).sessions] == ["b", "c", "a"]
    assert [s.session_id for s in (await _service(rows).get_user_sessions("u1", sort="context")).sessions] == ["c", "b", "a"]
    assert [s.session_id for s in (await _service(rows).get_user_sessions("u1", sort="messages")).sessions] == ["b", "c", "a"]


@pytest.mark.asyncio
async def test_limit_applies_after_sorting_and_total_reports_the_universe():
    rows = [_row(f"s{i}", totalCost=float(i)) for i in range(5)]
    resp = await _service(rows).get_user_sessions("u1", sort="cost", limit=2)
    assert [s.session_id for s in resp.sessions] == ["s4", "s3"]
    assert resp.total == 5


@pytest.mark.asyncio
async def test_all_time_drops_the_period_and_the_share():
    service = _service([_row()], period_cost=2.0)
    resp = await service.get_user_sessions("u1", period="2026-09", all_time=True)
    assert resp.period is None and resp.user_period_cost is None
    assert resp.sessions[0].share_of_user_period is None
    # No period → no active_since filter reaches storage.
    service.storage.get_user_session_diagnostics.assert_awaited_once_with(user_id="u1", active_since=None, include_deleted=True)
    service.storage.get_user_cost_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_period_scoping_passes_the_month_start_to_storage():
    service = _service([])
    await service.get_user_sessions("u1", period="2026-09")
    service.storage.get_user_session_diagnostics.assert_awaited_once_with(
        user_id="u1", active_since="2026-09-01", include_deleted=True
    )


@pytest.mark.asyncio
async def test_row_level_diagnoses_fire_on_the_rollups(monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD", raising=False)
    spiral = _row(
        "spiral",
        lastContextTokens=150_000,
        totalCacheReadTokens=10_000,
        totalCacheWriteTokens=400_000,
        totalCost=30.0,
        partialMissUsd=27.0,
    )
    resp = await _service([spiral]).get_user_sessions("u1")
    s = resp.sessions[0]
    assert s.top_diagnosis_severity == "high"
    assert s.diagnosis_count >= 3  # spiral, partial-miss heavy, over threshold


@pytest.mark.asyncio
async def test_agent_bound_and_missing_preferences_are_tolerated():
    rows = [_row("bound", preferences={"assistantId": "agent-9"}), _row("bare", preferences=None)]
    resp = await _service(rows).get_user_sessions("u1", sort="recent")
    by_id = {s.session_id: s for s in resp.sessions}
    assert by_id["bound"].agent_bound is True and by_id["bound"].enabled_tool_count is None
    assert by_id["bare"].agent_bound is False and by_id["bare"].model_id is None


@pytest.mark.asyncio
async def test_a_broken_period_cost_lookup_degrades_to_no_share():
    service = _service([_row()])
    service.storage.get_user_cost_summary = AsyncMock(side_effect=RuntimeError("ddb down"))
    resp = await service.get_user_sessions("u1", period="2026-09")
    assert resp.user_period_cost is None
    assert resp.sessions[0].share_of_user_period is None


@pytest.mark.asyncio
async def test_deleted_conversations_are_listed_flagged_and_rolled_up():
    service = _service(
        [
            _row("live", totalCost=0.5),
            _row("gone", totalCost=2.25, deleted=True, status="deleted"),
            _row("legacy-tombstone", totalCost=1.0, deleted=True, status="active"),
            _row("gone-unpriced", totalCost=None, deleted=True, status="deleted"),
        ],
        period_cost=4.0,
    )

    response = await service.get_user_sessions(user_id="u1", period="2026-09")

    # The storage reader is asked for tombstones explicitly — the default
    # reader hides them, which is what left a $20 month showing one $3 row.
    service.storage.get_user_session_diagnostics.assert_awaited_once()
    assert service.storage.get_user_session_diagnostics.await_args.kwargs["include_deleted"] is True

    by_id = {s.session_id: s for s in response.sessions}
    assert by_id["live"].status == "active"
    assert by_id["gone"].status == "deleted"
    # A legacy tombstone carries `deleted` without the status flip; the page
    # gets one normalised signal.
    assert by_id["legacy-tombstone"].status == "deleted"
    assert response.total == 4
    assert response.deleted_session_count == 3
    assert response.deleted_session_cost == pytest.approx(3.25)
    # Deleted rows still take their share of the period total.
    assert by_id["gone"].share_of_user_period == pytest.approx(56.25)


@pytest.mark.asyncio
async def test_no_deleted_conversations_reports_zero():
    service = _service([_row("live")], period_cost=1.0)
    response = await service.get_user_sessions(user_id="u1", period="2026-09")
    assert response.deleted_session_count == 0
    assert response.deleted_session_cost == 0.0
