"""The PDF first-page thumbnail the prepare Lambda writes (apis/shared/files/pdf_thumbnail.py)."""

from __future__ import annotations

import struct
import zlib

import pytest

from apis.shared.files.document_read import _open_pdf
from apis.shared.files.pdf_thumbnail import (
    THUMBNAIL_MAX_DIMENSION,
    _to_rgb,
    encode_png,
    render_first_page,
    thumbnail_key,
)
from tests.shared.test_document_read import build_pdf


def _decode(png: bytes) -> tuple[int, int, bytes]:
    """Width, height and the unfiltered pixel rows of an 8-bit RGB PNG."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", data[:10])
            assert (depth, colour) == (8, 2)
            size = (width, height)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    assert size is not None
    width, height = size
    raw = zlib.decompress(idat)
    rows = b"".join(raw[y * (width * 3 + 1) + 1:(y + 1) * (width * 3 + 1)] for y in range(height))
    assert all(raw[y * (width * 3 + 1)] == 0 for y in range(height))
    return width, height, rows


def test_encode_png_round_trips_its_pixels():
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 10, 20, 30])  # 2x2
    assert _decode(encode_png(pixels, 2, 2)) == (2, 2, pixels)


def test_encode_png_refuses_a_short_buffer():
    with pytest.raises(ValueError):
        encode_png(b"\x00" * 5, 2, 2)


def test_rgba_rows_with_padding_become_packed_rgb():
    # Two pixels per row, 4 channels, one padding byte per row (stride 9).
    buffer = bytes([1, 2, 3, 9, 4, 5, 6, 9, 0, 7, 8, 9, 9, 10, 11, 12, 9, 0])
    assert _to_rgb(buffer, 2, 2, 9, 4) == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])


def test_first_page_renders_within_the_bound():
    pdf = _open_pdf(build_pdf(["Quarterly report", "Page two"]))
    try:
        width, height, rows = _decode(render_first_page(pdf))
    finally:
        pdf.close()
    assert max(width, height) == THUMBNAIL_MAX_DIMENSION
    assert len(rows) == width * height * 3
    assert min(rows) < 128  # the page's text, not a blank image


def test_thumbnail_sits_in_the_uploads_derived_folder():
    assert thumbnail_key("user-files/u1/s1/up-1/report.pdf") == "user-files/u1/s1/up-1/_derived/thumb.png"
