"""A minimal PDF writer: Helvetica text, rules and images. Stdlib only.

Enough to build the harness fixtures deterministically without a PDF library:
the fixtures need control over **content-stream order** (the layout fixture
emits text in an order that differs from how it reads on the page, as many real
brochures do) and over **image-only pages** (the scanned fixture has no text
layer at all). Text is WinAnsi Helvetica, so fixture strings are plain ASCII.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

PAGE_WIDTH = 612
PAGE_HEIGHT = 792

#: Helvetica's average advance at 1pt, for wrapping. Approximate on purpose:
#: the fixtures only need lines that stay inside their column.
_AVG_CHAR_EM = 0.5


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap(text: str, size: float, width: float) -> List[str]:
    """Greedy word wrap for ``text`` at ``size`` pt inside ``width`` pt."""
    limit = max(10, int(width / (size * _AVG_CHAR_EM)))
    lines: List[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


@dataclass
class Image:
    width: int
    height: int
    data: bytes
    #: ``DeviceRGB`` raw bytes, or ``DeviceGray`` JPEG (``DCTDecode``).
    colorspace: str = "DeviceRGB"
    filter: Optional[str] = None


@dataclass
class Page:
    """One page: a content stream built with the helpers below, plus its images."""

    ops: List[str] = field(default_factory=list)
    images: List[Tuple[str, Image]] = field(default_factory=list)

    def text(self, x: float, y: float, text: str, size: float = 10, bold: bool = False) -> None:
        font = "F2" if bold else "F1"
        self.ops.append(f"BT /{font} {size:g} Tf {x:.2f} {y:.2f} Td ({escape(text)}) Tj ET")

    def paragraph(self, x: float, y: float, text: str, width: float, size: float = 10, leading: float = 13) -> float:
        """Wrapped text from the top line at ``y`` downwards. Returns the next free ``y``."""
        for line in wrap(text, size, width):
            self.text(x, y, line, size)
            y -= leading
        return y

    def rule(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.6) -> None:
        self.ops.append(f"{width:g} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def image(self, image: Image, x: float, y: float, w: float, h: float) -> None:
        name = f"Im{len(self.images) + 1}"
        self.images.append((name, image))
        self.ops.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q")


def build(pages: List[Page]) -> bytes:
    """Serialize ``pages`` into one PDF file."""
    objects: List[Optional[bytes]] = [None]  # 1-indexed

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects) - 1

    def reserve() -> int:
        return add(b"")

    def stream(entries: str, data: bytes) -> bytes:
        return f"<< {entries} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"

    catalog = reserve()
    pages_obj = reserve()
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    kids: List[int] = []
    for page in pages:
        xobjects = []
        for name, image in page.images:
            entries = (
                f"/Type /XObject /Subtype /Image /Width {image.width} /Height {image.height} "
                f"/ColorSpace /{image.colorspace} /BitsPerComponent 8"
            )
            if image.filter:
                entries += f" /Filter /{image.filter}"
            xobjects.append(f"/{name} {add(stream(entries, image.data))} 0 R")
        content = add(stream("", "\n".join(page.ops).encode("latin-1")))
        resources = f"<< /Font << /F1 {font} 0 R /F2 {bold} 0 R >>"
        if xobjects:
            resources += f" /XObject << {' '.join(xobjects)} >>"
        resources += " >>"
        kids.append(add(
            f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources {resources} /Contents {content} 0 R >>".encode()
        ))

    objects[pages_obj] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    objects[catalog] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number in range(1, len(objects)):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects)}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects)} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
