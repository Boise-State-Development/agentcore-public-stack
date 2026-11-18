"""A crawled web page's citation names the address it came from.

The URL is read off the document's own ``DOC#`` row by the status filter, which
already fetches that row for its status, so it reaches the citation on either
engine with no extra read and no re-ingest. It is display metadata only: the
excerpts the model reads are built from chunk text.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.assistants import rag_service
from apis.shared.assistants.rag_service import (
    SOURCE_URL_METADATA_KEY,
    _filter_chunks_by_document_status,
    augment_prompt_with_context,
    web_source_url,
)
from apis.shared.kb_backend.protocol import Chunk

ASSISTANT_ID = "ast-test-001"
PAGE_URL = "https://www.example.edu/research/centers"


def _table(rows):
    table = MagicMock()

    def _get_item(**kwargs):
        doc_id = kwargs["Key"]["SK"].removeprefix("DOC#")
        return {"Item": rows[doc_id]} if doc_id in rows else {}

    table.get_item = MagicMock(side_effect=_get_item)
    return table


def _web_row(url=PAGE_URL, status="complete"):
    return {"status": status, "sourceConnectorId": "web", "sourceFileId": url}


def _chunk(doc_id, **extra):
    return Chunk(text=f"text of {doc_id}", relevance=0.9, document_id=doc_id, metadata={"document_id": doc_id, **extra})


def _filter(chunks, rows):
    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t"}), patch.object(
        rag_service, "_documents_table", return_value=_table(rows)
    ):
        return _filter_chunks_by_document_status(chunks, ASSISTANT_ID)


class TestWebSourceUrl:
    def test_crawled_page_row_gives_its_url(self):
        assert web_source_url(_web_row()) == PAGE_URL

    @pytest.mark.parametrize(
        "row",
        [
            {"sourceConnectorId": "google_drive", "sourceFileId": "1AbCdEf"},  # opaque Drive id
            {"sourceConnectorId": "google_drive", "sourceFileId": PAGE_URL},  # only the crawler's rows count
            {"sourceConnectorId": "web", "sourceFileId": "javascript:alert(1)"},
            {"sourceConnectorId": "web", "sourceFileId": "ftp://example.edu/file"},
            {"sourceConnectorId": "web", "sourceFileId": "https://"},
            {"sourceConnectorId": "web", "sourceFileId": None},
            {"sourceConnectorId": "web"},
            {"filename": "upload.pdf"},
        ],
    )
    def test_anything_else_gives_none(self, row):
        assert web_source_url(row) is None


class TestStatusFilterCarriesUrl:
    def test_crawled_document_chunk_gets_url_from_its_row(self):
        out = _filter([_chunk("doc-web"), _chunk("doc-upload")], {
            "doc-web": _web_row(),
            "doc-upload": {"status": "complete", "filename": "handbook.pdf"},
        })
        assert [c.metadata.get(SOURCE_URL_METADATA_KEY) for c in out] == [PAGE_URL, None]
        assert SOURCE_URL_METADATA_KEY not in out[1].metadata

    def test_one_read_per_document(self):
        table = _table({"doc-web": _web_row()})
        with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t"}), patch.object(
            rag_service, "_documents_table", return_value=table
        ):
            out = _filter_chunks_by_document_status([_chunk("doc-web"), _chunk("doc-web")], ASSISTANT_ID)
        assert table.get_item.call_count == 1
        assert all(c.metadata[SOURCE_URL_METADATA_KEY] == PAGE_URL for c in out)

    def test_index_attribute_cannot_supply_or_spoof_the_url(self):
        out = _filter(
            [_chunk("doc-upload", source_url="https://evil.example/"), _chunk("doc-web", source_url="https://evil.example/")],
            {"doc-upload": {"status": "complete"}, "doc-web": _web_row()},
        )
        assert SOURCE_URL_METADATA_KEY not in out[0].metadata
        assert out[1].metadata[SOURCE_URL_METADATA_KEY] == PAGE_URL

    def test_incomplete_crawled_page_is_still_dropped(self):
        assert _filter([_chunk("doc-web")], {"doc-web": _web_row(status="deleting")}) == []

    def test_input_chunks_are_not_mutated(self):
        original = _chunk("doc-web")
        _filter([original], {"doc-web": _web_row()})
        assert SOURCE_URL_METADATA_KEY not in original.metadata

    def test_unannotated_chunk_is_returned_as_is(self):
        original = _chunk("doc-upload")
        assert _filter([original], {"doc-upload": {"status": "complete"}})[0] is original


def test_url_never_reaches_the_model_excerpts():
    chunk = {"text": "Centers list", "metadata": {"document_id": "d", SOURCE_URL_METADATA_KEY: PAGE_URL}}
    without = {"text": "Centers list", "metadata": {"document_id": "d"}}
    assert augment_prompt_with_context("q", [chunk]) == augment_prompt_with_context("q", [without])
    assert PAGE_URL not in augment_prompt_with_context("q", [chunk])


class TestBuildCitations:
    @pytest.fixture
    def build(self):
        from apis.inference_api.chat.routes import _build_citations

        return _build_citations

    def test_web_citation_carries_source_url(self, build):
        chunks = [
            {"text": "a", "metadata": {"document_id": "d1", "filename": "Centers.md", "source_url": PAGE_URL}},
            {"text": "b", "metadata": {"document_id": "d2", "filename": "handbook.pdf"}},
        ]
        out = build(SimpleNamespace(show_citations=True), chunks, ASSISTANT_ID)
        assert out[0]["sourceUrl"] == PAGE_URL
        assert "sourceUrl" not in out[1]
        assert out[1] == {"assistantId": ASSISTANT_ID, "documentId": "d2", "fileName": "handbook.pdf", "text": "b"}

    def test_citations_off_still_suppresses_everything(self, build):
        chunks = [{"text": "a", "metadata": {"document_id": "d1", "source_url": PAGE_URL}}]
        assert build(SimpleNamespace(show_citations=False), chunks, ASSISTANT_ID) == []


def test_stored_citation_keeps_url_across_reload():
    from apis.shared.sessions.models import Citation

    web = Citation(**{"assistantId": "a", "documentId": "d", "fileName": "Centers.md", "text": "x", "sourceUrl": PAGE_URL})
    upload = Citation(**{"assistantId": "a", "documentId": "d", "fileName": "h.pdf", "text": "x"})
    assert web.model_dump(by_alias=True, exclude_none=True)["sourceUrl"] == PAGE_URL
    assert "sourceUrl" not in upload.model_dump(by_alias=True, exclude_none=True)
