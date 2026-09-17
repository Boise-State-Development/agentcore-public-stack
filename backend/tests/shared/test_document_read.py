"""Document read service (apis/shared/files/document_read.py).

The retrieval primitive behind ``document_read``: PDF page ranges re-assembled
as native document blocks (bounded by ``max_pages``), pattern search over the
text layer with page numbers, bounded text for DOCX and text-family files, the
session listing, and the tool-injection gate. Fixtures are hand-built PDFs
and DOCX archives — never user files — rendered through the same pypdfium2
the service uses, so page identity under re-assembly is exercised for real.
"""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.shared.files import document_read as dr
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.files.workspace import WorkspaceFileNotFoundError, WorkspaceValidationError

MODULE = "apis.shared.files.document_read"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_pdf(page_texts):
    """A minimal multi-page PDF with one Helvetica text line per page."""
    objs = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    contents = []
    for text in page_texts:
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode()
        contents.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
    pages_id = len(objs) + len(page_texts) + 1
    page_ids = [
        add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {c} 0 R >>".encode()
        )
        for c in contents
    ]
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()) == pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n")
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def build_docx(paragraphs):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


PAGES = [f"Page {i} alpha beta" for i in range(1, 11)] + ["The termination clause survives", "Page 12 end"]


def _meta(upload_id="up-1", filename="policy.pdf", mime="application/pdf", size=1000, user="u1", session="s1"):
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id=session, filename=filename, mime_type=mime,
        size_bytes=size, s3_key=f"user-files/{user}/{session}/{upload_id}/{filename}", s3_bucket="b",
        status=FileStatus.READY,
    )


@pytest.fixture
def stored(monkeypatch):
    """Patch the metadata lookup and the S3 fetch; returns a setter."""
    state = {}

    async def _owned(user_id, upload_id):
        meta = state.get("meta")
        if meta is None or meta.user_id != user_id or meta.upload_id != upload_id:
            raise WorkspaceFileNotFoundError(f"No file with id '{upload_id}'")
        return meta

    monkeypatch.setattr(f"{MODULE}._get_owned_ready_file", _owned)
    monkeypatch.setattr(f"{MODULE}._get_object_bytes", lambda bucket, key: state["raw"])

    def _set(meta, raw):
        state["meta"], state["raw"] = meta, raw

    return _set


# ---------------------------------------------------------------------------
# Classification and argument parsing
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "mime,filename,expected",
        [
            ("application/pdf", "a.pdf", "pdf"),
            ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx", "docx"),
            ("text/plain", "a.txt", "txt"),
            ("text/markdown", "a.md", "md"),
            ("text/html", "a.html", "html"),
            ("text/csv", "a.csv", None),  # spreadsheet tools
            ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx", None),
            ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "a.pptx", None),
            ("image/png", "a.png", None),
        ],
    )
    def test_document_class(self, mime, filename, expected):
        assert dr.document_format_for(mime, filename) == expected
        assert dr.is_document_class(mime, filename) is (expected is not None)

    @pytest.mark.parametrize(
        "value,expected",
        [("4-7", (4, 7)), ("4", (4, 4)), (" 4 .. 7 ", (4, 7)), ("4:7", (4, 7)), ({"start": 2, "end": 3}, (2, 3)),
         ([5, 6], (5, 6)), (3, (3, 3)), ("", None), (None, None)],
    )
    def test_parse_page_range(self, value, expected):
        assert dr.parse_page_range(value) == expected

    @pytest.mark.parametrize("value", ["0-3", "7-4", "abc", "1-2-3"])
    def test_bad_page_range_is_a_validation_error(self, value):
        with pytest.raises(WorkspaceValidationError):
            dr.parse_page_range(value)

    def test_max_pages_is_clamped_to_the_hard_cap(self):
        assert dr.clamp_max_pages(None) == dr.DOCUMENT_READ_MAX_PAGES
        assert dr.clamp_max_pages(0) == 1
        assert dr.clamp_max_pages(10_000) == dr.DOCUMENT_READ_HARD_MAX_PAGES
        assert dr.clamp_max_pages("x") == dr.DOCUMENT_READ_MAX_PAGES


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


