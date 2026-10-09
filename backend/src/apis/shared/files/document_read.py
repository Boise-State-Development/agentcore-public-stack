"""Document read service — page-range and pattern retrieval over uploaded documents.

Backs the ``document_read`` agent tool (``agents/builtin_tools/document_read_tool.py``;
design in ``docs/specs/document-context-offload.md`` §4B). The tool is the
recovery path for a document that is no longer inline in the model's context:
history restore strips inline document bytes (Bedrock rejects duplicate
document names across a conversation), and the offload trigger will later
replace a document with a digest on purpose. Either way the model needs a way
to pull *part* of a document back at native fidelity.

Three retrieval modes, modelled on Strands' ``retrieve_offloaded_content``:

* **pages** — a PDF page range, re-assembled server-side into a new PDF that
  contains only those pages and returned as a native ``document`` block. The
  model sees the pages as it would the original (text layer plus page
  images), not as flattened text. Hard-capped by ``max_pages`` so one call can
  never re-inject a whole document.
* **pattern** — a case-insensitive regex over the document's text layer.
  Returns the matching lines with their page numbers (PDF) or line /
  paragraph numbers (text, DOCX), so the model can then ask for the pages
  that matter. Bounded match count.
* **text** — bounded text for the text-family formats (txt / md / html via the
  workspace read path, DOCX via a stdlib extractor), with ``offset``
  continuation.

Plus a **list** mode (no ``upload_id``) enumerating the session's readable
documents, and an **index** mode (a PDF with neither range nor pattern) that
returns the page count and a short per-page snippet index.

Every read goes through the DynamoDB user-files table (ownership by key shape,
``PK = USER#{userId}``) and reuses the workspace service's S3 client. No
operation accepts a model-supplied S3 key. Nothing here emits metrics or
touches the agent layer — the tool does that.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from .models import FileMetadata, FileStatus, get_file_format, is_presentation_file, is_tabular_file
from .repository import get_file_upload_repository
from .workspace import (
    WORKSPACE_READ_MAX_BYTES,
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceValidationError,
    _get_owned_ready_file,
    _require_identity,
    _s3,
    is_text_mime,
    read_workspace_file,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds (env-backed; every one caps a per-turn payload)
# ---------------------------------------------------------------------------

#: Default page-range size when the model does not say. 8 pages of a dense PDF
#: is ~10–20k tokens — a bounded, deliberate spend, not a whole document.
DOCUMENT_READ_MAX_PAGES = int(os.environ.get("DOCUMENT_READ_MAX_PAGES", 8))
#: Absolute ceiling on one call's page count, whatever ``max_pages`` the model
#: asks for. The tool's contract is "one call cannot re-inject the document".
DOCUMENT_READ_HARD_MAX_PAGES = int(os.environ.get("DOCUMENT_READ_HARD_MAX_PAGES", 20))
#: Pattern mode returns at most this many matching lines.
DOCUMENT_READ_MAX_MATCHES = int(os.environ.get("DOCUMENT_READ_MAX_MATCHES", 40))
#: Index mode lists a snippet for at most this many pages.
DOCUMENT_READ_INDEX_PAGES = int(os.environ.get("DOCUMENT_READ_INDEX_PAGES", 40))
#: Wall-clock bound on one pattern scan. The backstop behind
#: ``catastrophic_pattern``: a single ``re.search`` cannot be cancelled, but the
#: walk across lines and pages can be stopped, so a merely-slow pattern over a
#: long document degrades to a partial answer instead of hanging the turn to the
#: 600 s SSE timeout. ``0`` disables the bound.
DOCUMENT_READ_PATTERN_BUDGET_SECONDS = float(
    os.environ.get("DOCUMENT_READ_PATTERN_BUDGET_SECONDS", 2.0)
)
#: Characters kept per matching / snippet line.
_LINE_CHARS = 200
_SNIPPET_CHARS = 90
#: Lines of context on either side of a pattern match.
_CONTEXT_LINES = 1
#: Documents larger than this are not searched or sliced (the upload cap is
#: 4 MB, so this only guards a misconfigured bucket).
_MAX_DOCUMENT_BYTES = int(os.environ.get("DOCUMENT_READ_MAX_SOURCE_BYTES", 8 * 1024 * 1024))

#: Bedrock document formats the tool can read. Tabular and presentation files
#: have their own tools; images have no page/text structure to retrieve.
DOCUMENT_CLASS_FORMATS = frozenset({"pdf", "docx", "txt", "html", "md"})
_TEXT_FORMATS = frozenset({"txt", "html", "md"})

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentReadError(WorkspaceError):
    """A document_read failure surfaced conversationally by the tool."""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def document_format_for(mime_type: str, filename: str = "") -> Optional[str]:
    """The Bedrock document format the tool can read this file as, or ``None``.

    ``None`` for tabular files (spreadsheet tools), presentations (PowerPoint
    tools), images, and anything the upload allowlist does not know.
    """
    if is_tabular_file(filename or "", mime_type or "") or is_presentation_file(filename or "", mime_type or ""):
        return None
    fmt = get_file_format((mime_type or "").lower().split(";")[0].strip())
    if fmt is None and is_text_mime(mime_type or ""):
        fmt = "txt"
    return fmt if fmt in DOCUMENT_CLASS_FORMATS else None


def is_document_class(mime_type: str, filename: str = "") -> bool:
    """True when ``document_read`` has something to retrieve from this file."""
    return document_format_for(mime_type, filename) is not None


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class DocumentReadResult:
    """What one read produced. ``payload`` is JSON for the tool result; the
    optional ``document_block`` is a native Bedrock document block appended
    after it. The counters are what the tool records (content-free)."""

    mode: str
    payload: Dict[str, Any]
    document_block: Optional[Dict[str, Any]] = None
    pages_returned: int = 0
    bytes_returned: int = 0
    format: Optional[str] = None
    extra_blocks: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def list_session_documents(user_id: str, session_id: str) -> Dict[str, Any]:
    """The session's READY documents the tool can read, newest first."""
    _require_identity(user_id, session_id)
    files = await get_file_upload_repository().list_session_files(session_id, status=FileStatus.READY)
    documents: List[Dict[str, Any]] = []
    for meta in files:
        if meta.user_id != user_id:
            continue
        fmt = document_format_for(meta.mime_type, meta.filename)
        if fmt is None:
            continue
        documents.append(
            {
                "upload_id": meta.upload_id,
                "filename": meta.filename,
                "format": fmt,
                "size_bytes": meta.size_bytes,
                "modes": _modes_for(fmt),
            }
        )
    return {"documents": documents, "count": len(documents)}


