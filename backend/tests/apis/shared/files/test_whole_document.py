"""One conversation file, whole, for a side call (``apis.shared.files.whole_document``)."""

from __future__ import annotations

import asyncio
import io
from typing import Dict, Optional

import pytest

from apis.shared.files import large_document
from apis.shared.files import models as file_models
from apis.shared.files.models import LinkedFrom
from apis.shared.files.whole_document import WholeDocumentError, read_whole_document

PDF_MIME = "application/pdf"


def _pdf(pages: int) -> bytes:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 200)
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


def _meta(upload_id: str = "f1", filename: str = "proposal.pdf", mime: str = PDF_MIME, size: int = 1_000, **kw):
    fields = dict(
        upload_id=upload_id,
        user_id="alice",
        session_id="src",
        filename=filename,
        mime_type=mime,
        size_bytes=size,
        s3_key=f"user-files/alice/src/{upload_id}/{filename}",
        s3_bucket="b",
        status=file_models.FileStatus.READY,
    )
    fields.update(kw)
    return file_models.FileMetadata(**fields)


class _Repo:
    def __init__(self, rows: Dict[str, file_models.FileMetadata]) -> None:
        self.rows = rows

    async def get_file(self, user_id: str, upload_id: str) -> Optional[file_models.FileMetadata]:
        row = self.rows.get(upload_id)
        return row if row is not None and row.user_id == user_id else None


@pytest.fixture
def world(monkeypatch):
    rows: Dict[str, file_models.FileMetadata] = {}
    objects: Dict[str, bytes] = {}
    monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: _Repo(rows))
    monkeypatch.setattr("apis.shared.files.document_read._get_object_bytes", lambda bucket, key: objects[key])
    return rows, objects


def _read(upload_id: str = "f1"):
    return asyncio.run(read_whole_document("alice", "src", upload_id))


def test_a_pdf_goes_native_and_whole_with_its_pages(world):
    rows, objects = world
    raw = _pdf(4)
    rows["f1"] = _meta(size=len(raw))
    objects[rows["f1"].s3_key] = raw

    document = _read()

    assert document.native and document.pdf_bytes == raw
    assert document.pages == 4 and document.facts.page_count == 4
    assert document.text is None


def test_text_documents_go_as_text_in_the_documents_as_text_envelope(world):
    rows, objects = world
    rows["f1"] = _meta(filename="notes.md", mime="text/markdown", size=12)
    objects[rows["f1"].s3_key] = b"# Notes\nhi\n"

    document = _read()

    assert not document.native
    assert document.text.startswith('<document name="notes.md" format="md">')
    assert "# Notes" in document.text


def test_a_prepared_large_document_goes_as_its_text_layer(world):
    rows, objects = world
    rows["f1"] = _meta(size=40 * 1024 * 1024, preparation={"status": "ready", "version": 1, "pageCount": 2, "shardPages": 10})
    if not large_document.is_prepared(rows["f1"]):
        pytest.skip("preparation shape differs from this fixture")
    objects[large_document.text_layer_key(rows["f1"].s3_key)] = large_document.encode_text_layer("page", ["one", "two"])

    document = _read()

    assert not document.native
    assert "one\n\ntwo" in document.text


def test_a_reference_reads_through_to_its_original_and_a_gone_one_raises(world):
    rows, objects = world
    raw = _pdf(1)
    rows["orig"] = _meta(upload_id="orig", session_id="other", size=len(raw))
    objects[rows["orig"].s3_key] = raw
    rows["f1"] = _meta(
        size=len(raw),
        s3_key=rows["orig"].s3_key,
        source="linked",
        delivery="reference",
        linked_from=LinkedFrom(session_id="other", upload_id="orig", from_title="Planning"),
    )

    assert _read().pdf_bytes == raw

    del rows["orig"]
    with pytest.raises(WholeDocumentError) as gone:
        _read()
    assert gone.value.reason == "gone"


def test_another_conversations_file_is_not_found(world):
    rows, _ = world
    rows["f1"] = _meta(session_id="elsewhere")

    with pytest.raises(WholeDocumentError) as missing:
        _read()
    assert missing.value.reason == "not_found"


def test_a_file_with_no_document_form_is_named_not_dropped(world):
    rows, _ = world
    rows["f1"] = _meta(filename="budget.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    document = _read()

    assert document.fmt is None
    assert "budget.xlsx" in document.text


def test_an_unprepared_source_over_the_cap_is_too_large(world):
    rows, _ = world
    rows["f1"] = _meta(size=9 * 1024 * 1024)

    with pytest.raises(WholeDocumentError) as big:
        asyncio.run(read_whole_document("alice", "src", "f1", max_source_bytes=8 * 1024 * 1024))
    assert big.value.reason == "too_large"
