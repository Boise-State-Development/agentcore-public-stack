"""The legacy ingestion pipeline only ingests assistant document keys.

The RAG documents bucket also holds agent icons, at
``assistants/{agent_id}/icons/{digest}.{png|jpg}``, and the pipeline's S3
notification (prefix ``assistants/``) delivers them here too — S3 filters are
prefix/suffix only, so the notification cannot be narrowed to ``documents/``.

The old parser read a 4-part icon key as ``document_id="icons"``, the pipeline
failed on the PNG, and a failed ``DOC#icons`` row was left in the agent's
partition. These tests pin the strict parse: documents still parse, and anything
else is skipped with no status written and a success response (a failure would
only be redelivered).
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, List

import pytest

from apis.app_api.documents.ingestion import handler as handler_module

ASSISTANT_ID = "ast-parse-1"
DOCUMENT_ID = "DOC-parse-1"
BUCKET = "docs-bucket"


def _event(key: str) -> Dict[str, Any]:
    return {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key}}}]}


class RecordingStatusManager:
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
    """Stand in for the `status` module, which only resolves inside the image."""
    recorder = RecordingStatusManager()
    fake = types.ModuleType("status")
    fake.create_status_manager = lambda: recorder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "status", fake)
    return recorder


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Replace the Docling/embedding pipeline and the engine lookup."""
    reached: List[str] = []

    async def _fake_pipeline(**kwargs: Any) -> None:
        reached.append(kwargs.get("document_id", "?"))

    monkeypatch.setattr(handler_module, "_process_document_pipeline", _fake_pipeline)
    monkeypatch.setattr(handler_module, "_resolve_engine", lambda _aid: "s3vectors")
    return reached


class TestDocumentKeysStillParse:
    def test_a_document_key(self):
        data = handler_module._parse_s3_event(_event(f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/report.pdf"))
        assert data is not None
        assert (data["assistant_id"], data["document_id"], data["filename"]) == (ASSISTANT_ID, DOCUMENT_ID, "report.pdf")
        assert data["s3_key"] == f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/report.pdf"

    def test_a_url_encoded_key_is_decoded(self):
        data = handler_module._parse_s3_event(_event(f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/my+report+%282024%29.pdf"))
        assert data is not None
        assert data["filename"] == "my report (2024).pdf"

    def test_a_filename_containing_slashes_is_preserved(self):
        data = handler_module._parse_s3_event(_event(f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/sub/dir/file.pdf"))
        assert data is not None
        assert data["filename"] == "sub/dir/file.pdf"

    def test_the_explicit_test_event_shape_is_still_honored(self):
        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": BUCKET},
                        "object": {"key": "any/key", "assistant_id": ASSISTANT_ID, "document_id": DOCUMENT_ID, "filename": "f.pdf"},
                    }
                }
            ]
        }
        data = handler_module._parse_s3_event(event)
        assert data is not None
        assert data["document_id"] == DOCUMENT_ID


class TestNonDocumentKeysAreSkipped:
    @pytest.mark.parametrize(
        "key",
        [
            f"assistants/{ASSISTANT_ID}/icons/0123456789abcdef.png",
            f"assistants/{ASSISTANT_ID}/icons/0123456789abcdef.jpg",
            f"assistants/{ASSISTANT_ID}/something-else",
            f"assistants/{ASSISTANT_ID}/other/{DOCUMENT_ID}/file.pdf",
            f"assistants//documents/{DOCUMENT_ID}/file.pdf",
            f"assistants/{ASSISTANT_ID}/documents//file.pdf",
            f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/",
            "models/model-1/icons/0123456789abcdef.png",
        ],
    )
    def test_the_parser_returns_none(self, key):
        assert handler_module._parse_s3_event(_event(key)) is None

    @pytest.mark.asyncio
    async def test_an_icon_writes_no_status_and_is_not_processed(self, status_manager: RecordingStatusManager, pipeline: List[str]):
        """The regression: no `DOC#icons` row, failed or otherwise."""
        response = await handler_module.async_lambda_handler(_event(f"assistants/{ASSISTANT_ID}/icons/0123456789abcdef.png"), None)

        assert status_manager.calls == []
        assert pipeline == []
        assert response["statusCode"] == 200
        assert "Skipped" in json.loads(response["body"])["message"]

    @pytest.mark.asyncio
    async def test_a_document_is_still_processed(self, status_manager: RecordingStatusManager, pipeline: List[str]):
        await handler_module.async_lambda_handler(_event(f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/report.pdf"), None)

        assert status_manager.calls == ["chunking"]
        assert pipeline == [DOCUMENT_ID]


class TestAMalformedEventStillFails:
    """A broken event is a different thing from a key that isn't ours."""

    def test_no_records(self):
        with pytest.raises(ValueError):
            handler_module._parse_s3_event({})

    def test_missing_key(self):
        with pytest.raises(ValueError):
            handler_module._parse_s3_event({"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {}}}]})
