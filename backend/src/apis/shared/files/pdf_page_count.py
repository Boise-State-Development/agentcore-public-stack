"""A PDF's page count at admission, without holding the PDF in memory.

``docs/specs/unified-attachment-delivery.md`` §2, §4.4: the ``token`` profile
refuses a PDF over its ``pdf_max_pages`` (1,000) when its upload completes, and records
the count on the row so a turn that arrives before the file's facts can still
judge the PDF by its pages (``attachment_plan``, ``facts_pending``).

PDFium opens the document through :class:`S3RangeReader`, a seekable reader
that fetches fixed-size blocks with ranged ``GetObject`` calls and keeps them.
Opening and counting reads only the trailer, the cross-reference data and the
page tree root: for a 1,200-page PDF, 31 reads totalling 37 KB, which is one or
two 256 KB blocks. A 50 MB upload is never held in app-api.

Every PDFium call runs under ``serialized_pdfium`` (PDFium is not thread-safe;
two concurrent calls crashed an app-api task in production).
"""

from __future__ import annotations

import io
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: One ranged GET fetches this much.
BLOCK_BYTES = 256 * 1024
#: A reader that would fetch more than this gives up (a damaged PDF whose
#: cross-reference is missing makes PDFium scan the whole file to rebuild it).
MAX_FETCH_BYTES = 8 * 1024 * 1024


class PageCountUnavailable(Exception):
    """The count could not be read; the caller admits the file and the facts decide later."""


class S3RangeReader(io.RawIOBase):
    """A read-only, seekable view of one S3 object, fetched in cached blocks."""

    def __init__(self, s3_client: Any, bucket: str, key: str, size: int, *, max_fetch_bytes: int = MAX_FETCH_BYTES):
        super().__init__()
        self._s3 = s3_client
        self._bucket = bucket
        self._key = key
        self._size = int(size)
        self._pos = 0
        self._blocks: Dict[int, bytes] = {}
        self._max_fetch = int(max_fetch_bytes)
        self.fetched_bytes = 0
        self.requests = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self._size + offset
        self._pos = max(0, self._pos)
        return self._pos

    def _block(self, index: int) -> bytes:
        block = self._blocks.get(index)
        if block is not None:
            return block
        start = index * BLOCK_BYTES
        end = min(self._size, start + BLOCK_BYTES) - 1
        if self.fetched_bytes + (end - start + 1) > self._max_fetch:
            raise PageCountUnavailable("PDF needs more than the page-count read budget")
        response = self._s3.get_object(Bucket=self._bucket, Key=self._key, Range=f"bytes={start}-{end}")
        block = response["Body"].read()
        self.requests += 1
        self.fetched_bytes += len(block)
        self._blocks[index] = block
        return block

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        wanted = len(view)
        written = 0
        while written < wanted and self._pos < self._size:
            index, offset = divmod(self._pos, BLOCK_BYTES)
            block = self._block(index)
            chunk = block[offset : offset + (wanted - written)]
            if not chunk:
                break
            view[written : written + len(chunk)] = chunk
            written += len(chunk)
            self._pos += len(chunk)
        return written


def count_pages(source: Any) -> int:
    """Pages in a PDF, from bytes or a seekable reader. Raises :class:`PageCountUnavailable`."""
    from apis.shared.files.pdfium_lock import serialized_pdfium

    @serialized_pdfium
    def _count() -> int:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(source)
        try:
            return len(pdf)
        finally:
            pdf.close()

    try:
        return int(_count())
    except PageCountUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - any PDFium failure leaves the count to the facts
        raise PageCountUnavailable(str(e)) from e


def count_s3_pdf_pages(s3_client: Any, bucket: str, key: str, size: int) -> int:
    """Pages in a stored PDF, read by ranges. Raises :class:`PageCountUnavailable`."""
    reader = S3RangeReader(s3_client, bucket, key, size)
    pages = count_pages(reader)
    logger.info(
        "Counted a PDF's pages by range: pages=%d requests=%d fetched_bytes=%d",
        pages,
        reader.requests,
        reader.fetched_bytes,
    )
    return pages


def too_many_pages_message(filename: str, pages: int, limit: int) -> str:
    return f"{filename} has {pages:,} pages. A PDF can have at most {limit:,} pages; split it and attach the parts."


__all__ = [
    "BLOCK_BYTES",
    "PageCountUnavailable",
    "S3RangeReader",
    "count_pages",
    "count_s3_pdf_pages",
    "too_many_pages_message",
]
