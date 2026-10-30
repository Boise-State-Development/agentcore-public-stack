"""``cleanup_document_resources`` resolves the knowledge base's engine once and
hands it to both phases, so a managed-KB document never pays the S3 Vectors
full-index scan and the record is read once, off the loop."""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.documents.services import cleanup_service
from apis.shared.kb_backend.records import ENGINE_MANAGED

ENV = {"S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": "test-bucket"}


def _run(monkeypatch, record):
    reads: list[int] = []

    def fake_get_kb_record(*_a):
        reads.append(threading.get_ident())
        return record

    monkeypatch.setattr("apis.shared.kb_backend.records.get_kb_record", fake_get_kb_record)
    delete_doc = AsyncMock(return_value=0)
    managed = AsyncMock(return_value=True)
    monkeypatch.setattr(cleanup_service, "_delete_managed_documents_with_retries", managed)
    monkeypatch.setattr(cleanup_service, "_delete_s3_with_retries", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "apis.app_api.documents.services.document_service.hard_delete_document", AsyncMock()
    )
    return reads, delete_doc, managed


@pytest.mark.asyncio
async def test_managed_document_skips_the_index_scan(monkeypatch):
    reads, delete_doc, managed = _run(monkeypatch, {"retrievalEngine": ENGINE_MANAGED})
    with (
        patch.dict("os.environ", ENV),
        patch("apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document", delete_doc),
    ):
        ok = await cleanup_service.cleanup_document_resources("DOC-1", "ast-1", "k", chunk_count=None)
    assert ok is True
    delete_doc.assert_awaited_once_with("DOC-1", scan_index=False)
    assert managed.await_args.kwargs["engine"] == ENGINE_MANAGED
    assert len(reads) == 1, "the record is read once for both phases"
    assert reads[0] != threading.get_ident(), "the DynamoDB read runs off the loop"


@pytest.mark.asyncio
async def test_legacy_document_keeps_the_scan(monkeypatch):
    reads, delete_doc, managed = _run(monkeypatch, None)
    with (
        patch.dict("os.environ", ENV),
        patch("apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document", delete_doc),
    ):
        await cleanup_service.cleanup_document_resources("DOC-1", "ast-1", "k", chunk_count=None)
    delete_doc.assert_awaited_once_with("DOC-1", scan_index=True)
    assert managed.await_args.kwargs["engine"] == "s3vectors"


@pytest.mark.asyncio
async def test_unreadable_record_keeps_the_scan_and_lets_the_managed_phase_fail(monkeypatch):
    def boom(*_a):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr("apis.shared.kb_backend.records.get_kb_record", boom)
    delete_doc = AsyncMock(return_value=0)
    monkeypatch.setattr(cleanup_service, "_delete_s3_with_retries", AsyncMock(return_value=True))
    hard_delete = AsyncMock()
    monkeypatch.setattr("apis.app_api.documents.services.document_service.hard_delete_document", hard_delete)
    with (
        patch.dict("os.environ", ENV),
        patch("apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document", delete_doc),
    ):
        ok = await cleanup_service.cleanup_document_resources(
            "DOC-1", "ast-1", "k", chunk_count=None, max_retries=1, base_delay=0
        )
    assert ok is False, "an unknown engine must not hard-delete the row"
    delete_doc.assert_awaited_once_with("DOC-1", scan_index=True)
    hard_delete.assert_not_awaited()
