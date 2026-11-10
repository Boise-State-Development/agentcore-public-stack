"""What an attachment may be, as the server serves it on ``GET /files/limits``.

The server builds the body from its attachment planner (unified-attachment-delivery
§4.4), so a file the TUI refuses here is one presign would refuse anyway, and the
user hears it before the bytes are read. ``None`` on any limit means none applies.
When the fetch fails the TUI checks nothing and the server decides, as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MB = 1024 * 1024

#: Classes by extension, as the server classifies them (``upload_size_limit``):
#: decks, then spreadsheets, then documents ``document_read`` can read.
PRESENTATION_SUFFIXES = frozenset({".pptx"})
SPREADSHEET_SUFFIXES = frozenset({".csv", ".xls", ".xlsx"})
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md", ".html", ".sql"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def _positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


@dataclass(frozen=True, slots=True)
class AttachmentLimits:
    profile: str
    files_per_message: int | None
    document_max_bytes: int | None
    image_max_bytes: int | None
    spreadsheet_max_bytes: int | None
    presentation_max_bytes: int | None
    large_document_max_bytes: int | None

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> AttachmentLimits:
        sizes = body.get("maxBytes")
        sizes = sizes if isinstance(sizes, dict) else {}
        large = _positive(sizes.get("largeDocument")) if body.get("largeDocuments") is not False else None
        return cls(
            profile=str(body.get("profile") or ""),
            files_per_message=_positive(body.get("filesPerMessage")),
            document_max_bytes=_positive(sizes.get("document")),
            image_max_bytes=_positive(sizes.get("image")),
            spreadsheet_max_bytes=_positive(sizes.get("spreadsheet")),
            presentation_max_bytes=_positive(sizes.get("presentation")),
            large_document_max_bytes=large,
        )

    def size_limit_for(self, suffix: str) -> int | None:
        """The byte ceiling for a file with this extension, or None to let the server judge."""
        suffix = suffix.lower()
        if suffix in PRESENTATION_SUFFIXES:
            return self.presentation_max_bytes
        if suffix in SPREADSHEET_SUFFIXES:
            return self.spreadsheet_max_bytes
        if suffix in DOCUMENT_SUFFIXES:
            return self.large_document_max_bytes or self.document_max_bytes
        if suffix in IMAGE_SUFFIXES:
            return self.image_max_bytes
        return self.document_max_bytes


def format_limit(size_bytes: int) -> str:
    """``4 MB`` / ``25 MB``, the server's own wording for a whole-MB limit."""
    return f"{size_bytes // MB} MB" if size_bytes % MB == 0 else f"{size_bytes / MB:.1f} MB"
