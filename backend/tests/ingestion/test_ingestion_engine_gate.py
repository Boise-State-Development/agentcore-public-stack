"""The legacy ingestion pipeline must not touch a promoted knowledge base.

Routing exclusivity (design §537, Requirement 10.5). Both pipelines are triggered
by the same S3 upload — the legacy `s3:ObjectCreated` notification and the managed
consumer's EventBridge rule — so exactly one of them has to stand down per
document. The consumer already returns immediately for a legacy document; these
tests cover the half that was missing, which is this pipeline standing down for a
*managed* one.

WHY THE STATUS HALF MATTERS MORE THAN THE DUPLICATE VECTORS
Two writers owning one `status` field means the last writer wins by luck. Both
outcomes were observed in dev before this gate existed:

* A PDF was marked `complete` by this pipeline 65 s before the managed knowledge
  base could answer for it — "your document is ready", then an answer that does
  not mention it.
* An image-only PDF that Docling could not parse at all (`Docling produced zero
  chunks`) was marked `failed` while the managed knowledge base was serving it
  correctly. That one only read `complete` in the end because the managed
  consumer happened to finish second and overwrite it. Reverse the finishing
  order — entirely a matter of document size and parse time — and a good,
  retrievable document reads `failed` forever, with no way to retry it.

So the assertions below are mostly about what is *not* written.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from apis.app_api.documents.ingestion import handler as handler_module

ASSISTANT_ID = "ast-gate-1"
DOCUMENT_ID = "DOC-gate-1"


class RecordingStatusManager:
    """Captures every status transition the handler attempts."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def mark_chunking(self, **_: Any) -> None:
        self.calls.append("chunking")

    async def mark_embedding(self, **_: Any) -> None:
        self.calls.append("embedding")

    async def mark_complete(self, **_: Any) -> None:
        self.calls.append("complete")

    async def mark_failed(self, **_: Any) -> None:
        self.calls.append("failed")


@pytest.fixture
def status_manager(monkeypatch: pytest.MonkeyPatch) -> RecordingStatusManager:
    """Stand in for the `status` module, which only resolves inside the image.

    `handler.py` does `from status import create_status_manager` as a bare
    top-level import because the Dockerfile flattens `documents/ingestion/` onto
    LAMBDA_TASK_ROOT. Injecting the module is how a test calls the handler
    without reproducing that layout.
    """
    recorder = RecordingStatusManager()
    fake = types.ModuleType("status")
    fake.create_status_manager = lambda: recorder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "status", fake)
    return recorder


