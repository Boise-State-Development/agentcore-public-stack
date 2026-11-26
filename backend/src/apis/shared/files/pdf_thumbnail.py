"""A PDF's first page as a PNG for the attachment card, rendered by the prepare Lambda.

The attachment card shows a PDF's first page. app-api used to render it with
PDFium on the request that first asked for it, and PDFium has crashed app-api
tasks in production. The prepare Lambda already opens every PDF at upload (the
facts pass for a small one, the full preparation for a large one), so it
renders the page there and stores it at ``_derived/thumb-w720.png``, which app-api
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

#: Width of the thumbnail, in pixels. The card draws the page
#: ``object-cover object-top``, so its width fills the sheet and the bottom is
#: cropped: width is the side that has to be sharp. The widest sheet (an
#: output card's left half) is about 360 logical pixels, so 720 covers a 2×
#: screen. A dense text page is about 150 KB at this width.
THUMBNAIL_WIDTH = 720

#: Tallest thumbnail, in pixels. A page taller than 2:1 (a receipt, a long
#: scroll) renders narrower rather than many thousands of pixels tall, since
#: the card only ever shows the top.
THUMBNAIL_MAX_HEIGHT = 2 * THUMBNAIL_WIDTH

#: The size is in the name so a change of size re-renders: app-api serves an
#: older name while it queues the new one (``LEGACY_THUMBNAIL_NAMES``).
THUMBNAIL_NAME = f"thumb-w{THUMBNAIL_WIDTH}.png"

#: Names an earlier renderer stored under ``_derived/``: 256 px on the longest
#: side, which a 2× screen drew about 3× too large.
LEGACY_THUMBNAIL_NAMES = ("thumb.png",)


class ThumbnailRenderError(Exception):
    """The PDF opened but its first page could not be rendered."""


def thumbnail_key(s3_key: str) -> str:
    """``.../{upload_id}/_derived/thumb-w720.png`` for an original at ``s3_key``."""
    return f"{derived_prefix(s3_key)}{THUMBNAIL_NAME}"


def legacy_thumbnail_keys(s3_key: str) -> tuple[str, ...]:
    """Where an earlier, smaller thumbnail of ``s3_key`` may be stored."""
    return tuple(f"{derived_prefix(s3_key)}{name}" for name in LEGACY_THUMBNAIL_NAMES)


def thumbnail_scale(width: float, height: float) -> float:
    """PDFium's render scale for a page of ``width`` × ``height`` points.

    ``THUMBNAIL_WIDTH`` across, unless that would pass ``THUMBNAIL_MAX_HEIGHT``.
    """
    if width <= 0 or height <= 0:
        raise ThumbnailRenderError("PDF page has zero dimensions")
    return min(THUMBNAIL_WIDTH / width, THUMBNAIL_MAX_HEIGHT / height)


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
        bitmap = page.render(scale=thumbnail_scale(*page.get_size()), rev_byteorder=True)
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
