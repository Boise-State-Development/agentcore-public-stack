"""document_read's byte cap on a page range (apis/shared/files/document_read.py).

A page count is not a byte bound: in prod an 8-page slice of a scanned PDF was
86 MB, and a tool round's page reads are written to AgentCore Memory as one
event, which 413s past the 10 MB quota and force-stops the turn. A page range
read with ``max_document_bytes`` returns the longest run of pages that fits,
with a continuation, or the pages' text layer when not even the first fits.
Without a cap, a read is exactly what it was.
"""

from __future__ import annotations

import io

import pytest

from apis.shared.files import document_read as dr
from tests.shared.test_document_read import PAGES, _meta, build_pdf, patch_store


@pytest.fixture
def stored(monkeypatch):
    return patch_store(monkeypatch)


def _slice_size(raw: bytes, start: int, end: int) -> int:
    pdf = dr._open_pdf(raw)
    try:
        return len(dr._slice_pdf(pdf, start, end))
    finally:
        pdf.close()


def _page_texts(block) -> list:
    import pypdfium2 as pdfium

    sub = pdfium.PdfDocument(io.BytesIO(block["document"]["source"]["bytes"]))
    try:
        return [dr._pdf_page_text(sub, i) for i in range(len(sub))]
    finally:
        sub.close()


class TestFitToBytes:
    """The fitting loop, over a fake slicer whose size is known."""

    @staticmethod
    def _slicer(sizes):
        calls = []

        def slice_to(last):
            calls.append(last)
            return b"x" * sum(sizes[:last])

        return slice_to, calls

    def test_a_range_under_the_cap_is_sliced_once(self):
        slice_to, calls = self._slicer([1000] * 8)
        fit = dr._fit_to_bytes(slice_to, 1, 8, 10_000)
        assert (fit.end, len(fit.sliced), calls) == (8, 8000, [8])

    def test_no_cap_is_the_full_range(self):
        slice_to, calls = self._slicer([10_000_000] * 8)
        assert dr._fit_to_bytes(slice_to, 1, 8, None).end == 8
        assert calls == [8]

    def test_an_over_range_shrinks_to_what_fits(self):
        slice_to, _ = self._slicer([1000] * 8)
        fit = dr._fit_to_bytes(slice_to, 1, 8, 3500)
        assert len(fit.sliced) <= 3500
        assert 1 <= fit.end < 8

    def test_uneven_pages_still_fit(self):
        # A dense page late in the range: the proportional estimate overshoots
        # and has to come down again.
        slice_to, _ = self._slicer([100, 100, 100, 5000, 100, 100, 100, 100])
        fit = dr._fit_to_bytes(slice_to, 1, 8, 400)
        assert fit.end == 3 and len(fit.sliced) == 300

    @pytest.mark.parametrize("pages", [1, 3])
    def test_a_first_page_over_the_cap_returns_none_with_its_size(self, pages):
        slice_to, _ = self._slicer([9000, 10, 10][:pages])
        fit = dr._fit_to_bytes(slice_to, 1, pages, 5000)
        assert fit.sliced is None
        assert fit.last_size == 9000

    @pytest.mark.parametrize("fits", [0, 1, 7, 19])
    def test_the_answer_is_exact_and_the_work_bounded(self, fits):
        # Pages up to ``fits`` are tiny and the next is huge: whatever the
        # boundary, the search finds it in six slices or fewer (20 pages).
        calls = []

        def slice_to(last):
            calls.append(last)
            return b"x" * (10 * last if last <= fits else 10_000_000)

        fit = dr._fit_to_bytes(slice_to, 1, 20, 1000)
        assert fit.end == (fits if fits else 0)
        assert (fit.sliced is None) == (fits == 0)
        assert len(calls) <= 6


@pytest.mark.asyncio
class TestPageRangeCap:
    async def test_without_a_cap_the_payload_is_unchanged(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="4-7")
        assert result.pages_returned == 4
        assert "truncated_to_byte_budget" not in result.payload
        assert "byte_budget_note" not in result.payload
        assert result.payload["next_start"] is None

    async def test_zero_means_no_cap(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="4-7", max_document_bytes=0)
        assert result.pages_returned == 4

    async def test_a_range_over_the_cap_returns_fewer_pages_and_where_to_resume(self, stored):
        raw = build_pdf(PAGES)
        stored(_meta(), raw)
        cap = _slice_size(raw, 4, 5)
        assert _slice_size(raw, 4, 6) > cap, "fixture: three pages must not fit"

        result = await dr.read_document("u1", "s1", "up-1", page_range="4-7", max_document_bytes=cap)

        assert result.mode == "pages"
        assert result.pages_returned == 2
        assert result.bytes_returned <= cap
        assert result.payload["pages"] == {"start": 4, "end": 5}
        assert result.payload["truncated_to_byte_budget"] is True
        assert result.payload["next_start"] == 6
        assert "Read from page 6" in result.payload["byte_budget_note"]
        assert result.document_block["document"]["name"].startswith("policy p4-5 ")
        # The slice is the first pages of the request, in order.
        assert _page_texts(result.document_block) == ["Page 4 alpha beta", "Page 5 alpha beta"]

    async def test_a_page_too_large_to_attach_comes_back_as_its_text(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="4-5", max_document_bytes=1)

        assert result.mode == "pages_text"
        assert result.document_block is None
        assert result.payload["page_images_omitted"] == "byte_budget"
        assert result.payload["page_texts"] == [
            {"page": 4, "text": "Page 4 alpha beta"},
            {"page": 5, "text": "Page 5 alpha beta"},
        ]
        assert "more than" in result.payload["note"]

    async def test_a_scan_with_no_text_layer_says_so(self, stored):
        # Blank text must not read as blank pages.
        stored(_meta(), build_pdf(["", "", ""]))
        result = await dr.read_document("u1", "s1", "up-1", page_range="1-3", max_document_bytes=1)
        assert result.mode == "pages_text"
        assert "no text layer" in result.payload["note"]

    async def test_a_page_with_text_does_not_claim_to_be_a_scan(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document("u1", "s1", "up-1", page_range="1-3", max_document_bytes=1)
        assert "no text layer" not in result.payload["note"]

    async def test_the_note_says_whether_another_read_used_the_budget(self, stored):
        stored(_meta(), build_pdf(PAGES))
        shared = await dr.read_document("u1", "s1", "up-1", page_range="4", max_document_bytes=1)
        assert "read this range again on its own" in shared.payload["note"]
        assert "cannot be attached" not in shared.payload["note"]

    def test_a_page_over_the_whole_budget_cannot_be_retried(self):
        note = dr._over_budget_note(3, dr.DOCUMENT_READ_MAX_BATCH_BYTES + 1, dr.DOCUMENT_READ_MAX_BATCH_BYTES)
        assert "cannot be attached" in note
        assert "on its own" not in note

    async def test_a_text_only_read_ignores_the_cap(self, stored):
        stored(_meta(), build_pdf(PAGES))
        result = await dr.read_document(
            "u1", "s1", "up-1", page_range="4-5", text_only=True, max_document_bytes=1
        )
        assert result.mode == "pages_text"
        assert "page_images_omitted" not in result.payload


def test_the_round_budget_is_the_inline_attachment_budget():
    # Both bound one message, i.e. one Memory event, against the same quota.
    from apis.shared.files.models import INLINE_ATTACHMENTS_MAX_TOTAL_BYTES

    assert dr.DOCUMENT_READ_MAX_BATCH_BYTES == INLINE_ATTACHMENTS_MAX_TOTAL_BYTES == 7_000_000
