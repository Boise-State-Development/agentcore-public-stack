"""Document-context fields on the admin cost surfaces
(docs/specs/document-context-offload.md §6.1).

Rows carry the attachment footprint at each call; the anatomy projects it
per call, the profile rolls it up (digest-vs-full turn shares, peak document
tokens, document_read totals) and reports coverage. The session-row rollups
the write path ADDs are pinned too, so the two never disagree on a definition.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.models import CompactionEvent
from apis.app_api.admin.costs.service import AdminCostService, _call_ledger
from apis.shared.sessions.metadata import DOCUMENT_ROLLUP_ATTRS, _document_rollups


def _record(i, **extra):
    rec = {
        "timestamp": f"2026-09-16T00:00:{i:02d}Z",
        "messageId": i,
        "tokenUsage": {"inputTokens": 100, "outputTokens": 5, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
        "modelInfo": {"modelId": "m1"},
        "cost": {"total": 0.01},
        "cacheStatus": "hit",
    }
    rec.update(extra)
    return rec


FULL = dict(hasDocuments=True, documentCount=2, documentTokens=12_000, documentDigests=0,
            documentsAttached=2, documentSlices=0, documentSliceTokens=0, documentMime={"pdf": 2})
DIGEST = dict(hasDocuments=False, documentCount=0, documentTokens=0, documentDigests=2,
              documentsAttached=0, documentSlices=1, documentSliceTokens=900, documentMime={},
              documentReads={"calls": 1, "pages": 4, "bytes": 3600})


class TestCallLedger:
    def test_absent_fields_stay_untracked(self):
        ledger = _call_ledger(_record(0), None)
        assert ledger.documents is None and ledger.document_reads is None

    def test_fields_decode_with_decimal_coercion(self):
        from decimal import Decimal

        rec = _record(0, hasDocuments=True, documentCount=Decimal("1"), documentTokens=Decimal("500"),
                      documentMime={"pdf": Decimal("1")}, documentReads={"calls": Decimal("2"), "pages": 3})
        ledger = _call_ledger(rec, None)
        assert ledger.documents == {"hasDocuments": True, "documentCount": 1, "documentTokens": 500, "documentMime": {"pdf": 1}}
        assert (ledger.document_reads.calls, ledger.document_reads.pages, ledger.document_reads.bytes) == (2, 3, 0)

    def test_compaction_event_accepts_the_document_kinds(self):
        event = CompactionEvent(kind="document_offload", documents=1, documentTokens=9000, cacheGapSeconds=420)
        assert (event.documents, event.document_tokens, event.cache_gap_seconds) == (1, 9000, 420)


def _service(records, row=None, files=None):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_session_cost_records = AsyncMock(return_value=records)
    service.storage.get_session_diagnostic_row = AsyncMock(return_value=row)
    service.storage.get_user_cost_summary = AsyncMock(return_value=None)
    service._file_repository = AsyncMock()
    service._file_repository.list_session_file_stats = AsyncMock(return_value=files or [])
    return service


class TestAnatomy:
    @pytest.mark.asyncio
    async def test_rows_project_the_document_fields_or_null(self):
        anatomy = await _service([_record(0, **FULL), _record(1, **DIGEST), _record(2)]).get_session_cost_anatomy("s1")
        full, digest, bare = anatomy.calls
        assert full.has_documents is True and full.document_tokens == 12_000 and full.document_mime == {"pdf": 2}
        assert full.document_reads is None
        assert digest.has_documents is False and digest.document_digests == 2
        assert digest.document_reads.pages == 4 and digest.document_slice_tokens == 900
        assert bare.has_documents is None and bare.document_reads is None
        wire = anatomy.model_dump(by_alias=True)["calls"][1]
        assert wire["documentReads"] == {"calls": 1, "pages": 4, "bytes": 3600}


def _row(**overrides):
    row = {
        "sessionId": "s1", "userId": "u1", "status": "active", "messageCount": 4, "totalCost": 0.5,
        "lastContextTokens": 20_000, "contextWindow": 200_000, "totalCacheReadTokens": 1, "totalCacheWriteTokens": 1,
        "preferences": {"lastModel": "m1", "enabledTools": []}, "compaction": {},
    }
    row.update(overrides)
    return row


class TestProfile:
    @pytest.mark.asyncio
    async def test_rollups_from_rows(self):
        records = [_record(0, **FULL), _record(1, **{**FULL, "documentTokens": 15_000}), _record(2, **DIGEST), _record(3)]
        profile = await _service(records, row=_row()).get_session_profile("s1")
        assert profile.data_coverage.documents is True
        assert (profile.full_document_calls, profile.digest_only_calls) == (2, 1)
        assert profile.peak_document_tokens == 15_000
        assert (profile.document_read_calls, profile.document_read_pages) == (1, 4)

    @pytest.mark.asyncio
    async def test_session_row_rollups_cover_rows_that_expired(self):
        row = _row(fullDocumentCalls=7, digestOnlyCalls=2, documentReadCalls=3, documentReadPages=11)
        profile = await _service([_record(0)], row=row).get_session_profile("s1")
        assert profile.data_coverage.documents is True
        assert (profile.full_document_calls, profile.digest_only_calls) == (7, 2)
        assert (profile.document_read_calls, profile.document_read_pages) == (3, 11)
        assert profile.peak_document_tokens is None

    @pytest.mark.asyncio
    async def test_untracked_reads_as_not_tracked(self):
        profile = await _service([_record(0)], row=_row()).get_session_profile("s1")
        assert profile.data_coverage.documents is False
        assert profile.full_document_calls == 0 and profile.peak_document_tokens is None


class TestWriteSideRollups:
    def test_attrs_are_the_ones_the_profile_reads(self):
        assert DOCUMENT_ROLLUP_ATTRS == ("fullDocumentCalls", "digestOnlyCalls", "documentReadCalls", "documentReadPages")

    @pytest.mark.parametrize(
        "extra,expected",
        [
            ({}, (0, 0, 0, 0)),
            (FULL, (1, 0, 0, 0)),
            (DIGEST, (0, 1, 1, 4)),
            ({"hasDocuments": True, "documentDigests": 3}, (1, 0, 0, 0)),   # inline wins over digests
            ({"documentReads": {"calls": "x", "pages": None}}, (0, 0, 0, 0)),
        ],
    )
    def test_deltas_from_the_calls_extras(self, extra, expected):
        rollups = _document_rollups(SimpleNamespace(model_extra=dict(extra)))
        assert tuple(rollups[a] for a in DOCUMENT_ROLLUP_ATTRS) == expected
