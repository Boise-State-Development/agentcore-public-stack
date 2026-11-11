"""``attachmentPlan`` on ``GET /admin/costs/sessions/{id}/calls``
(docs/specs/unified-attachment-delivery.md §8.2).

The turn's last cost row carries how each attachment was delivered; the anatomy
projects it per call (codes, estimates and upload ids, Decimal-coerced) and
leaves it null on every other row.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService
from apis.shared.observability.content_policy import CALL_ROW_PROJECTION


def _record(i, **extra):
    rec = {
        "timestamp": f"2026-11-09T00:00:{i:02d}Z",
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
    service.storage.get_session_diagnostic_row = AsyncMock(return_value=None)
    service.storage.get_user_cost_summary = AsyncMock(return_value=None)
    service._file_repository = AsyncMock()
    service._file_repository.list_session_file_stats = AsyncMock(return_value=[])
    return service


PLAN = {
    "profile": "legacy",
    "files": [
        {"mode": "native", "class": "pdf", "tokens": Decimal("1200"), "reason": "inline", "uploadId": "up-1"},
        {"mode": "tool", "class": "tabular", "tokens": Decimal("0"), "reason": "tabular"},
    ],
    "droppedOverCount": Decimal("1"),
}


def test_the_projection_reads_it():
    assert "attachmentPlan" in CALL_ROW_PROJECTION


@pytest.mark.asyncio
async def test_the_anatomy_projects_the_plan_on_its_row_only():
    anatomy = await _service([_record(0), _record(1, attachmentPlan=PLAN)]).get_session_cost_anatomy("s1")
    first, last = anatomy.calls

    assert first.attachment_plan is None
    dumped = last.model_dump(by_alias=True)["attachmentPlan"]
    assert dumped == {
        "profile": "legacy",
        "files": [
            {"mode": "native", "class": "pdf", "tokens": 1200, "reason": "inline", "uploadId": "up-1",
             "variant": None},
            {"mode": "tool", "class": "tabular", "tokens": 0, "reason": "tabular", "uploadId": None, "variant": None},
        ],
        "droppedOverCount": 1,
    }


@pytest.mark.asyncio
async def test_a_malformed_plan_is_dropped_not_raised():
    anatomy = await _service([_record(0, attachmentPlan={"files": "nope"}), _record(1, attachmentPlan="x")]).get_session_cost_anatomy("s1")
    assert [c.attachment_plan for c in anatomy.calls] == [None, None]


@pytest.mark.asyncio
async def test_an_images_variant_survives_the_projection():
    plan = {"profile": "token", "files": [
        {"mode": "native", "class": "image", "tokens": Decimal("1530"), "reason": "inline", "uploadId": "up-1",
         "variant": "compact"},
    ]}
    anatomy = await _service([_record(0, attachmentPlan=plan)]).get_session_cost_anatomy("s1")
    [entry] = anatomy.calls[0].model_dump(by_alias=True)["attachmentPlan"]["files"]
    assert entry["variant"] == "compact"
