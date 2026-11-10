"""File facts: the immutable fact set preparation writes on a file row.

``docs/specs/unified-attachment-delivery.md`` §4.1. Every document-class upload
is prepared (§7, behind ``LARGE_DOCUMENT_ATTACHMENTS_ENABLED``), and the
preparation writes, beside ``preparation`` and ``digest``, one ``facts`` map::

    {
        "version": 1,
        "class": "document",
        "tokens": {"text": 1234, "native": 4500},
        "pages": 3,              # PDF only; None otherwise
        "hasTextLayer": True,    # False for a scanned PDF
        "units": "page",         # page | paragraph | line
    }

Numbers and enums only, never content. Written once per set of bytes: a
replaced file drops its facts with its digest and is prepared again.

**Who reads it.** Compaction and offload estimate a document from these facts
(``apis.shared.files.document_estimate``, phase 2b). Version 1's
``tokens.native`` is the page images only, so that reader adds ``tokens.text``
for a native PDF (§4.1). A row without facts (written before this, the flag
off, or a preparation still queued) falls back to the bytes, and a turn never
waits for them.

How each fact is derived:

* ``tokens.text`` is the extracted text the model would be sent as text,
  counted at ``CHARS_PER_TOKEN`` (the repository's one heuristic, shared with
  compaction): a PDF's page texts, a .docx rendered as Markdown (what
  ``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED`` sends), HTML rendered as Markdown, and
  the text formats as decoded.
* ``tokens.native`` is :func:`native_document_tokens`, the same formula
  ``estimate_document_tokens`` applies to bytes in hand: a PDF is
  ``max(pages * PDF_PAGE_TOKEN_ESTIMATE, bytes / 4)``, everything else
  ``bytes / 4``.
* ``hasTextLayer`` is false for a PDF where fewer than half the pages carry
  ``MIN_TEXT_CHARS_PER_PAGE`` non-blank characters (a scan, or a scan with a
  typed cover page). A text format has a text layer when it has any text.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from apis.shared.files.document_tokens import CHARS_PER_TOKEN, PDF_PAGE_TOKEN_ESTIMATE

logger = logging.getLogger(__name__)

#: Bumped when a fact's definition changes, so a reader can tell facts it
#: understands from older ones.
FACTS_VERSION = 1

CLASS_DOCUMENT = "document"

#: Unit by document format, the same one ``document_read`` numbers by.
_UNITS = {"pdf": "page", "docx": "paragraph", "txt": "line", "md": "line", "html": "line"}

#: A PDF page with fewer non-blank characters than this has no usable text
#: (a scanned page, or one carrying only a page number).
MIN_TEXT_CHARS_PER_PAGE = 16


def native_document_tokens(fmt: Optional[str], size_bytes: int, page_count: Optional[int]) -> int:
    """Estimated tokens a document costs as a native block.

    The formula ``document_tokens.estimate_document_tokens`` applies to bytes
    in hand, from the row's numbers: ``max(pages * PDF_PAGE_TOKEN_ESTIMATE,
    bytes / 4)`` for a PDF whose pages are known, ``bytes / 4`` otherwise.
    """
    byte_tokens = max(0, int(size_bytes or 0)) // CHARS_PER_TOKEN
    if str(fmt or "").lower() != "pdf" or not page_count:
        return byte_tokens
    return max(int(page_count) * PDF_PAGE_TOKEN_ESTIMATE, byte_tokens)


def text_tokens(text: str) -> int:
    return len(text or "") // CHARS_PER_TOKEN


def _pdf_has_text_layer(page_texts: Sequence[str]) -> bool:
    if not page_texts:
        return False
    with_text = sum(1 for t in page_texts if len("".join((t or "").split())) >= MIN_TEXT_CHARS_PER_PAGE)
    return with_text * 2 >= len(page_texts)


def render_text(fmt: str, raw: bytes) -> str:
    """The non-PDF document as the text a model is sent. Raises on a corrupt file.

    Mirrors ``apis.shared.models.document_cap``'s renderer, which the
    preparation Lambda cannot import (its image carries ``apis/shared/files``
    only): a .docx and HTML as Markdown, the text formats decoded.
    """
    if fmt == "docx":
        from apis.shared.files.docx_markdown import docx_to_markdown

        try:
            return docx_to_markdown(raw)
        except Exception:  # noqa: BLE001 - the plain extractor, as document_cap falls back
            from apis.shared.files.document_read import docx_paragraphs

            return "\n\n".join(docx_paragraphs(raw))
    text = raw.decode("utf-8", errors="replace")
    if fmt == "html":
        from apis.shared.files.html_markdown import html_to_markdown

        return html_to_markdown(text)
    return text


def build_document_facts(
    fmt: str,
    *,
    size_bytes: int,
    text: Optional[str] = None,
    page_texts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The ``facts`` map for one document.

    A PDF passes ``page_texts`` (one per page); every other format passes
    ``text`` (from :func:`render_text`).
    """
    if fmt == "pdf":
        pages_text = list(page_texts or [])
        pages: Optional[int] = len(pages_text)
        body = "\n\n".join(pages_text)
        has_text = _pdf_has_text_layer(pages_text)
    else:
        pages = None
        body = text or ""
        has_text = bool(body.strip())
    return {
        "version": FACTS_VERSION,
        "class": CLASS_DOCUMENT,
        "tokens": {"text": text_tokens(body), "native": native_document_tokens(fmt, size_bytes, pages)},
        "pages": pages,
        "hasTextLayer": has_text,
        "units": _UNITS.get(fmt, "line"),
    }


def compute_document_facts(fmt: str, raw: bytes) -> Dict[str, Any]:
    """Facts for a document from its bytes. Raises on a document that will not parse."""
    if fmt == "pdf":
        from apis.shared.files.document_read import _open_pdf, _pdf_page_text
        from apis.shared.files.pdfium_lock import serialized_pdfium

        @serialized_pdfium
        def _page_texts() -> List[str]:
            pdf = _open_pdf(raw)
            try:
                return [_pdf_page_text(pdf, i) for i in range(len(pdf))]
            finally:
                pdf.close()

        return build_document_facts(fmt, size_bytes=len(raw), page_texts=_page_texts())
    return build_document_facts(fmt, size_bytes=len(raw), text=render_text(fmt, raw))


def facts_current(meta: Any) -> bool:
    """Whether the row already carries facts of this version."""
    facts = getattr(meta, "facts", None)
    if not isinstance(facts, dict):
        return False
    try:
        return int(facts.get("version", 0)) == FACTS_VERSION
    except (TypeError, ValueError):
        return False


def record_facts(*, outcome: str, fmt: Optional[str], ms: int) -> None:
    """One content-free ``FileFactsComputed`` record per facts-only preparation. Never raises."""
    logger.info("file_facts_computed: outcome=%s format=%s ms=%d", outcome, fmt, ms)
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={"FileFactsComputed": 1, "FileFactsMs": int(ms)},
            properties={"format": fmt, "outcome": outcome},
            units={"FileFactsMs": "Milliseconds"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("file_facts_computed EMF skipped: %s", e)


__all__ = [
    "CLASS_DOCUMENT",
    "FACTS_VERSION",
    "MIN_TEXT_CHARS_PER_PAGE",
    "build_document_facts",
    "compute_document_facts",
    "facts_current",
    "native_document_tokens",
    "record_facts",
    "render_text",
    "text_tokens",
]
