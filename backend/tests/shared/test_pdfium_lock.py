"""Every pypdfium2 call runs under the process-wide PDFium lock.

PDFium is not thread-safe. Two document digests built on ``asyncio.to_thread``
at once (two PDFs imported 90 ms apart) ran PDFium on two threads and the
production app-api process died with SIGSEGV on 2026-10-30. These tests put a
fake ``pypdfium2`` in place that records whether the lock is held on every
entry, then drive each PDF code path through it; one more test runs the real
library from four threads at once and expects the serialized result.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from apis.shared.files import document_digest as dd
from apis.shared.files import document_read as dr
from apis.shared.files import document_tokens as dt
from apis.shared.files.pdfium_lock import PDFIUM_LOCK, pdfium_lock_held, serialized_pdfium
from tests.shared.test_document_read import build_pdf


class _Probe:
    """What the fake records: lock state on every entry, and how many threads
    are inside the "library" at once."""

    def __init__(self, delay: float = 0.0):
        self.unlocked: list[str] = []
        self.inside = 0
        self.max_inside = 0
        self.delay = delay
        self._guard = threading.Lock()

    def enter(self, what: str) -> None:
        if not pdfium_lock_held():
            self.unlocked.append(what)
        with self._guard:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        if self.delay:
            time.sleep(self.delay)

    def leave(self) -> None:
        with self._guard:
            self.inside -= 1


def _fake_pdfium(probe: _Probe, pages: int = 2) -> types.ModuleType:
    class TextPage:
        def get_text_bounded(self):
            probe.enter("get_text_bounded")
            probe.leave()
            return "Heading\nbody text"

        def close(self):
            probe.enter("textpage.close")
            probe.leave()

    class Bitmap:
        def to_pil(self):
            class Img:
                def save(self, buffer, format=None, optimize=None):
                    buffer.write(b"png")

            return Img()

    class Page:
        def get_textpage(self):
            probe.enter("get_textpage")
            probe.leave()
            return TextPage()

        def get_size(self):
            probe.enter("get_size")
            probe.leave()
            return (612.0, 792.0)

        def render(self, scale=1.0):
            probe.enter("render")
            probe.leave()
            return Bitmap()

        def close(self):
            probe.enter("page.close")
            probe.leave()

    class PdfDocument:
        def __init__(self, src=None):
            probe.enter("PdfDocument")
            probe.leave()
            self._n = pages

        @classmethod
        def new(cls):
            doc = cls.__new__(cls)
            probe.enter("PdfDocument.new")
            probe.leave()
            doc._n = 0
            return doc

        def __len__(self):
            return self._n

        def __getitem__(self, i):
            probe.enter("__getitem__")
            probe.leave()
            return Page()

        def import_pages(self, other, pages=None):
            probe.enter("import_pages")
            probe.leave()
            self._n += len(pages or [])

        def save(self, buffer):
            probe.enter("save")
            probe.leave()
            buffer.write(b"%PDF-fake")

        def close(self):
            probe.enter("close")
            probe.leave()

    mod = types.ModuleType("pypdfium2")
    mod.PdfDocument = PdfDocument
    return mod


@pytest.fixture()
def probe(monkeypatch):
    p = _Probe()
    monkeypatch.setitem(sys.modules, "pypdfium2", _fake_pdfium(p))
    return p


class TestEveryEntryPointHoldsTheLock:
    def test_digest_units(self, probe):
        unit, texts = dd._units_for("pdf", b"%PDF")
        assert unit == "page" and len(texts) == 2
        assert probe.unlocked == []

    def test_digest_outline_and_sample(self, probe):
        dd.extract_outline("pdf", b"%PDF")
        dd.text_sample("pdf", b"%PDF")
        dd.document_text("pdf", b"%PDF", 100)
        assert probe.unlocked == []

    def test_page_count(self, probe):
        assert dt.pdf_page_count(b"%PDF") == 2
        assert probe.unlocked == []

    def test_document_read_modes(self, probe):
        base = {"upload_id": "u", "filename": "a.pdf"}
        dr._pdf_pages(b"%PDF", (1, 2), 10, base, "a.pdf")
        dr._pdf_pages_text(b"%PDF", (1, 2), 10, base)
        dr._pdf_pattern(b"%PDF", "Heading", base)
        dr._pdf_index(b"%PDF", base)
        dr._assemble_from_shards([((1, 2), b"%PDF")], 1, 2)
        dr._units_text("pdf", b"%PDF")
        assert probe.unlocked == []

    def test_thumbnail_render(self, probe):
        from apis.app_api.files.thumbnails import ThumbnailRenderer

        png = ThumbnailRenderer().render("application/pdf", b"%PDF")
        assert png == b"png"
        assert probe.unlocked == []

    def test_large_document_prepare(self, probe, monkeypatch):
        from lambdas.large_document_prepare import handler as h

        monkeypatch.setattr(h, "_put", lambda *a, **k: None)
        meta = types.SimpleNamespace(s3_bucket="b", s3_key="user-files/u/s/up/a.pdf")
        texts = h._prepare_pdf(object(), meta, b"%PDF", shard_pages=1)
        assert len(texts) == 2
        assert probe.unlocked == []

    def test_the_per_call_helpers_lock_on_their_own(self, probe):
        """A caller that forgets the outer guard is still serialized at the
        call boundary: ``_open_pdf`` and ``_pdf_page_text`` take the lock."""
        pdf = dr._open_pdf(b"%PDF")
        dr._pdf_page_text(pdf, 0)
        assert probe.unlocked == []


class TestConcurrency:
    def test_fake_library_is_never_entered_by_two_threads(self, monkeypatch):
        p = _Probe(delay=0.002)
        monkeypatch.setitem(sys.modules, "pypdfium2", _fake_pdfium(p, pages=5))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: dd.extract_outline("pdf", b"%PDF"), range(8)))
        assert p.max_inside == 1
        assert p.unlocked == []

    def test_real_library_from_four_threads_matches_sequential(self):
        pytest.importorskip("pypdfium2")
        pdfs = [build_pdf([f"Doc {d} page {i}" for i in range(6)]) for d in range(4)]
        expected = [dd.extract_outline("pdf", raw).count for raw in pdfs]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda raw: dd.extract_outline("pdf", raw).count, pdfs * 3))
        assert results == expected * 3


class TestLockPrimitive:
    def test_serialized_pdfium_is_reentrant(self):
        @serialized_pdfium
        def inner():
            return pdfium_lock_held()

        @serialized_pdfium
        def outer():
            return inner()

        assert outer() is True
        assert pdfium_lock_held() is False

    def test_lock_is_released_on_exception(self):
        @serialized_pdfium
        def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            boom()
        assert PDFIUM_LOCK.acquire(blocking=False)
        PDFIUM_LOCK.release()