def _modes_for(fmt: str) -> List[str]:
    if fmt == "pdf":
        return ["page_range", "pattern"]
    return ["pattern", "text"]


async def session_has_documents(user_id: str, session_id: str) -> bool:
    """Whether the session has at least one READY document the tool can read.

    The tool-injection gate. One ``SessionIndex`` query; callers memoize the
    positive answer because it is monotonic for practical purposes (a file,
    once uploaded, stays unless the user deletes it).
    """
    _require_identity(user_id, session_id)
    files = await get_file_upload_repository().list_session_files(session_id, status=FileStatus.READY)
    return any(meta.user_id == user_id and is_document_class(meta.mime_type, meta.filename) for meta in files)


async def session_has_tabular_files(user_id: str, session_id: str) -> bool:
    """Whether the session has at least one READY spreadsheet (CSV/XLSX).

    The Spreadsheet Analysis auto-enable gate — the tabular counterpart of
    :func:`session_has_documents`, same one-query cost and the same
    "memoize the positive answer" contract for callers. Spreadsheets never go
    inline, so a session holding one is a session whose turns need the
    analysis tools to read it at all.
    """
    _require_identity(user_id, session_id)
    files = await get_file_upload_repository().list_session_files(session_id, status=FileStatus.READY)
    return any(meta.user_id == user_id and is_tabular_file(meta.filename, meta.mime_type) for meta in files)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def parse_page_range(value: Any) -> Optional[Tuple[int, int]]:
    """``"4-7"`` / ``"4"`` / ``"4..7"`` / ``"4:7"`` / ``{"start": 4, "end": 7}``
    → ``(4, 7)`` (1-indexed, inclusive). ``None`` for empty input. Raises
    ``WorkspaceValidationError`` on anything else."""
    if value is None:
        return None
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end", value.get("start"))
    elif isinstance(value, (list, tuple)) and len(value) in (1, 2):
        start, end = value[0], value[-1]
    elif isinstance(value, int):
        start = end = value
    else:
        text = str(value).strip()
        if not text:
            return None
        match = re.fullmatch(r"\s*(\d+)\s*(?:(?:-|\.\.|:|–|to)\s*(\d+))?\s*", text)
        if not match:
            raise WorkspaceValidationError(
                f"Unrecognized page_range '{value}'. Use 'start-end' with 1-indexed page numbers, e.g. '4-7'."
            )
        start, end = match.group(1), match.group(2) or match.group(1)
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        raise WorkspaceValidationError(f"Unrecognized page_range '{value}'.") from None
    if start_i < 1 or end_i < start_i:
        raise WorkspaceValidationError(
            f"Invalid page_range '{value}': pages are 1-indexed and end must not precede start."
        )
    return start_i, end_i


