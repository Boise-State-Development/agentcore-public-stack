"""Turn outcome on the session cost anatomy (release-ledger spec §5.1).

Each ``C#`` row carries ``outcome`` (how its turn ended) and, on the last row
of a completed turn whose final call streamed no text, ``emptyAnswer: true``.
The anatomy projects both per call; a row written before they shipped reads
as no outcome and not empty, never as a guess. Both names are in the admin
read projection, or DynamoDB would never return them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService
from apis.shared.observability.content_policy import CALL_ROW_PROJECTION


def _record(i, **extra):
    rec = {
        "timestamp": f"2026-11-06T00:00:{i:02d}Z",
        "messageId": i,
        "tokenUsage": {"inputTokens": 100, "outputTokens": 5, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
        "modelInfo": {"modelId": "m1"},
        "cost": {"total": 0.01},
        "cacheStatus": "hit",
    }
    rec.update(extra)
    return rec


def _service(records):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_session_cost_records = AsyncMock(return_value=records)
    return service


@pytest.mark.asyncio
async def test_rows_project_outcome_and_empty_answer():
    anatomy = await _service([
        _record(0, outcome="completed"),
        _record(1, outcome="completed", emptyAnswer=True),
        _record(2, outcome="stopped"),
        _record(3),  # written before the field shipped
    ]).get_session_cost_anatomy("s1")

    calls = anatomy.calls
    assert [c.outcome for c in calls] == ["completed", "completed", "stopped", None]
    assert [c.empty_answer for c in calls] == [False, True, False, False]
    dumped = anatomy.model_dump(by_alias=True)["calls"][1]
    assert dumped["outcome"] == "completed" and dumped["emptyAnswer"] is True


def test_the_read_projection_returns_both_fields():
    assert "outcome" in CALL_ROW_PROJECTION
    assert "emptyAnswer" in CALL_ROW_PROJECTION
