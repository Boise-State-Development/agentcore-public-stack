"""One conversation file, whole, for a side call that sends it to a model in one request.

``docs/specs/workflow-designer.md`` §21.3 (a workflow's prompt steps). ``document_read`` reads a
*part* of a document for a tool round; this reads the *whole* document once, read through a
reference to its original (``readable_view``), and hands back what a request carries:

- **A PDF** stays native: its bytes, for a ``document`` block, and its page count.
- **A prepared large document** (``_derived/text.jsonl``) is its text layer, never its bytes.
- **Any other document** is its text, rendered the way documents-as-text on Converse renders it
  (``render_document_as_text``: UTF-8, ``html_markdown`` or ``docx_markdown``), in the same
  ``<document>`` envelope.
- **A file with no document form** (a spreadsheet, a deck, an image) is a one-line note that names
  it, so the model knows a file was shared that it is not shown, rather than reading a set that
  silently lost one.

The output is a pure function of the stored bytes and the row, so a run's steps that send the same
files send byte-identical blocks, which is what lets one call warm a prompt-cache prefix the rest
read. Nothing is truncated here: a caller that has a budget refuses a document over it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Optional

from apis.shared.files import large_document
from apis.shared.files.attachment_plan import FileFacts, facts_from_row
from apis.shared.files.document_tokens import pdf_page_count
from apis.shared.files.models import FileMetadata, FileStatus

logger = logging.getLogger(__name__)


class WholeDocumentError(Exception):
    """The file can't be read whole. ``reason`` is ``not_found``, ``gone`` or ``too_large``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class WholeDocument:
    """One file as a request carries it: native PDF bytes, or text."""

    upload_id: str
    filename: str
    #: The Bedrock document format, or ``None`` for a file with no document form.
    fmt: Optional[str]
    #: A PDF's bytes, for a native ``document`` block. ``None`` for text.
    pdf_bytes: Optional[bytes] = None
    #: The text the request carries, in its ``<document>`` envelope. ``None`` for a native PDF.
    text: Optional[str] = None
    #: A PDF's pages, when known.
    pages: Optional[int] = None
    #: The row's planner facts (size, pages, ``tokens.text`` when current), for a token estimate.
    facts: Optional[FileFacts] = None

    @property
    def native(self) -> bool:
        return self.pdf_bytes is not None


def _envelope(name: str, fmt: str, text: str) -> str:
    """The envelope ``document_cap`` gives a document sent as text."""
    body = text.replace("</document", "<\\/document")
    quoted = name.replace('"', "'")
    return f'<document name="{quoted}" format="{fmt}">\n{body}\n</document>'


def _no_document_form(meta: FileMetadata) -> str:
    name = (meta.filename or "file").replace('"', "'")
    return f'[File "{name}" is shared with this step, but it has no document form that can be shown here.]'


async def read_whole_document(
    user_id: str,
    session_id: str,
    upload_id: str,
    *,
    reader: str = "whole_document",
    max_source_bytes: Optional[int] = None,
) -> WholeDocument:
    """``upload_id`` in ``session_id``, whole. Raises ``WholeDocumentError``; never truncates.

    The file must be the caller's, ready, and in this conversation. A reference is read through to
    its original, and one whose original is gone raises ``gone``. An unprepared source over
    ``max_source_bytes`` (default ``document_read``'s source cap) raises ``too_large``.
    """
    from apis.shared.files.document_read import (
        _MAX_DOCUMENT_BYTES,
        _get_object_bytes,
        document_format_for,
    )
    from apis.shared.files.linked_files import linked_file_gone_message, readable_view
    from apis.shared.files.repository import get_file_upload_repository
    from apis.shared.models.document_cap import RENDERABLE_FORMATS, render_document_as_text

    meta =await get_file_upload_repository().get_file(user_id, upload_id)
    status = (meta.status if isinstance(meta.status, str) else meta.status.value) if meta is not None else None
    if meta is None or meta.session_id != session_id or status != FileStatus.READY.value:
        raise WholeDocumentError("not_found", f"No file with id '{upload_id}' in this conversation")
    view = await readable_view(meta, reader=reader)
    if view is None:
        raise WholeDocumentError("gone", linked_file_gone_message(meta))

    facts = facts_from_row(view)
    fmt = document_format_for(view.mime_type, view.filename)
    if fmt is None or (fmt != "pdf" and fmt not in RENDERABLE_FORMATS and not large_document.is_prepared(view)):
        return WholeDocument(upload_id=upload_id, filename=view.filename, fmt=None, text=_no_document_form(view), facts=facts)

    if large_document.is_prepared(view):
        key = large_document.text_layer_key(view.s3_key)
        body = await asyncio.to_thread(_get_object_bytes, view.s3_bucket, key)
        units = await asyncio.to_thread(large_document.decode_text_layer, body)
        return WholeDocument(
            upload_id=upload_id,
            filename=view.filename,
            fmt=fmt,
            text=_envelope(view.filename or "document", fmt, "\n\n".join(units)),
            pages=large_document.prepared_page_count(view) if fmt == "pdf" else None,
            facts=facts,
        )

    limit = max_source_bytes if max_source_bytes and max_source_bytes > 0 else _MAX_DOCUMENT_BYTES
    if int(view.size_bytes or 0) > limit:
        raise WholeDocumentError("too_large", f"'{view.filename}' is too large to send whole ({view.size_bytes} bytes)")
    raw = await asyncio.to_thread(_get_object_bytes, view.s3_bucket, view.s3_key)
    if len(raw) > limit:
        raise WholeDocumentError("too_large", f"'{view.filename}' is too large to send whole ({len(raw)} bytes)")

    if fmt == "pdf":
        pages = await asyncio.to_thread(pdf_page_count, raw)
        if facts.page_count is None and pages:
            facts = replace(facts, page_count=pages)
        return WholeDocument(
            upload_id=upload_id, filename=view.filename, fmt=fmt, pdf_bytes=raw, pages=pages or facts.page_count, facts=facts
        )

    text = await asyncio.to_thread(
        render_document_as_text, {"format": fmt, "name": view.filename or "document", "source": {"bytes": raw}}
    )
    return WholeDocument(upload_id=upload_id, filename=view.filename, fmt=fmt, text=text, facts=facts)


__all__ = ["WholeDocument", "WholeDocumentError", "read_whole_document"]