@pytest.fixture
def no_real_pipeline(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Replace the Docling/embedding pipeline; record whether it was reached."""
    reached: List[str] = []

    async def _fake_pipeline(**kwargs: Any) -> None:
        reached.append(kwargs.get("document_id", "?"))

    monkeypatch.setattr(handler_module, "_process_document_pipeline", _fake_pipeline)
    return reached


def _kb_record(engine: Optional[str]) -> Dict[str, Any]:
    record: Dict[str, Any] = {"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}"}
    if engine is not None:
        record["retrievalEngine"] = engine
    return record


def _set_record(monkeypatch: pytest.MonkeyPatch, record) -> None:
    """Point `records.get_kb_record` at a fixed answer, or make it raise."""
    from apis.shared.kb_backend import records as r

    def _get(_assistant_id: str, _app_kb_id: str):
        if isinstance(record, Exception):
            raise record
        return record

    monkeypatch.setattr(r, "get_kb_record", _get)


def _event() -> Dict[str, Any]:
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "docs-bucket"},
                    "object": {
                        "key": (
                            f"assistants/{ASSISTANT_ID}/documents/"
                            f"{DOCUMENT_ID}/flowchart.pdf"
                        )
                    },
                }
            }
        ]
    }


async def _invoke() -> Dict[str, Any]:
    return await handler_module.async_lambda_handler(_event(), None)


class TestAPromotedKnowledgeBaseIsLeftAlone:
    @pytest.mark.asyncio
    async def test_no_document_status_is_written_at_all(
        self, status_manager: RecordingStatusManager, no_real_pipeline, monkeypatch
    ):
        """The whole point. `complete` and `failed` both belong to the consumer."""
        _set_record(monkeypatch, _kb_record("managed"))

        response = await _invoke()

        assert status_manager.calls == [], (
            "the legacy pipeline wrote document status for a knowledge base served "
            "by the managed engine; two writers on one field is how a good "
            "document ends up reading 'failed'"
        )
        assert response["statusCode"] == 200
        assert "Skipped" in json.loads(response["body"])["message"]

    @pytest.mark.asyncio
    async def test_the_document_is_not_parsed_or_embedded(
        self, status_manager, no_real_pipeline: List[str], monkeypatch
    ):
        """No Docling time and no S3 Vectors storage for vectors nothing reads."""
        _set_record(monkeypatch, _kb_record("managed"))

        await _invoke()

        assert no_real_pipeline == []

    @pytest.mark.asyncio
    async def test_it_returns_success_so_the_event_is_not_retried(
        self, status_manager, no_real_pipeline, monkeypatch
    ):
        """A skip is a correct outcome, not a failure to redeliver."""
        _set_record(monkeypatch, _kb_record("managed"))

        response = await _invoke()

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["engine"] == "managed"


class TestEveryOtherKnowledgeBaseStillRuns:
    """The gate keys on the ENGINE, not on the presence of a record.

    During `shadow` and `verify` the legacy path is still authoritative and must
    keep working (Requirements 16.1, 16.6). Only `promote` writes
    `retrievalEngine`, so a record that exists but names no engine — which is
    exactly a migration in flight — must not be skipped.
    """

    @pytest.mark.asyncio
    async def test_a_record_with_no_engine_still_runs(
        self, status_manager: RecordingStatusManager, no_real_pipeline: List[str], monkeypatch
    ):
        _set_record(monkeypatch, _kb_record(None))

        await _invoke()

        assert status_manager.calls == ["chunking"]
        assert no_real_pipeline == [DOCUMENT_ID]

    @pytest.mark.asyncio
    async def test_a_migration_in_flight_still_runs(
        self, status_manager: RecordingStatusManager, no_real_pipeline: List[str], monkeypatch
    ):
        """`shadow` is not `promoted`. Skipping here would strand live uploads."""
        record = _kb_record(None)
        record["migrationState"] = "shadow"
        _set_record(monkeypatch, record)

        await _invoke()

        assert status_manager.calls == ["chunking"]
        assert no_real_pipeline == [DOCUMENT_ID]

    @pytest.mark.asyncio
    async def test_no_record_at_all_still_runs(
        self, status_manager: RecordingStatusManager, no_real_pipeline: List[str], monkeypatch
    ):
        """Every knowledge base that predates this feature. Absence ⇒ legacy."""
        _set_record(monkeypatch, None)

        await _invoke()

        assert status_manager.calls == ["chunking"]
        assert no_real_pipeline == [DOCUMENT_ID]


class TestAnUnreadableRecordRunsTheLegacyPipeline:
    """Fail towards legacy, deliberately — the two errors are not symmetric.

    Wrong towards legacy on a promoted knowledge base costs a duplicate index and
    a status race, and the managed consumer still drives the document to a correct
    terminal state. Wrong towards skipping on a legacy knowledge base means
    nothing indexes the document at all: it sits un-ingested with no error, and
    the only way out is a re-upload. The second is much worse.
    """

    @pytest.mark.asyncio
    async def test_a_lookup_failure_does_not_skip_the_document(
        self, status_manager: RecordingStatusManager, no_real_pipeline: List[str], monkeypatch
    ):
        _set_record(monkeypatch, RuntimeError("DynamoDB unavailable"))

        await _invoke()

        assert status_manager.calls == ["chunking"], (
            "an unreadable KB record skipped the document; a transient read "
            "failure must not silently leave an upload un-ingested"
        )
        assert no_real_pipeline == [DOCUMENT_ID]

    @pytest.mark.asyncio
    async def test_a_lookup_failure_is_not_reported_as_a_document_failure(
        self, status_manager: RecordingStatusManager, no_real_pipeline, monkeypatch
    ):
        """The user's document is fine; our read of an unrelated row was not."""
        _set_record(monkeypatch, RuntimeError("DynamoDB unavailable"))

        await _invoke()

        assert "failed" not in status_manager.calls
