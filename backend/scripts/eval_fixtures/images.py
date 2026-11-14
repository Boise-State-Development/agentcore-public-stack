"""PNG fixtures drawn with Pillow: a labelled bar chart and a screenshot of a dialog.

Pillow is already a pinned backend dependency. Text uses Pillow's bundled
scalable default font, so a fixture renders the same on any host with the
same Pillow version, and every value a case asks about is printed on the
image as well as drawn: the cases measure reading an image, not estimating a
bar's height.
"""

from __future__ import annotations

import io
from typing import Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

_INK = (17, 24, 39)
_MUTED = (75, 85, 99)
_BAR = (37, 99, 235)
_PAPER = (255, 255, 255)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False)
    return out.getvalue()


def bar_chart(title: str, bars: Sequence[Tuple[str, int]], *, width: int = 900, height: int = 560) -> bytes:
    """Vertical bars, each labelled with its category below and its value above."""
    image = Image.new("RGB", (width, height), _PAPER)
    draw = ImageDraw.Draw(image)
    draw.text((40, 24), title, fill=_INK, font=_font(30))

    left, right, top, bottom = 80, width - 40, 110, height - 80
    draw.line((left, bottom, right, bottom), fill=_MUTED, width=2)
    peak = max(value for _, value in bars) or 1
    slot = (right - left) / len(bars)
    for i, (label, value) in enumerate(bars):
        x0 = left + i * slot + slot * 0.2
        x1 = left + (i + 1) * slot - slot * 0.2
        y0 = bottom - (bottom - top) * value / peak
        draw.rectangle((x0, y0, x1, bottom), fill=_BAR)
        value_text = f"{value:,}"
        draw.text(((x0 + x1) / 2, y0 - 12), value_text, fill=_INK, font=_font(24), anchor="ms")
        draw.text(((x0 + x1) / 2, bottom + 14), label, fill=_INK, font=_font(22), anchor="mt")
    return _png(image)


def dialog(title: str, lines: Sequence[str], button: str = "OK", *, width: int = 820, height: int = 360) -> bytes:
    """A desktop-style dialog: a title bar, a message and a button, as in a user's screenshot."""
    image = Image.new("RGB", (width, height), (229, 231, 235))
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, width - 30, height - 30), fill=_PAPER, outline=(156, 163, 175), width=2)
    draw.rectangle((30, 30, width - 30, 78), fill=(243, 244, 246), outline=(156, 163, 175), width=2)
    draw.text((50, 42), title, fill=_INK, font=_font(24))
    y = 108
    for line in lines:
        draw.text((60, y), line, fill=_INK, font=_font(24))
        y += 40
    draw.rectangle((width - 190, height - 100, width - 60, height - 56), fill=_BAR)
    draw.text((width - 125, height - 78), button, fill=_PAPER, font=_font(22), anchor="mm")
    return _png(image)