def clamp_max_pages(max_pages: Any) -> int:
    try:
        requested = int(max_pages) if max_pages is not None else DOCUMENT_READ_MAX_PAGES
    except (TypeError, ValueError):
        requested = DOCUMENT_READ_MAX_PAGES
    return max(1, min(requested, DOCUMENT_READ_HARD_MAX_PAGES))


async def read_document(
    user_id: str,
    session_id: str,
    upload_id: str,
    *,
    page_range: Any = None,
    pattern: Optional[str] = None,
    max_pages: Any = None,
    offset: int = 0,
    text_only: bool = False,
) -> DocumentReadResult:
    """Read part of one document. See the module docstring for the modes.

    ``text_only`` is for a model whose catalog row declares TEXT input only:
    a PDF page range comes back as those pages' text layer instead of a
    native ``document`` block, which such a model rejects outright.
    """
    _require_identity(user_id, session_id)
    if not upload_id:
        raise WorkspaceValidationError("upload_id is required (call with no arguments to list documents)")
    if offset < 0:
        raise WorkspaceValidationError("offset must be >= 0")

    meta = await _get_owned_ready_file(user_id, upload_id)
    fmt = document_format_for(meta.mime_type, meta.filename)
    if fmt is None:
        raise DocumentReadError(
            f"'{meta.filename}' is not a readable document. Spreadsheets go through "
            "analyze_spreadsheet, presentations through read_powerpoint_presentation, "
            "and images are only available inline."
        )
    if meta.size_bytes > _MAX_DOCUMENT_BYTES:
        raise DocumentReadError(f"'{meta.filename}' is too large to read ({meta.size_bytes} bytes).")

    pages = parse_page_range(page_range)
    pattern_text = (pattern or "").strip() or None
    limit = clamp_max_pages(max_pages)

    base = {
        "upload_id": meta.upload_id,
        "filename": meta.filename,
        "format": fmt,
        "size_bytes": meta.size_bytes,
    }

    if fmt == "pdf":
        raw = await _fetch_bytes(meta)
        if pattern_text:
            return await asyncio.to_thread(_pdf_pattern, raw, pattern_text, base)
        if pages and text_only:
            return await asyncio.to_thread(_pdf_pages_text, raw, pages, limit, base)
        if pages:
            return await asyncio.to_thread(_pdf_pages, raw, pages, limit, base, meta.filename)
        return await asyncio.to_thread(_pdf_index, raw, base)

    if pages:
        raise WorkspaceValidationError(
            f"page_range applies to PDFs only; '{meta.filename}' is {fmt}. Use pattern or offset instead."
        )

    if fmt == "docx":
        raw = await _fetch_bytes(meta)
        return await asyncio.to_thread(_docx_read, raw, pattern_text, offset, base)

    # txt / md / html
    if pattern_text:
        raw = await _fetch_bytes(meta)
        return await asyncio.to_thread(_text_pattern, raw.decode("utf-8", errors="replace"), pattern_text, base, "line")
    result = await read_workspace_file(user_id, upload_id, offset=offset)
    payload = {
        **base,
        "mode": "text",
        "content": result.get("content", ""),
        "offset": result.get("offset", offset),
        "truncated": bool(result.get("truncated")),
        "next_offset": result.get("next_offset"),
    }
    return DocumentReadResult(
        mode="text", payload=payload, bytes_returned=len(payload["content"].encode("utf-8")), format=fmt
    )


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _get_object_bytes(bucket: str, key: str) -> bytes:
    return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()


