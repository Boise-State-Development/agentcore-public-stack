"""Document token estimation (apis/shared/files/document_tokens.py).

The regression this guards: ``bytes/4`` scored a 27,578-byte, 60-page PDF at
6,894 tokens when the measured first write was ~94K (dev session ``61de2256``,
2026-09-16). Bedrock dual-encodes each PDF page as an image, so the dominant
term is page count, not file size. Fixtures are hand-built PDFs — never user
files — so the page-driven estimate is exercised for real.
"""

from __future__ import annotations

import io

import pytest

from apis.shared.files import document_tokens as dt

from .test_document_read import build_pdf


class TestPdfPageCount:
    def test_counts_pages(self):
        assert dt.pdf_page_count(build_pdf([f"p{i}" for i in range(1, 13)])) == 12

    @pytest.mark.parametrize("raw", [b"", b"not a pdf", b"%PDF-1.4\ngarbage", None, 12, "str"])
    def test_unparseable_is_none_never_raises(self, raw):
        assert dt.pdf_page_count(raw) is None


class TestEstimateDocumentTokens:
    def test_pdf_is_driven_by_pages_not_bytes(self):
        """The defect, stated as a test: a byte-small PDF with many pages."""
        raw = build_pdf([f"SECTION {i}" for i in range(1, 61)])
        tokens = dt.estimate_document_tokens("pdf", raw)
        assert tokens == 60 * dt.PDF_PAGE_TOKEN_ESTIMATE
        # ...and that is far above what bytes/4 would have said.
        assert tokens > 10 * (len(raw) // dt.CHARS_PER_TOKEN)

    def test_byte_heavy_pdf_is_never_scored_below_bytes_over_four(self):
        """max(), not replace: a scanned PDF whose bytes dominate keeps the
        old floor so the estimate can only move up."""
        raw = build_pdf(["one page"]) + b"%" + b"\0" * 400_000
        assert dt.estimate_document_tokens("pdf", raw) == len(raw) // dt.CHARS_PER_TOKEN

    @pytest.mark.parametrize("fmt", ["docx", "txt", "md", "html", "", None])
    def test_non_pdf_keeps_bytes_over_four(self, fmt):
        """Bedrock extracts these to text — no image channel to miss."""
        raw = b"x" * 40_000
        assert dt.estimate_document_tokens(fmt, raw) == 10_000

    def test_pdf_format_match_is_case_insensitive(self):
        raw = build_pdf(["a", "b"])
        assert dt.estimate_document_tokens("PDF", raw) == dt.estimate_document_tokens("pdf", raw)

    def test_unparseable_pdf_falls_back_to_bytes(self):
        raw = b"not a pdf at all" * 100
        assert dt.estimate_document_tokens("pdf", raw) == len(raw) // dt.CHARS_PER_TOKEN

    @pytest.mark.parametrize("raw", [None, "", 0, [], {}])
    def test_non_bytes_is_zero(self, raw):
        assert dt.estimate_document_tokens("pdf", raw) == 0

    def test_env_tunable(self, monkeypatch):
        """The constant is re-fittable from measured rows without a deploy."""
        monkeypatch.setattr(dt, "PDF_PAGE_TOKEN_ESTIMATE", 2_000)
        assert dt.estimate_document_tokens("pdf", build_pdf(["a", "b", "c"])) == 6_000


class TestAgainstTheDevMeasurement:
    def test_sixty_page_pdf_lands_near_the_measured_write(self):
        """Dev session 61de2256: 60 pages measured ~94K tokens. The estimate
        must be the same order of magnitude, and deliberately a little under
        (it gates an eviction)."""
        raw = build_pdf([f"SECTION {i} clause text" for i in range(1, 61)])
        tokens = dt.estimate_document_tokens("pdf", raw)
        measured = 94_000
        assert 0.7 * measured <= tokens <= measured, tokens