class TestPdf:
    @pytest.mark.asyncio
    async def test_page_range_returns_only_those_pages_as_a_native_block(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="4-7")

        assert result.mode == "pages"
        assert result.pages_returned == 4
        assert result.payload["pages"] == {"start": 4, "end": 7}
        assert result.payload["page_count"] == 12
        assert result.payload["truncated_to_max_pages"] is False
        block = result.document_block
        assert block["document"]["format"] == "pdf"
        assert block["document"]["name"].startswith("policy p4-7 ")
        # The slice really is pages 4–7 of the original, in order.
        import pypdfium2 as pdfium

        sub = pdfium.PdfDocument(io.BytesIO(block["document"]["source"]["bytes"]))
        assert len(sub) == 4
        assert dr._pdf_page_text(sub, 0) == "Page 4 alpha beta"
        assert dr._pdf_page_text(sub, 3) == "Page 7 alpha beta"
        sub.close()
        assert "page k is original page 4 + k - 1" in result.payload["page_numbering"]

    @pytest.mark.asyncio
    async def test_max_pages_caps_a_range_and_points_at_the_continuation(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="1-12", max_pages=3)
        assert result.pages_returned == 3
        assert result.payload["pages"] == {"start": 1, "end": 3}
        assert result.payload["truncated_to_max_pages"] is True
        assert result.payload["next_start"] == 4

    @pytest.mark.asyncio
    async def test_hard_cap_holds_whatever_the_model_asks(self, stored):
        stored(_meta(), build_pdf([f"p{i}" for i in range(1, 41)]))
        result = await dr.read_document("u1", "s1", "up-1", page_range="1-40", max_pages=999)
        assert result.pages_returned == dr.DOCUMENT_READ_HARD_MAX_PAGES

    @pytest.mark.asyncio
    async def test_range_past_the_end_clips_or_rejects(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="11-30")
        assert result.payload["pages"] == {"start": 11, "end": 12}
        with pytest.raises(WorkspaceValidationError):
            await dr.read_document("u1", "s1", "up-1", page_range="13-14")

    @pytest.mark.asyncio
    async def test_pattern_returns_page_numbers_and_context(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", pattern="TERMINATION clause")
        assert result.mode == "pattern"
        assert result.document_block is None
        assert result.payload["pages_matched"] == [11]
        assert result.payload["matches"][0]["page"] == 11
        assert "termination" in result.payload["matches"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_invalid_regex_falls_back_to_a_literal_search(self, stored):
        stored(_meta(), build_pdf(["cost (usd", "other"]))
        result = await dr.read_document("u1", "s1", "up-1", pattern="cost (usd")
        assert result.payload["pages_matched"] == [1]

    @pytest.mark.asyncio
    async def test_no_range_no_pattern_returns_a_bounded_page_index(self, stored, monkeypatch):
        monkeypatch.setattr(dr, "DOCUMENT_READ_INDEX_PAGES", 5)
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1")
        assert result.mode == "index"
        assert result.payload["page_count"] == 12
        assert len(result.payload["page_index"]) == 5
        assert result.payload["page_index"][0] == {"page": 1, "snippet": "Page 1 alpha beta"}
        assert result.payload["index_truncated"] is True

    @pytest.mark.asyncio
    async def test_corrupt_pdf_is_a_conversational_error(self, stored):
        stored(_meta(), b"not a pdf")
        with pytest.raises(dr.DocumentReadError):
            await dr.read_document("u1", "s1", "up-1", page_range="1")

    def test_slice_names_are_bedrock_safe_and_never_collide(self):
        a = dr._unique_document_name("BBR 5.0 Policy_Form (final).pdf", "p4-7")
        b = dr._unique_document_name("BBR 5.0 Policy_Form (final).pdf", "p4-7")
        assert a != b
        assert a.startswith("BBR 5 0 Policy Form (final) p4-7 ")
        import re

        assert re.fullmatch(r"[a-zA-Z0-9\s\-\(\)\[\]]+", a) and "  " not in a


# ---------------------------------------------------------------------------
# DOCX and text
# ---------------------------------------------------------------------------


class TestDocxAndText:
    DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    @pytest.mark.asyncio
    async def test_docx_text_is_bounded_with_offset_continuation(self, stored, monkeypatch):
        monkeypatch.setattr(dr, "WORKSPACE_READ_MAX_BYTES", 20)
        stored(_meta(filename="memo.docx", mime=self.DOCX_MIME), build_docx(["Alpha paragraph", "Beta paragraph"]))
        first = await dr.read_document("u1", "s1", "up-1")
        assert first.mode == "text" and first.payload["paragraph_count"] == 2
        assert first.payload["truncated"] is True and first.payload["next_offset"] == 20
        second = await dr.read_document("u1", "s1", "up-1", offset=first.payload["next_offset"])
        assert first.payload["content"] + second.payload["content"] == "Alpha paragraph\n\nBeta paragraph"
        assert second.payload["truncated"] is False

    @pytest.mark.asyncio
    async def test_docx_pattern_reports_paragraph_numbers(self, stored):
        stored(_meta(filename="memo.docx", mime=self.DOCX_MIME), build_docx(["Intro", "Budget is 4 million", "Close"]))
        result = await dr.read_document("u1", "s1", "up-1", pattern=r"\d+ million")
        assert result.payload["unit"] == "paragraph"
        assert result.payload["matches"] == [
            {"line": 2, "text": "Budget is 4 million", "before": ["Intro"], "after": ["Close"]}
        ]
        assert result.format == "docx"

    @pytest.mark.asyncio
    async def test_page_range_on_a_non_pdf_is_rejected(self, stored):
        stored(_meta(filename="memo.docx", mime=self.DOCX_MIME), build_docx(["x"]))
        with pytest.raises(WorkspaceValidationError):
            await dr.read_document("u1", "s1", "up-1", page_range="1-2")

    @pytest.mark.asyncio
    async def test_text_family_delegates_to_the_workspace_read(self, stored, monkeypatch):
        stored(_meta(filename="notes.md", mime="text/markdown"), b"# heading\nbody line\n")
        delegate = AsyncMock(return_value={"content": "# heading", "offset": 0, "truncated": True, "next_offset": 9})
        monkeypatch.setattr(f"{MODULE}.read_workspace_file", delegate)
        result = await dr.read_document("u1", "s1", "up-1")
        delegate.assert_awaited_once_with("u1", "up-1", offset=0)
        assert result.payload["content"] == "# heading" and result.payload["next_offset"] == 9

    @pytest.mark.asyncio
    async def test_text_family_pattern_greps_lines(self, stored):
        stored(_meta(filename="notes.txt", mime="text/plain"), b"one\ntwo Fish\nthree\n")
        result = await dr.read_document("u1", "s1", "up-1", pattern="fish")
        assert result.payload["matches"][0]["line"] == 2

    @pytest.mark.asyncio
    async def test_non_document_files_are_refused(self, stored):
        stored(_meta(filename="data.csv", mime="text/csv"), b"a,b\n")
        with pytest.raises(dr.DocumentReadError):
            await dr.read_document("u1", "s1", "up-1")

    @pytest.mark.asyncio
    async def test_missing_upload_id_and_negative_offset_are_validation_errors(self):
        with pytest.raises(WorkspaceValidationError):
            await dr.read_document("u1", "s1", "")
        with pytest.raises(WorkspaceValidationError):
            await dr.read_document("u1", "s1", "up-1", offset=-1)


# ---------------------------------------------------------------------------
# Listing and the injection gate
# ---------------------------------------------------------------------------


class TestListingAndGate:
    def _repo(self, monkeypatch, files):
        repo = MagicMock()
        repo.list_session_files = AsyncMock(return_value=files)
        monkeypatch.setattr(f"{MODULE}.get_file_upload_repository", lambda: repo)
        return repo

    @pytest.mark.asyncio
    async def test_listing_keeps_only_this_users_readable_documents(self, monkeypatch):
        repo = self._repo(monkeypatch, [
            _meta("up-1", "policy.pdf"),
            _meta("up-2", "data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            _meta("up-3", "other.pdf", user="someone-else"),
            _meta("up-4", "notes.md", "text/markdown"),
        ])
        listing = await dr.list_session_documents("u1", "s1")
        repo.list_session_files.assert_awaited_once_with("s1", status=FileStatus.READY)
        assert [d["upload_id"] for d in listing["documents"]] == ["up-1", "up-4"]
        assert listing["documents"][0]["modes"] == ["page_range", "pattern"]
        assert listing["documents"][1]["modes"] == ["pattern", "text"]
        assert listing["count"] == 2

    @pytest.mark.asyncio
    async def test_gate_is_true_only_with_a_readable_document(self, monkeypatch):
        self._repo(monkeypatch, [_meta("up-2", "data.csv", "text/csv")])
        assert await dr.session_has_documents("u1", "s1") is False
        self._repo(monkeypatch, [_meta("up-2", "data.csv", "text/csv"), _meta("up-1", "a.pdf")])
        assert await dr.session_has_documents("u1", "s1") is True

    @pytest.mark.asyncio
    async def test_identity_is_mandatory(self):
        with pytest.raises(Exception):
            await dr.list_session_documents("", "s1")
        with pytest.raises(Exception):
            await dr.session_has_documents("u1", "")
