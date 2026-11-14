"""Session cost anatomy with rows whose message id is not a turn's.

``document_extract`` writes its ``C#`` row under ``extract:<toolUseId>`` and
voice under ``voice:<n>``. Neither sets ``callKind``. The anatomy used to pass
the raw id into an ``Optional[int]`` field, so one extraction made the whole
session's ``GET /admin/costs/sessions/{id}/calls`` a 500 (found on dev,
2026-11-13). Such a row now reads as no message id, with its kind named.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _record(message_id, **extra):
    rec = {
        "timestamp": "2026-11-13T17:16:39Z",
        "messageId": message_id,
        "tokenUsage": {"inputTokens": 100, "outputTokens": 5, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
        "modelInfo": {"modelId": "m1"},
        "cost": {"total": 0.005},
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
async def test_extract_and_voice_rows_do_not_fail_the_anatomy():
    anatomy = await _service([
        _record("extract:tooluse_abc", cacheStatus="uncached"),
        _record(1),
        _record("voice:3"),
        _record(5, callKind="handoff_brief"),
    ]).get_session_cost_anatomy("s1")

    assert [c.message_id for c in anatomy.calls] == [None, 1, None, 5]
    assert [c.call_kind for c in anatomy.calls] == ["document_extract", None, "voice", "handoff_brief"]
    assert anatomy.total_cost == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_a_numeric_string_message_id_still_reads_as_its_number():
    anatomy = await _service([_record("7")]).get_session_cost_anatomy("s1")

    assert anatomy.calls[0].message_id == 7
    assert anatomy.calls[0].call_kind is None