async def _fetch_bytes(meta: FileMetadata) -> bytes:
    raw = await asyncio.to_thread(_get_object_bytes, meta.s3_bucket, meta.s3_key)
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise DocumentReadError(f"'{meta.filename}' is too large to read ({len(raw)} bytes).")
    return raw


# ---------------------------------------------------------------------------
# PDF (pypdfium2 — already a dependency for attachment thumbnails)
# ---------------------------------------------------------------------------


def _open_pdf(raw: bytes):
    import pypdfium2 as pdfium  # lazy: native lib, not needed on non-PDF paths

    try:
        return pdfium.PdfDocument(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise DocumentReadError(f"Could not open the PDF: {e}") from e


def _pdf_page_text(pdf, index: int) -> str:
    page = pdf[index]
    try:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded() or ""
        finally:
            textpage.close()
    finally:
        page.close()


def _pdf_pages(raw: bytes, pages: Tuple[int, int], limit: int, base: Dict[str, Any], filename: str) -> DocumentReadResult:
    import pypdfium2 as pdfium

    start, end = pages
    pdf = _open_pdf(raw)
    try:
        count = len(pdf)
        if start > count:
            raise WorkspaceValidationError(f"page_range starts at {start} but the document has {count} pages.")
        end = min(end, count)
        truncated = False
        if end - start + 1 > limit:
            end = start + limit - 1
            truncated = True
        sub = pdfium.PdfDocument.new()
        try:
            sub.import_pages(pdf, pages=list(range(start - 1, end)))
            buffer = io.BytesIO()
            sub.save(buffer)
        finally:
            sub.close()
    finally:
        pdf.close()

    sliced = buffer.getvalue()
    returned = end - start + 1
    name = _unique_document_name(filename, f"p{start}-{end}")
    payload = {
        **base,
        "mode": "pages",
        "page_count": count,
        "pages": {"start": start, "end": end},
        "pages_returned": returned,
        "truncated_to_max_pages": truncated,
        "next_start": end + 1 if (truncated and end < count) else None,
        "page_numbering": (
            f"The attached document contains original pages {start}-{end} only; "
            f"its page k is original page {start} + k - 1."
        ),
    }
    block = {"document": {"format": "pdf", "name": name, "source": {"bytes": sliced}}}
    return DocumentReadResult(
        mode="pages", payload=payload, document_block=block,
        pages_returned=returned, bytes_returned=len(sliced), format="pdf",
    )


def _pdf_pages_text(raw: bytes, pages: Tuple[int, int], limit: int, base: Dict[str, Any]) -> DocumentReadResult:
    """``_pdf_pages`` for a text-only model: the same range and the same
    ``max_pages`` cap, returned as each page's text layer in the payload."""
    start, end = pages
    pdf = _open_pdf(raw)
    try:
        count = len(pdf)
        if start > count:
            raise WorkspaceValidationError(f"page_range starts at {start} but the document has {count} pages.")
        end = min(end, count)
        truncated = False
        if end - start + 1 > limit:
            end = start + limit - 1
            truncated = True
        texts = [{"page": i + 1, "text": _pdf_page_text(pdf, i)} for i in range(start - 1, end)]
    finally:
        pdf.close()

    returned = end - start + 1
    payload = {
        **base,
        "mode": "pages_text",
        "page_count": count,
        "pages": {"start": start, "end": end},
        "pages_returned": returned,
        "truncated_to_max_pages": truncated,
        "next_start": end + 1 if (truncated and end < count) else None,
        "note": (
            "The current model reads text only, so these are the pages' text layers; "
            "page images, charts and scanned pages are not included."
        ),
        "page_texts": texts,
    }
    return DocumentReadResult(
        mode="pages_text", payload=payload, pages_returned=returned,
        bytes_returned=sum(len(t["text"].encode("utf-8")) for t in texts), format="pdf",
    )


def _pdf_pattern(raw: bytes, pattern: str, base: Dict[str, Any]) -> DocumentReadResult:
    regex, note = _compile(pattern)
    pdf = _open_pdf(raw)
    # The clock starts after the open: lazily importing the pypdfium2 native
    # library and parsing the document are setup, not scanning, and charging
    # them to the scan's budget can report a timeout on pages never examined.
    clock = _Budget(DOCUMENT_READ_PATTERN_BUDGET_SECONDS)
    pages_searched = 0
    try:
        count = len(pdf)
        matches: List[Dict[str, Any]] = []
        pages_matched: List[int] = []
        for index in range(count):
            if len(matches) >= DOCUMENT_READ_MAX_MATCHES or clock.out_of_time():
                break
            pages_searched = index + 1
            lines = _pdf_page_text(pdf, index).splitlines()
            page_no = index + 1
            hit = False
            for entry in _grep_lines(lines, regex, DOCUMENT_READ_MAX_MATCHES - len(matches), clock):
                entry["page"] = page_no
                matches.append(entry)
                hit = True
            if hit:
                pages_matched.append(page_no)
    finally:
        pdf.close()
    payload = {
        **base,
        "mode": "pattern",
        "pattern": pattern,
        "page_count": count,
        "pages_matched": pages_matched,
        "match_count": len(matches),
        "truncated": len(matches) >= DOCUMENT_READ_MAX_MATCHES,
        "matches": matches,
        "hint": "Call again with page_range to read the matching pages at full fidelity.",
    }
    if note:
        payload["pattern_note"] = note
    if clock.expired:
        payload["timed_out"] = True
        payload["pages_searched"] = pages_searched
        payload["hint"] = (
            f"The search ran out of time after {pages_searched} of {count} pages; these are "
            "the matches found so far. Use a simpler pattern, or read a page_range directly."
        )
    return DocumentReadResult(mode="pattern", payload=payload, pages_returned=0, format="pdf")


def _pdf_index(raw: bytes, base: Dict[str, Any]) -> DocumentReadResult:
    pdf = _open_pdf(raw)
    try:
        count = len(pdf)
        index: List[Dict[str, Any]] = []
        for i in range(min(count, DOCUMENT_READ_INDEX_PAGES)):
            snippet = ""
            for line in _pdf_page_text(pdf, i).splitlines():
                line = line.strip()
                if line:
                    snippet = line[:_SNIPPET_CHARS]
                    break
            index.append({"page": i + 1, "snippet": snippet})
    finally:
        pdf.close()
    payload = {
        **base,
        "mode": "index",
        "page_count": count,
        "page_index": index,
        "index_truncated": count > DOCUMENT_READ_INDEX_PAGES,
        "hint": (
            f"Read specific pages with page_range (max {DOCUMENT_READ_MAX_PAGES} per call, "
            f"hard cap {DOCUMENT_READ_HARD_MAX_PAGES}) or locate them first with pattern."
        ),
    }
    return DocumentReadResult(mode="index", payload=payload, format="pdf")


# ---------------------------------------------------------------------------
# DOCX (stdlib: zipfile + ElementTree over word/document.xml)
# ---------------------------------------------------------------------------


def docx_paragraphs(raw: bytes) -> List[str]:
    """Paragraph text of a .docx, in document order. Tables contribute their
    cell paragraphs; runs are joined; tabs and line breaks become whitespace.
    Good enough for pattern search and bounded reading, not a renderer."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise DocumentReadError(f"Could not open the Word document: {e}") from e
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as e:
        raise DocumentReadError(f"Could not parse the Word document: {e}") from e
    paragraphs: List[str] = []
    for para in root.iter(f"{_W_NS}p"):
        parts: List[str] = []
        for node in para.iter():
            if node.tag == f"{_W_NS}t":
                parts.append(node.text or "")
            elif node.tag in (f"{_W_NS}tab",):
                parts.append("\t")
            elif node.tag in (f"{_W_NS}br", f"{_W_NS}cr"):
                parts.append(" ")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _docx_read(raw: bytes, pattern: Optional[str], offset: int, base: Dict[str, Any]) -> DocumentReadResult:
    paragraphs = docx_paragraphs(raw)
    if pattern:
        result = _text_pattern("\n".join(paragraphs), pattern, base, "paragraph")
        result.format = "docx"
        return result
    text = "\n\n".join(paragraphs)
    encoded = text.encode("utf-8")
    if offset and offset >= len(encoded):
        raise WorkspaceValidationError(f"offset {offset} is beyond the end of the document text ({len(encoded)} bytes)")
    chunk = encoded[offset: offset + WORKSPACE_READ_MAX_BYTES]
    end = offset + len(chunk)
    truncated = end < len(encoded)
    payload = {
        **base,
        "mode": "text",
        "paragraph_count": len(paragraphs),
        "content": chunk.decode("utf-8", errors="replace"),
        "offset": offset,
        "truncated": truncated,
        "next_offset": end if truncated else None,
    }
    return DocumentReadResult(mode="text", payload=payload, bytes_returned=len(chunk), format="docx")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _scan_flags(text: str):
    """Yield ``(index, char)`` for the regex-significant characters of ``text``
    — skipping escaped characters and the contents of ``[...]`` classes, where
    ``*``, ``+``, ``|`` and parentheses are literals."""
    i, in_class, n = 0, False, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            continue
        yield i, ch
        i += 1


def _unbounded_quantifier_at(pattern: str, i: int) -> bool:
    """Is there an unbounded quantifier (``*``, ``+`` or ``{n,}``) at ``i``?"""
    if i >= len(pattern):
        return False
    if pattern[i] in "*+":
        return True
    if pattern[i] == "{":
        close = pattern.find("}", i)
        return close != -1 and pattern[i + 1: close].endswith(",")
    return False


def _has_unbounded_quantifier(body: str) -> bool:
    return any(
        _unbounded_quantifier_at(body, i) for i, ch in _scan_flags(body) if ch in "*+{"
    )


def catastrophic_pattern(pattern: str) -> bool:
    """Whether ``pattern`` has the nested-quantifier shape that makes Python's
    backtracking engine run in exponential time.

    ``(a+)+``, ``(\\w+\\s*)*``, ``(a+|b){2,}`` — an unbounded quantifier applied to
    a group that itself contains one. Measured on the real engine: ``(a+)+$``
    against ``"a"*24 + "!"`` takes 0.87 s, 26 takes 3.5 s, 28 takes 14 s.
    Extracted PDF lines run 60–100 characters, so such a pattern never returns.

    Deliberately narrow. It flags only the unambiguous family, so ordinary
    patterns — ``\\d+``, ``(invoice|receipt)``, ``(foo)+`` — are never refused.
    Shapes it does not catch (overlapping literal alternations like ``(a|a)+``,
    backreference blowups) are bounded by the wall-clock budget instead, not by
    this test.
    """
    if not pattern:
        return False
    starts: List[int] = []
    for i, ch in _scan_flags(pattern):
        if ch == "(":
            starts.append(i)
        elif ch == ")" and starts:
            body = pattern[starts.pop() + 1: i]
            if _unbounded_quantifier_at(pattern, i + 1) and _has_unbounded_quantifier(body):
                return True
    return False


def _compile(pattern: str) -> Tuple["re.Pattern[str]", Optional[str]]:
    """``(regex, note)``. The note is non-``None`` when the pattern was searched
    literally instead of as a regex, and is carried to the model on the payload
    so a silently different result is never presented as the requested one.

    Two reasons to fall back, both degrading rather than erroring: the pattern
    does not compile, or it has the nested-quantifier shape that would hang the
    turn (``catastrophic_pattern``)."""
    if catastrophic_pattern(pattern):
        logger.warning("document_read: refusing catastrophic pattern, searching literally")
        return re.compile(re.escape(pattern), re.IGNORECASE), (
            "This pattern nests one unbounded quantifier inside another, which can take "
            "exponential time to match, so it was searched as literal text instead. "
            "Rewrite it without the nesting (for example '\\w+' rather than '(\\w+)+')."
        )
    try:
        return re.compile(pattern, re.IGNORECASE), None
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE), (
            "This pattern is not a valid regular expression, so it was searched as literal text."
        )


class _Budget:
    """Wall-clock bound on one pattern scan.

    The backstop behind ``catastrophic_pattern``: it cannot interrupt a single
    ``re.search`` — CPython exposes no way to cancel one, and the scan runs in a
    worker thread where signals are unavailable — but it stops the *walk*, so a
    merely-slow pattern over a 200-page document cannot run away. A scan that
    runs out reports ``timed_out`` with the matches it already has.
    """

    __slots__ = ("_deadline", "expired")

    def __init__(self, seconds: float = 0.0) -> None:
        self._deadline = (time.monotonic() + seconds) if seconds > 0 else None
        self.expired = False

    def out_of_time(self) -> bool:
        if self._deadline is not None and time.monotonic() > self._deadline:
            self.expired = True
        return self.expired


def _grep_lines(
    lines: Sequence[str],
    regex: "re.Pattern[str]",
    budget: int,
    clock: Optional[_Budget] = None,
) -> List[Dict[str, Any]]:
    """Matching lines with ``_CONTEXT_LINES`` of context, 1-indexed."""
    out: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        if len(out) >= budget or (clock is not None and clock.out_of_time()):
            break
        if not regex.search(line):
            continue
        before = [ln.strip()[:_LINE_CHARS] for ln in lines[max(0, i - _CONTEXT_LINES): i]]
        after = [ln.strip()[:_LINE_CHARS] for ln in lines[i + 1: i + 1 + _CONTEXT_LINES]]
        out.append({"line": i + 1, "text": line.strip()[:_LINE_CHARS], "before": before, "after": after})
    return out


def _text_pattern(text: str, pattern: str, base: Dict[str, Any], unit: str) -> DocumentReadResult:
    regex, note = _compile(pattern)
    lines = text.splitlines()
    clock = _Budget(DOCUMENT_READ_PATTERN_BUDGET_SECONDS)
    matches = _grep_lines(lines, regex, DOCUMENT_READ_MAX_MATCHES, clock)
    payload = {
        **base,
        "mode": "pattern",
        "pattern": pattern,
        "unit": unit,
        f"{unit}_count": len(lines),
        "match_count": len(matches),
        "truncated": len(matches) >= DOCUMENT_READ_MAX_MATCHES,
        "matches": matches,
    }
    if note:
        payload["pattern_note"] = note
    if clock.expired:
        payload["timed_out"] = True
        payload["hint"] = (
            "The search ran out of time; these are the matches found so far. "
            "Use a simpler pattern, or read the document directly."
        )
    return DocumentReadResult(mode="pattern", payload=payload, format=base.get("format"))


_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9\s\-\(\)\[\]]")


def _unique_document_name(filename: str, suffix: str) -> str:
    """A Bedrock-safe document name that cannot collide with any other block in
    the conversation. Bedrock allows alphanumerics, whitespace, hyphens,
    parentheses and square brackets, and rejects duplicate names across the
    whole message history — so every slice gets a fresh random tail."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = re.sub(r"\s+", " ", _NAME_UNSAFE.sub(" ", stem)).strip()[:40] or "document"
    return f"{stem} {suffix} {uuid.uuid4().hex[:6]}"
