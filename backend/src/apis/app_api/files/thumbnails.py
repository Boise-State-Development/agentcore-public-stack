"""
Thumbnail rendering for non-image file attachments.

Currently supports PDF (page 1) via pypdfium2. Office formats (.docx, .xlsx)
are intentionally not implemented here — see ThumbnailRenderer.render() for
guidance on how to extend this.
"""

import io
import logging
from typing import Callable, Dict

logger = logging.getLogger(__name__)


# Bounded box for the longest dimension of a generated thumbnail. The UI's
# attachment card body is ~240x128 logical pixels, so 256 covers retina
# without being wasteful on storage / CPU.
THUMBNAIL_MAX_DIMENSION = 256


class ThumbnailUnsupportedError(Exception):
    """Raised when no renderer is registered for a given MIME type."""


class ThumbnailRenderError(Exception):
    """Raised when rendering ran but the source file was unreadable."""


class ThumbnailRenderer:
    """
    MIME-type-dispatching renderer that produces a PNG of a file's first page.

    Today this is PDF-only. The dispatcher exists so that callers (the file
    upload service, the route layer) speak a single API: hand it a MIME type
    plus bytes, get back a PNG or a typed error. New formats plug in by
    adding an entry to ``_renderers``.

    ----- Future formats: .docx and .xlsx -----

    Office formats are deliberately out of scope for this in-process renderer.
    The standard rasterization path requires LibreOffice (``soffice --headless
    --convert-to pdf``) to first convert the document to PDF, which can then be
    handed to the existing PDF path. LibreOffice adds roughly 500 MB to a
    container image, pulls in ~20 system packages, and noticeably increases
    cold start time — costs that are inappropriate for the app-api request
    path, which today serves chat traffic with a tight latency budget.

    Recommendation when those formats are needed:

    1. Build a separate **thumbnail render service**. A small Fargate task or
       a dedicated Lambda using a pre-baked LibreOffice container image is a
       clean fit. Either flavor can stay scaled to zero when idle.
    2. Have app-api enqueue render requests (SQS or a synchronous HTTPS call
       behind an internal ALB) instead of importing the converter. The render
       service writes the resulting PNG to the same `_thumb.png` sibling key
       the PDF path uses, so the cache-and-serve flow on this side is
       unchanged.
    3. Keep the dispatcher's public API stable: callers should still get a PNG
       back, and the cache layout in S3 should not change. The only difference
       is *where* the bytes are produced.

    Until that service exists, callers are expected to filter on
    ``THUMBNAIL_SUPPORTED_MIME_TYPES`` from ``apis.shared.files.models`` and
    return a 415 for unsupported types so the UI can fall back to the
    existing skeleton card.
    """

    def __init__(self) -> None:
        self._renderers: Dict[str, Callable[[bytes], bytes]] = {
            "application/pdf": self._render_pdf,
            # Future entries plug in here. See class docstring for the
            # recommended out-of-process design for .docx and .xlsx.
        }

    def render(self, mime_type: str, raw: bytes) -> bytes:
        """
        Render a thumbnail PNG for the given file bytes.

        Args:
            mime_type: The source file's MIME type.
            raw: The raw file bytes.

        Returns:
            PNG-encoded bytes for a thumbnail bounded by
            THUMBNAIL_MAX_DIMENSION on its longest side.

        Raises:
            ThumbnailUnsupportedError: No renderer is registered for mime_type.
            ThumbnailRenderError: The renderer ran but the file was unreadable.
        """
        renderer = self._renderers.get(mime_type)
        if renderer is None:
            raise ThumbnailUnsupportedError(
                f"No thumbnail renderer registered for {mime_type}"
            )
        return renderer(raw)

    def _render_pdf(self, raw: bytes) -> bytes:
        # Imported lazily so unit tests that don't touch the renderer don't
        # need the native lib loaded.
        try:
            import pypdfium2 as pdfium
        except ImportError as e:
            raise ThumbnailRenderError(
                "pypdfium2 is not installed; PDF thumbnails are unavailable"
            ) from e

        try:
            pdf = pdfium.PdfDocument(io.BytesIO(raw))
        except Exception as e:
            raise ThumbnailRenderError(f"Failed to open PDF: {e}") from e

        try:
            if len(pdf) == 0:
                raise ThumbnailRenderError("PDF has no pages")

            page = pdf[0]
            try:
                width, height = page.get_size()
                longest = max(width, height)
                if longest <= 0:
                    raise ThumbnailRenderError("PDF page has zero dimensions")

                # Scale so the longest side lands at THUMBNAIL_MAX_DIMENSION.
                scale = THUMBNAIL_MAX_DIMENSION / longest
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
            finally:
                page.close()
        finally:
            pdf.close()

        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


_renderer_instance: ThumbnailRenderer | None = None


def get_thumbnail_renderer() -> ThumbnailRenderer:
    """Get or create the singleton ThumbnailRenderer."""
    global _renderer_instance
    if _renderer_instance is None:
        _renderer_instance = ThumbnailRenderer()
    return _renderer_instance
