"""One process-wide lock for every pypdfium2 call.

PDFium is not thread-safe (pypdfium2 README, *Incompatibility with Threading*),
and nothing in it detects a second thread: two threads inside the library at
once corrupt its global state and the process dies with SIGSEGV, with no
Python traceback to point at either caller. That is what killed a production
app-api task on 2026-10-30: two PDFs imported 90 ms apart, each parsed for its
digest on ``asyncio.to_thread``, so the two parses ran side by side.

Every path that touches pypdfium2 takes this lock — the digest, thumbnails,
``document_read``, the page-count estimate and the large-document preparation
Lambda — so the library only ever runs on one thread at a time, whatever the
callers around it do. The cost is that PDF parsing serializes within a
process, which is also roughly what PDFium would do anyway: it releases the
GIL but shares one allocator and one parser state.

Re-entrant on purpose: a document's lifetime (open, read pages, close) is held
under the lock as one unit, and the per-call helpers inside it (``_open_pdf``,
``_pdf_page_text``) take it again so a new caller that forgets the outer
guard is still serialized at the call boundary.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import wraps
from typing import Callable, Iterator, TypeVar

PDFIUM_LOCK = threading.RLock()

F = TypeVar("F", bound=Callable[..., object])


@contextmanager
def pdfium_lock() -> Iterator[None]:
    """Hold the PDFium lock for a block."""
    with PDFIUM_LOCK:
        yield


def serialized_pdfium(fn: F) -> F:
    """Run ``fn`` under the PDFium lock — for a function that opens a document
    and finishes with it before returning."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        with PDFIUM_LOCK:
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def pdfium_lock_held() -> bool:
    """Whether the calling thread holds the lock (tests, assertions)."""
    return PDFIUM_LOCK._is_owned()  # type: ignore[attr-defined]


__all__ = ["PDFIUM_LOCK", "pdfium_lock", "pdfium_lock_held", "serialized_pdfium"]
