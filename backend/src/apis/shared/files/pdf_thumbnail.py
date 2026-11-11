"""A PDF's first page as a small PNG, rendered by the prepare Lambda.

The attachment card shows a PDF's first page. app-api used to render it with
PDFium on the request that first asked for it, and PDFium has crashed app-api
tasks in production. The prepare Lambda already opens every PDF at upload (the
facts pass for a small one, the full preparation for a large one), so it
renders the page there and stores it at ``_derived/thumb.png``, which app-api
only presigns.

The PNG is encoded here with ``zlib`` rather than Pillow, which the Lambda
image does not carry: an 8-bit RGB image, one filter byte per row.
"""

from __future__ import annotations

import io
import struct
import zlib
from typing import Any

from apis.shared.files.large_document import derived_prefix

#: Longest side of the thumbnail, in pixels. The card's preview is about
#: 240×128 logical pixels, so 256 covers a 2× screen.
THUMBNAIL_MAX_DIMENSION = 256

THUMBNAIL_NAME = "thumb.png"


class ThumbnailRenderError(Exception):
    """The PDF opened but its first page could not be rendered."""


def thumbnail_key(s3_key: str) -> str:
    """``.../{upload_id}/_derived/thumb.png`` for an original at ``s3_key``."""
    return f"{derived_prefix(s3_key)}{THUMBNAIL_NAME}"


def encode_png(rgb: bytes, width: int, height: int) -> bytes:
    """An 8-bit RGB PNG from ``height`` rows of ``width * 3`` bytes."""
    row = width * 3
    if width <= 0 or height <= 0 or len(rgb) < row * height:
        raise ValueError("pixel buffer does not match its dimensions")
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: none
        raw += rgb[y * row:(y + 1) * row]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def render_first_page(pdf: Any) -> bytes:
    """Page 1 of an open ``pypdfium2.PdfDocument`` as a PNG.

    The caller holds the PDFium lock (``serialized_pdfium``) and owns ``pdf``.
    """
    if len(pdf) == 0:
        raise ThumbnailRenderError("PDF has no pages")
    page = pdf[0]
    try:
        width, height = page.get_size()
        longest = max(width, height)
        if longest <= 0:
            raise ThumbnailRenderError("PDF page has zero dimensions")
        bitmap = page.render(scale=THUMBNAIL_MAX_DIMENSION / longest, rev_byteorder=True)
        try:
            w, h, stride = bitmap.width, bitmap.height, bitmap.stride
            channels = int(getattr(bitmap, "n_channels", 0) or (stride // w if w else 3))
            buffer = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        page.close()
    return encode_png(_to_rgb(buffer, w, h, stride, channels), w, h)


def _to_rgb(buffer: bytes, width: int, height: int, stride: int, channels: int) -> bytes:
    """Tightly packed RGB rows from PDFium's (possibly padded, possibly RGBA) rows."""
    out = io.BytesIO()
    for y in range(height):
        line = buffer[y * stride:y * stride + width * channels]
        if channels == 3:
            out.write(line)
        else:
            out.write(b"".join(line[i:i + 3] for i in range(0, len(line), channels)))
    return out.getvalue()
