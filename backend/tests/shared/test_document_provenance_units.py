"""``document_provenance_units``: the document side of source provenance.

The claim that matters here is the one the spec rests on — a *prepared*
document needs no new sidecar, because ``text.jsonl``'s records already are its
page markers, so this is one S3 GET and no parse.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apis.shared.files import document_read as mod
from apis.shared.files.document_read import DocumentReadError, document_provenance_units
from apis.shared.files.workspace import WorkspaceValidationError



def _meta(**overrides):
    base = dict(
        upload_id="up-1",
        filename="award.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        s3_bucket="bucket",
        s3_key="user-files/u1/s1/up-1/award.pdf",
        preparation=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def owned(monkeypatch):
    def _set(meta):
        monkeypatch.setattr(mod, "_get_owned_ready_file", AsyncMock(return_value=meta))

    return _set


class TestPreparedDocuments:
    @pytest.mark.asyncio
    async def test_a_prepared_pdf_reads_its_text_layer_and_never_parses(
        self, monkeypatch, owned
    ):
        """text.jsonl's per-page records ARE the persisted page markers — one
        GET, no pypdfium2, and no second marker sidecar to disagree with."""
        owned(_meta())
        monkeypatch.setattr(mod.large_document, "is_prepared", lambda meta: True)
        monkeypatch.setattr(mod, "_fetch_text_layer", AsyncMock(return_value=["p1", "p2"]))
        parsed = AsyncMock(side_effect=AssertionError("must not fetch bytes"))
        monkeypatch.setattr(mod, "_fetch_bytes", parsed)

        meta, fmt, texts = await document_provenance_units("u1", "s1", "up-1")
        assert fmt == "pdf"
        assert texts == ["p1", "p2"]
        assert meta.filename == "award.pdf"

    @pytest.mark.asyncio
    async def test_a_prepared_document_is_not_subject_to_the_source_size_bound(
        self, monkeypatch, owned
    ):
        """It is read through its sidecars, so a 50 MB reference document is
        verifiable exactly as document_read's other modes read it."""
        owned(_meta(size_bytes=50 * 1024 * 1024))
        monkeypatch.setattr(mod.large_document, "is_prepared", lambda meta: True)
        monkeypatch.setattr(mod, "_fetch_text_layer", AsyncMock(return_value=["p1"]))

        _, _, texts = await document_provenance_units("u1", "s1", "up-1")
        assert texts == ["p1"]


class TestUnpreparedDocuments:
    @pytest.mark.asyncio
    async def test_a_docx_comes_back_as_paragraphs(self, monkeypatch, owned):
        owned(
            _meta(
                filename="contract.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )
        monkeypatch.setattr(mod.large_document, "is_prepared", lambda meta: False)
        monkeypatch.setattr(mod, "_fetch_bytes", AsyncMock(return_value=b"zip"))
        monkeypatch.setattr(mod, "docx_paragraphs", lambda raw: ["one", "two"])

        _, fmt, texts = await document_provenance_units("u1", "s1", "up-1")
        assert fmt == "docx"
        assert texts == ["one", "two"]

    @pytest.mark.asyncio
    async def test_a_text_file_comes_back_as_lines_without_html_stripping(
        self, monkeypatch, owned
    ):
        """Verified against exactly what document_read's pattern mode searches,
        not a second, differently-cleaned rendering of the same file."""
        owned(_meta(filename="notes.html", mime_type="text/html"))
        monkeypatch.setattr(mod.large_document, "is_prepared", lambda meta: False)
        monkeypatch.setattr(mod, "_fetch_bytes", AsyncMock(return_value=b"<p>a</p>\n<p>b</p>"))

        _, fmt, texts = await document_provenance_units("u1", "s1", "up-1")
        assert fmt == "html"
        assert texts == ["<p>a</p>", "<p>b</p>"]

    @pytest.mark.asyncio
    async def test_an_oversized_unprepared_document_is_refused(self, monkeypatch, owned):
        owned(_meta(size_bytes=mod._MAX_DOCUMENT_BYTES + 1))
        monkeypatch.setattr(mod.large_document, "is_prepared", lambda meta: False)
        with pytest.raises(DocumentReadError):
            await document_provenance_units("u1", "s1", "up-1")


class TestRefusals:
    @pytest.mark.asyncio
    async def test_identity_and_upload_id_are_required(self):
        with pytest.raises(WorkspaceValidationError):
            await document_provenance_units("u1", "s1", "")
        with pytest.raises(Exception):
            await document_provenance_units("", "s1", "up-1")

    @pytest.mark.asyncio
    async def test_a_non_document_says_there_is_nothing_to_verify_against(
        self, monkeypatch, owned
    ):
        owned(_meta(filename="budget.xlsx", mime_type="application/vnd.ms-excel"))
        with pytest.raises(DocumentReadError, match="nothing to"):
            await document_provenance_units("u1", "s1", "up-1")


def test_units_text_uses_the_same_extractors_as_the_retrieval_modes():
    """A quote must be verified against the text document_read would have
    shown, so the extraction is the same three functions, not a copy."""
    import inspect

    source = inspect.getsource(mod._units_text)
    assert "_pdf_page_text" in source
    assert "docx_paragraphs" in source
    assert "splitlines" in source
