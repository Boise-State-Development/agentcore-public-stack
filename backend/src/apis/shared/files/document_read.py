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

An **image** (:func:`read_image`) has no pages or text, so the tool returns it
whole: the stored compact copy ``image_downscale`` wrote at upload, as a native
``image`` block. It is the recovery path for an image whose turn compaction
summarized away (unified-attachment-delivery §6.2): restore re-sends only the
history after the checkpoint, so before this an image in a summarized turn was
gone for good while a document beside it stayed readable. The listing names
the session's images for the same reason, under ``images``.

A **query** mode (:func:`search_documents`) searches the conversation's large
documents semantically through the shared ``attachments`` managed KB
(``attachment_search.py``, the only module that may), when the deployment has it.
It finds the place; a page-range read then shows the page at full fidelity.

A **prepared** document (a reference-class upload the ``large-document-prepare``
Lambda has processed; ``large_document.py``) is read from its ``_derived/``
sidecars instead of the original: the text layer for pattern, text and index,
and only the covering page shards for a page range. Same modes, same output,
same caps; only where the bytes come from differs.

Every read goes through the DynamoDB user-files table (ownership by key shape,
``PK = USER#{userId}``) and reuses the workspace service's S3 client. No
operation accepts a model-supplied S3 key. Nothing here emits metrics or
touches the agent layer — the tool does that.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import logging
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from . import large_document
from .models import (
    INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
    FileMetadata,
    FileStatus,
    get_file_format,
    is_presentation_file,
    is_tabular_file,
)
from .office_charts import chart_line, chart_reference, load_charts
from .pdfium_lock import pdfium_lock, serialized_pdfium
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
#: Documents larger than this are not searched or sliced from the original (the
#: inline upload cap is 4 MB). A prepared document is exempt: it is read from
#: its sidecars, which are bounded per call.
_MAX_DOCUMENT_BYTES = int(os.environ.get("DOCUMENT_READ_MAX_SOURCE_BYTES", 8 * 1024 * 1024))
#: Raw PDF bytes the page reads of ONE tool round may return between them.
#: Strands puts every tool result of a round into one user message, and that
#: message is one AgentCore Memory event — the same unit, and so the same
#: 7.0 MB raw budget, as a turn's inline attachments (see
#: ``INLINE_ATTACHMENTS_MAX_TOTAL_BYTES`` for the derivation from the 10 MB
#: event quota). Over it, ``CreateEvent`` returns 413 and Strands force-stops
#: the turn. A page count is not a byte bound: in prod one 8-page slice of a
#: scanned PDF was 86 MB. The tool holds a round's reads to this budget
#: (``document_read_tool._PageReadBudget``); a read that would pass it returns
#: fewer pages, or the text layer when not even its first page fits.
#: ``0`` (or any non-positive value) disables the budget.
DOCUMENT_READ_MAX_BATCH_BYTES = int(
    os.environ.get("DOCUMENT_READ_MAX_BATCH_BYTES", INLINE_ATTACHMENTS_MAX_TOTAL_BYTES)
)


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


#: Bedrock image formats ``document_read`` returns an image as.
IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "webp"})


def image_format_for(mime_type: str) -> Optional[str]:
    """The Bedrock image format of an uploaded image, or ``None`` for anything else."""
    fmt = get_file_format((mime_type or "").lower().split(";")[0].strip())
    return fmt if fmt in IMAGE_FORMATS else None


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
    #: Search results returned (query mode only).
    hits: int = 0


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


#: ``source`` of a file imported from an attached folder:
#: ``connector:{connector_id}:folder:{folder_attach_id}`` (app-api's expansion).
_FOLDER_SOURCE_MARK = ":folder:"


def _scope_session_ids(session_id: str, ancestor_session_ids: Sequence[str]) -> List[str]:
    """The conversation, then a fork's parent and root; blanks and repeats dropped."""
    return list(dict.fromkeys(sid for sid in (session_id, *ancestor_session_ids) if sid))


async def _list_scope_files(session_ids: Sequence[str]) -> List[FileMetadata]:
    """READY rows of each session, in order, read concurrently. Not owner-filtered:
    the session index is keyed by conversation, so every caller filters on user."""
    repo = get_file_upload_repository()
    per_session = await asyncio.gather(
        *(repo.list_session_files(sid, status=FileStatus.READY) for sid in session_ids)
    )
    return [meta for rows in per_session for meta in rows]


async def list_session_documents(
    user_id: str,
    session_id: str,
    *,
    ancestor_session_ids: Sequence[str] = (),
    include_images: bool = False,
) -> Dict[str, Any]:
    """The session's READY documents the tool can read, newest first.

    ``include_images`` (the tool's listing) adds ``images``: the session's
    images, which the tool returns whole by upload id, so a model whose image
    turn was compacted away can find it again. The key is present only when
    the session has one. Callers that resolve a *document* by name leave it
    off, so an image can never be matched where a document was asked for.

    A file from an attached folder also carries ``path``: the folder's name and
    the file's path inside it (drive-folder spec §4.4), so a model that lost the
    manifest to compaction can still find its way around the folder.

    ``ancestor_session_ids`` (a fork's parent and root) appends this user's
    documents in those sessions after the session's own, because the fork's
    copied messages name them. Another user's rows are never listed.
    """
    _require_identity(user_id, session_id)
    session_ids = _scope_session_ids(session_id, ancestor_session_ids)
    files = await _list_scope_files(session_ids)
    documents: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []
    folder_files = False
    listed: set = set()
    for meta in files:
        if meta.user_id != user_id or meta.upload_id in listed:
            continue
        listed.add(meta.upload_id)
        fmt = document_format_for(meta.mime_type, meta.filename)
        if fmt is None:
            image_fmt = image_format_for(meta.mime_type) if include_images else None
            if image_fmt is not None:
                images.append(
                    {
                        "upload_id": meta.upload_id,
                        "filename": meta.filename,
                        "format": image_fmt,
                        "size_bytes": meta.size_bytes,
                    }
                )
            continue
        folder_files = folder_files or _FOLDER_SOURCE_MARK in (meta.source or "")
        documents.append(
            {
                "upload_id": meta.upload_id,
                "filename": meta.filename,
                "format": fmt,
                "size_bytes": meta.size_bytes,
                "modes": _modes_for(fmt),
            }
        )
    if folder_files:
        paths: Dict[str, str] = {}
        for sid in session_ids:
            paths.update(await _folder_paths(user_id, sid))
        for doc in documents:
            path = paths.get(doc["upload_id"])
            if path:
                doc["path"] = path
    listing: Dict[str, Any] = {"documents": documents, "count": len(documents)}
    if images:
        listing["images"] = images
    return listing


async def _folder_paths(user_id: str, session_id: str) -> Dict[str, str]:
    """``upload_id`` → ``folder name/relative path`` for the session's folder files.

    One ``SessionIndex`` query, only when the listing holds a folder file. Never
    raises: without it the listing is what it was before folders existed.
    """
    try:
        from apis.shared.files.folder_attachments import ENTRY_ATTACHED
        from apis.shared.files.folder_repository import get_folder_attachment_repository

        folders = await get_folder_attachment_repository().list_session_folders(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("Folder paths unavailable for the document listing", exc_info=True)
        return {}
    paths: Dict[str, str] = {}
    for folder in folders:
        if folder.user_id != user_id:
            continue
        for entry in folder.files:
            if entry.status == ENTRY_ATTACHED and entry.upload_id:
                paths[entry.upload_id] = f"{folder.folder_name}/{entry.relative_path}"
    return paths


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


async def session_has_presentation_files(user_id: str, session_id: str) -> bool:
    """Whether the session has at least one READY PowerPoint deck (.pptx).

    The PowerPoint auto-enable gate, the deck counterpart of
    :func:`session_has_tabular_files`: same one-query cost, same "memoize the
    positive answer" contract. Decks never go inline (Bedrock has no pptx
    document format) and this module refuses them, so a session holding one
    is a session whose turns need the PowerPoint tools to read it at all.
    """
    _require_identity(user_id, session_id)
    files = await get_file_upload_repository().list_session_files(session_id, status=FileStatus.READY)
    return any(meta.user_id == user_id and is_presentation_file(meta.filename, meta.mime_type) for meta in files)


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


async def _read_through(meta: FileMetadata, *, reader: str) -> FileMetadata:
    """``meta``, or a shared file read through to its original (handoffs spec §4.9 rule 4).

    A reference whose original was deleted answers "No longer available (deleted from …)"
    as a ``DocumentReadError`` the tool relays, never as ``NoSuchKey`` (rule 5).
    """
    from .linked_files import linked_file_gone_message, readable_view

    view = await readable_view(meta, reader=reader)
    if view is None:
        raise DocumentReadError(linked_file_gone_message(meta))
    return view


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
    max_document_bytes: Optional[int] = None,
) -> DocumentReadResult:
    """Read part of one document. See the module docstring for the modes.

    ``text_only`` is for a model whose catalog row declares TEXT input only:
    a PDF page range comes back as those pages' text layer instead of a
    native ``document`` block, which such a model rejects outright.

    ``max_document_bytes`` caps the native PDF a page range returns: the
    range is shortened until it fits (``next_start`` says where to resume),
    and when not even its first page fits, the pages' text layer comes back
    instead. ``None`` or a non-positive value means no cap. The tool passes
    what is left of its round's :data:`DOCUMENT_READ_MAX_BATCH_BYTES`.
    """
    _require_identity(user_id, session_id)
    if not upload_id:
        raise WorkspaceValidationError("upload_id is required (call with no arguments to list documents)")
    if offset < 0:
        raise WorkspaceValidationError("offset must be >= 0")

    meta = await _read_through(await _get_owned_ready_file(user_id, upload_id), reader="document_read")
    fmt = document_format_for(meta.mime_type, meta.filename)
    if fmt is None and image_format_for(meta.mime_type) is not None:
        return await read_image(meta, text_only=text_only, max_bytes=max_document_bytes)
    if fmt is None:
        raise DocumentReadError(
            f"'{meta.filename}' is not a readable document. Spreadsheets go through "
            "analyze_spreadsheet and presentations through read_powerpoint_presentation."
        )
    prepared = large_document.is_prepared(meta)
    if not prepared and meta.size_bytes > _MAX_DOCUMENT_BYTES:
        raise DocumentReadError(_unreadable_message(meta))

    pages = parse_page_range(page_range)
    pattern_text = (pattern or "").strip() or None
    limit = clamp_max_pages(max_pages)

    base = {
        "upload_id": meta.upload_id,
        "filename": meta.filename,
        "format": fmt,
        "size_bytes": meta.size_bytes,
    }

    byte_cap = max_document_bytes if max_document_bytes and max_document_bytes > 0 else None

    if prepared:
        return await _read_prepared(
            user_id, upload_id, meta, fmt, base,
            pages=pages, pattern=pattern_text, limit=limit, offset=offset, text_only=text_only,
            max_document_bytes=byte_cap,
        )

    if fmt == "pdf":
        raw = await _fetch_bytes(meta)
        if pattern_text:
            return await asyncio.to_thread(_pdf_pattern, raw, pattern_text, base)
        if pages and text_only:
            return await asyncio.to_thread(_pdf_pages_text, raw, pages, limit, base)
        if pages:
            return await asyncio.to_thread(_pdf_pages, raw, pages, limit, base, meta.filename, byte_cap)
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
        return await asyncio.to_thread(
            lambda: _text_pattern("\n".join(text_lines(fmt, raw)), pattern_text, base, "line")
        )
    return await _text_read(user_id, upload_id, offset, base, fmt)


async def read_image(
    meta: FileMetadata, *, text_only: bool = False, max_bytes: Optional[int] = None
) -> DocumentReadResult:
    """One image, whole, as a native ``image`` block: its stored compact copy.

    The compact copy (``image_downscale``: Claude's standard tier, 1,568 px and
    at most 340 KB) is what a re-look needs and what keeps a round of them
    under the Memory event budget: twenty fit in one round. A row with no
    compact copy answers with its high copy, and a legacy row with its original
    (``model_image_source``). The bytes are the stored object's, unchanged, so
    the tool result restores byte for byte like any other.

    ``max_bytes`` is what is left of the round's byte budget; an image past it,
    or past Bedrock's image limit, is refused with a reason rather than sent.
    """
    from .file_resolver import model_image_source
    from .image_downscale import MODEL_IMAGE_MAX_BYTES

    if text_only:
        raise DocumentReadError(
            f"'{meta.filename}' is an image, and the model in use reads text only, so it cannot be viewed."
        )
    key, mime_type = model_image_source(meta, "compact")
    fmt = image_format_for(mime_type) or image_format_for(meta.mime_type)
    raw = await asyncio.to_thread(_get_object_bytes, meta.s3_bucket, key)
    if len(raw) > MODEL_IMAGE_MAX_BYTES:
        raise DocumentReadError(f"'{meta.filename}' is too large to show again ({len(raw)} bytes).")
    if max_bytes is not None and max_bytes > 0 and len(raw) > max_bytes:
        raise DocumentReadError(
            f"'{meta.filename}' does not fit what this round of reads has left "
            f"({_megabytes(max_bytes)}); read it in a call of its own."
        )
    high_key = model_image_source(meta, "high")[0]
    variant = "original" if key == meta.s3_key else ("high" if key == high_key else "compact")
    payload = {
        "upload_id": meta.upload_id,
        "filename": meta.filename,
        "format": fmt,
        "size_bytes": len(raw),
        "variant": variant,
    }
    return DocumentReadResult(
        mode="image",
        payload=payload,
        extra_blocks=[{"image": {"format": fmt, "source": {"bytes": raw}}}],
        bytes_returned=len(raw),
        format=fmt,
    )


# ---------------------------------------------------------------------------
# Semantic search (the attachments managed KB; large-document spec §4F)
# ---------------------------------------------------------------------------

_SEARCH_NOT_READY = {
    "preparing": "is still being prepared; it can be searched a few minutes after that.",
    "indexing": "is still being indexed for search, which takes a few minutes.",
    "evicted": (
        "was removed from the search index after a period of inactivity. It is "
        "re-indexed when this conversation is opened, which takes a few minutes."
    ),
    "unavailable": "is not available for search.",
    "off": "is not indexed for search (only documents too large to keep in context are).",
}


def _not_searchable_entry(meta: FileMetadata, status: str) -> Dict[str, Any]:
    return {
        "upload_id": meta.upload_id,
        "filename": meta.filename,
        "search_status": status,
        "message": f"'{meta.filename}' {_SEARCH_NOT_READY[status]}",
    }


def _pattern_suggestion(metas: Sequence[FileMetadata]) -> str:
    pdf = any(document_format_for(m.mime_type, m.filename) == "pdf" for m in metas)
    then = "then page_range to read the pages it finds" if pdf else "then read on from the matches"
    return f"use pattern on the document (a regex over its text), {then}."


def _search_fallback_hint(metas: Sequence[FileMetadata]) -> str:
    return f"Instead, {_pattern_suggestion(metas)}"


async def search_documents(
    user_id: str,
    session_id: str,
    query: str,
    *,
    upload_id: Optional[str] = None,
    kb_enabled: bool = True,
    ancestor_session_ids: Sequence[str] = (),
) -> DocumentReadResult:
    """Semantic search over this conversation's indexed large documents.

    ``ancestor_session_ids`` (a fork's parent and root) widens the conversation
    to the caller's documents attached there, as ``list_session_documents``
    does: a fork's copied messages name them, and they stay indexed under the
    session they were attached in. Another user's rows are never searched.

    With ``upload_id``, that document only. A document that is not searchable yet
    (or any more) comes back as its status and the suggestion to use ``pattern``,
    never as an error: Tier 1 reads work whatever the index says. A knowledge
    base that is missing or failing degrades the same way.
    """
    from .attachment_index import SEARCH_SEARCHABLE, is_text_indexed, search_status
    from .attachment_search import search_attachments

    _require_identity(user_id, session_id)
    query_text = (query or "").strip()
    if not query_text:
        raise WorkspaceValidationError("query must not be empty")

    session_ids = _scope_session_ids(session_id, ancestor_session_ids)
    if upload_id:
        meta = await _get_owned_ready_file(user_id, upload_id)
        if meta.session_id not in session_ids:
            raise WorkspaceFileNotFoundError(f"No file with id '{upload_id}' found in this conversation")
        candidates = [meta]
    else:
        files = await _list_scope_files(session_ids)
        seen: set = set()
        candidates = []
        for m in files:
            if m.user_id != user_id or m.upload_id in seen:
                continue
            seen.add(m.upload_id)
            if document_format_for(m.mime_type, m.filename) is not None:
                candidates.append(m)

    # A shared file is searched as its original, which is what the attachments KB indexed
    # (handoffs spec §4.9 rule 4). A reference whose original is gone reads as unavailable.
    from .linked_files import readable_view

    linked: Dict[str, Optional[FileMetadata]] = {}
    for m in candidates:
        if m.linked_from is not None:
            linked[m.upload_id] = await readable_view(m, reader="document_read")
    candidates = [linked.get(m.upload_id) or m for m in candidates]
    statuses = {m.upload_id: search_status(m, kb_enabled=kb_enabled) for m in candidates}
    searchable = [m for m in candidates if statuses[m.upload_id] == SEARCH_SEARCHABLE]
    not_searchable = [
        _not_searchable_entry(m, statuses[m.upload_id])
        for m in candidates
        if statuses[m.upload_id] != SEARCH_SEARCHABLE
    ]
    payload: Dict[str, Any] = {"mode": "query", "query": query_text}
    if upload_id:
        payload["upload_id"] = upload_id
    if not_searchable:
        payload["not_searchable"] = not_searchable
    partial = [m for m in searchable if is_text_indexed(m)]
    if partial:
        payload["images_pending"] = [
            {
                "upload_id": m.upload_id,
                "filename": m.filename,
                "message": (
                    f"'{m.filename}' is searchable by its text; its images and scanned pages "
                    "are still being indexed. page_range reads show every page in full."
                ),
            }
            for m in partial
        ]

    if not searchable:
        payload.update(results=[], count=0, hint=_search_fallback_hint(candidates))
        if not candidates:
            payload["hint"] = "This conversation has no documents to search."
        return DocumentReadResult(mode="query", payload=payload)

    try:
        own = [m for m in searchable if m.linked_from is None]
        hits = (
            await search_attachments(
                user_id,
                session_id,
                query_text,
                upload_id=upload_id or None,
                ancestor_session_ids=session_ids[1:],
            )
            if own
            else []
        )
        # Each shared file is searched where its original was indexed, narrowed to that one
        # document, so a reference reaches only the file that was shared and nothing else in
        # the conversation it came from. Its hits are reported under the reference's id.
        for m in (m for m in searchable if m.linked_from is not None):
            assert m.linked_from is not None
            found = await search_attachments(
                user_id, m.linked_from.session_id, query_text, upload_id=m.linked_from.upload_id
            )
            hits.extend(dataclasses.replace(hit, upload_id=m.upload_id) for hit in found)
        if len(searchable) > len(own):
            hits.sort(key=lambda hit: hit.score if hit.score is not None else 0.0, reverse=True)
            hits = hits[: _attachment_top_k()]
    except Exception as exc:  # noqa: BLE001 - a failed search degrades to pattern, never to an error
        logger.warning("attachment search failed (%s); suggesting pattern", type(exc).__name__)
        payload.update(
            results=[], count=0,
            search_error="Search is unavailable right now.",
            hint=_search_fallback_hint(searchable),
        )
        return DocumentReadResult(mode="query", payload=payload)

    results = [
        {
            "upload_id": hit.upload_id,
            "filename": hit.filename,
            **({"page": hit.page} if hit.page is not None else {}),
            "text": hit.text,
            **({"truncated": True} if hit.truncated else {}),
        }
        for hit in hits
    ]
    payload.update(results=results, count=len(results))
    if any(hit.page is not None for hit in hits):
        payload["hint"] = (
            "These are excerpts. Read the pages they come from with page_range to see "
            "them in full, including tables and figures, before answering from them."
        )
    elif results:
        payload["hint"] = "These are excerpts. Use pattern to find the exact passage and its context."
    else:
        payload["hint"] = f"Nothing matched. Try other words, or {_pattern_suggestion(searchable)}"
    return DocumentReadResult(
        mode="query",
        payload=payload,
        bytes_returned=sum(len(r["text"].encode("utf-8")) for r in results),
        hits=len(results),
    )


def _attachment_top_k() -> int:
    from .attachment_search import DEFAULT_TOP_K

    return DEFAULT_TOP_K


async def _text_read(user_id: str, upload_id: str, offset: int, base: Dict[str, Any], fmt: str) -> DocumentReadResult:
    """Text mode for txt / md / html: a ranged GET of the original, so it is
    already bounded by ``WORKSPACE_READ_MAX_BYTES`` whatever the file's size.
    Prepared documents use it too: the bytes it returns are the original's
    exactly, which a text layer split into lines could not reproduce."""
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


def _unreadable_message(meta: FileMetadata) -> str:
    """Why a document over the source cap cannot be read (no usable sidecars)."""
    status = large_document.preparation_status(meta)
    if status == large_document.STATUS_PENDING:
        return (
            f"'{meta.filename}' is still being prepared for reading. "
            "Tell the user it will be readable shortly, then try again."
        )
    if status == large_document.STATUS_FAILED:
        return f"'{meta.filename}' could not be prepared for reading, so it cannot be searched or read."
    return f"'{meta.filename}' is too large to read ({meta.size_bytes} bytes)."


# ---------------------------------------------------------------------------
# Prepared documents (``_derived/`` sidecars; large_document.py)
# ---------------------------------------------------------------------------


async def _fetch_text_layer(meta: FileMetadata) -> List[str]:
    key = large_document.text_layer_key(meta.s3_key)
    body = await asyncio.to_thread(_get_object_bytes, meta.s3_bucket, key)
    return await asyncio.to_thread(large_document.decode_text_layer, body)


async def _read_prepared(
    user_id: str,
    upload_id: str,
    meta: FileMetadata,
    fmt: str,
    base: Dict[str, Any],
    *,
    pages: Optional[Tuple[int, int]],
    pattern: Optional[str],
    limit: int,
    offset: int,
    text_only: bool,
    max_document_bytes: Optional[int] = None,
) -> DocumentReadResult:
    """``read_document`` over the sidecars. Each branch ends in the same
    helper the original's branch does, so the payloads are the same."""
    if fmt == "pdf":
        # Same precedence as the original's branch: pattern, then a page range
        # (as text for a text-only model), then the index.
        if pages and not pattern and not text_only:
            return await _pdf_pages_from_shards(meta, pages, limit, base, max_document_bytes)
        texts = await _fetch_text_layer(meta)
        page_text = texts.__getitem__
        if pattern:
            regex, note = _compile(pattern)
            return await asyncio.to_thread(_scan_pages, len(texts), page_text, pattern, regex, note, base)
        if pages:
            return await asyncio.to_thread(_pages_text, len(texts), page_text, pages, limit, base)
        return await asyncio.to_thread(_page_index, len(texts), page_text, base)

    if pages:
        raise WorkspaceValidationError(
            f"page_range applies to PDFs only; '{meta.filename}' is {fmt}. Use pattern or offset instead."
        )

    if fmt == "docx":
        paragraphs = await _fetch_text_layer(meta)
        return await asyncio.to_thread(_docx_read_paragraphs, paragraphs, pattern, offset, base)

    if pattern:
        lines = await _fetch_text_layer(meta)
        return await asyncio.to_thread(_lines_pattern, lines, pattern, base, "line")
    return await _text_read(user_id, upload_id, offset, base, fmt)


async def _pdf_pages_from_shards(
    meta: FileMetadata,
    pages: Tuple[int, int],
    limit: int,
    base: Dict[str, Any],
    max_document_bytes: Optional[int] = None,
) -> DocumentReadResult:
    """``_pdf_pages`` over the page shards: the range is resolved against the
    prepared page count, then only the shards covering it are fetched (in
    parallel) and sliced with the same ``import_pages`` re-assembly. Shrinking
    the range to ``max_document_bytes`` re-assembles from the shards already
    fetched; it never fetches again."""
    count = large_document.prepared_page_count(meta)
    shard_pages = large_document.prepared_shard_pages(meta)
    start, end, truncated = _resolve_range(pages, count, limit)
    shards = large_document.covering_shards(start, end, count, shard_pages)
    blobs = await asyncio.gather(
        *(
            asyncio.to_thread(_get_object_bytes, meta.s3_bucket, large_document.shard_key(meta.s3_key, s, e))
            for s, e in shards
        )
    )
    fetched = list(zip(shards, blobs))

    def slice_to(last: int) -> bytes:
        return _assemble_from_shards([sb for sb in fetched if sb[0][0] <= last], start, last)

    fit = await asyncio.to_thread(_fit_to_bytes, slice_to, start, end, max_document_bytes)
    if fit.sliced is None:
        texts = await _fetch_text_layer(meta)
        note = _over_budget_note(start, fit.last_size, max_document_bytes)
        return _mark_over_budget(await asyncio.to_thread(
            _pages_text, len(texts), texts.__getitem__, (start, end), limit, base, note
        ))
    return _pages_result(
        fit.sliced, start, fit.end, count, truncated, base, meta.filename,
        requested_end=end, max_document_bytes=max_document_bytes,
    )


@serialized_pdfium
def _assemble_from_shards(shards: Sequence[Tuple[Tuple[int, int], bytes]], start: int, end: int) -> bytes:
    import pypdfium2 as pdfium

    sub = pdfium.PdfDocument.new()
    try:
        for (shard_start, shard_end), raw in shards:
            first, last = max(start, shard_start), min(end, shard_end)
            if last < first:
                continue
            shard = _open_pdf(raw)
            try:
                sub.import_pages(shard, pages=list(range(first - shard_start, last - shard_start + 1)))
            finally:
                shard.close()
        buffer = io.BytesIO()
        sub.save(buffer)
    finally:
        sub.close()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PDF (pypdfium2 — already a dependency for attachment thumbnails)
#
# PDFium is not thread-safe, so every function here runs under the process-wide
# lock in ``pdfium_lock``: the ones that open a document hold it for the
# document's whole lifetime, and the two per-call helpers take it again (it is
# re-entrant) so a caller elsewhere is serialized even without the outer guard.
# ---------------------------------------------------------------------------


@serialized_pdfium
def _open_pdf(raw: bytes):
    import pypdfium2 as pdfium  # lazy: native lib, not needed on non-PDF paths

    try:
        return pdfium.PdfDocument(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise DocumentReadError(f"Could not open the PDF: {e}") from e


@serialized_pdfium
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


def _resolve_range(pages: Tuple[int, int], count: int, limit: int) -> Tuple[int, int, bool]:
    """``(start, end, truncated)``: the requested range clamped to the document
    and to ``limit`` pages. Raises when it starts past the last page."""
    start, end = pages
    if start > count:
        raise WorkspaceValidationError(f"page_range starts at {start} but the document has {count} pages.")
    end = min(end, count)
    truncated = False
    if end - start + 1 > limit:
        end = start + limit - 1
        truncated = True
    return start, end, truncated


@dataclass
class _Fit:
    """What :func:`_fit_to_bytes` settled on. ``sliced`` is ``None`` when not
    even the first page fits; ``last_size`` is then that page's size."""

    sliced: Optional[bytes]
    end: int
    last_size: int


def _fit_to_bytes(
    slice_to: Callable[[int], bytes], start: int, end: int, max_bytes: Optional[int]
) -> _Fit:
    """The longest run of pages from ``start`` whose re-assembled PDF fits
    ``max_bytes``, re-slicing with ``slice_to(last_page)``.

    A binary search over the last page, because page sizes are uneven (one
    scanned page can outweigh the rest of the range) and no estimate from
    the overshoot survives that. A slice only grows as pages are added, so
    the search is exact. Bounded: one slice of the full range, then at most
    ``ceil(log2(pages))`` more — six in all for the 20-page hard cap — each
    under the process-wide PDFium lock. ``max_bytes`` of ``None`` returns the
    full range untouched.
    """
    sliced = slice_to(end)
    if max_bytes is None or len(sliced) <= max_bytes:
        return _Fit(sliced, end, len(sliced))
    best: Optional[_Fit] = None
    first_page_size = len(sliced) if end == start else 0
    lo, hi = start, end - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = slice_to(mid)
        if len(candidate) <= max_bytes:
            best = _Fit(candidate, mid, len(candidate))
            lo = mid + 1
        else:
            if mid == start:
                first_page_size = len(candidate)
            hi = mid - 1
    return best or _Fit(None, start - 1, first_page_size)


def _megabytes(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _mark_over_budget(result: DocumentReadResult) -> DocumentReadResult:
    """Tag a text-layer fallback so the tool's record can count it, and say so
    when the pages have no text at all (a scan), so blank text is not read as
    blank pages."""
    result.payload["page_images_omitted"] = "byte_budget"
    if not any((t.get("text") or "").strip() for t in result.payload.get("page_texts", [])):
        result.payload["note"] += " These pages have no text layer (they are likely scanned)."
    return result


def _over_budget_note(start: int, page_bytes: int, max_bytes: Optional[int]) -> str:
    """The text-layer fallback's note, for a page too large to attach."""
    budget = max_bytes or 0
    lead = (
        f"Page {start} as a PDF is {_megabytes(page_bytes)}, more than the "
        f"{_megabytes(budget)} that page reads can return in this step, so these are the "
        "pages' text layers; page images, charts and scanned pages are not included."
    )
    if DOCUMENT_READ_MAX_BATCH_BYTES > 0 and budget < DOCUMENT_READ_MAX_BATCH_BYTES and page_bytes <= DOCUMENT_READ_MAX_BATCH_BYTES:
        return lead + (
            " Other page reads in this step used the rest; read this range again on its own "
            "in your next step to get the pages themselves."
        )
    return lead + " A page that large cannot be attached; work from this text."


@serialized_pdfium
def _slice_pdf(pdf: Any, start: int, end: int) -> bytes:
    import pypdfium2 as pdfium

    sub = pdfium.PdfDocument.new()
    try:
        sub.import_pages(pdf, pages=list(range(start - 1, end)))
        buffer = io.BytesIO()
        sub.save(buffer)
    finally:
        sub.close()
    return buffer.getvalue()


@serialized_pdfium
def _pdf_pages(
    raw: bytes,
    pages: Tuple[int, int],
    limit: int,
    base: Dict[str, Any],
    filename: str,
    max_document_bytes: Optional[int] = None,
) -> DocumentReadResult:
    pdf = _open_pdf(raw)
    try:
        count = len(pdf)
        start, end, truncated = _resolve_range(pages, count, limit)
        fit = _fit_to_bytes(lambda last: _slice_pdf(pdf, start, last), start, end, max_document_bytes)
        if fit.sliced is None:
            note = _over_budget_note(start, fit.last_size, max_document_bytes)
            return _mark_over_budget(
                _pages_text(count, lambda i: _pdf_page_text(pdf, i), (start, end), limit, base, note)
            )
    finally:
        pdf.close()
    return _pages_result(
        fit.sliced, start, fit.end, count, truncated, base, filename,
        requested_end=end, max_document_bytes=max_document_bytes,
    )


def _pages_result(
    sliced: bytes,
    start: int,
    end: int,
    count: int,
    truncated: bool,
    base: Dict[str, Any],
    filename: str,
    *,
    requested_end: Optional[int] = None,
    max_document_bytes: Optional[int] = None,
) -> DocumentReadResult:
    returned = end - start + 1
    byte_truncated = requested_end is not None and end < requested_end
    name = _unique_document_name(filename, f"p{start}-{end}")
    payload = {
        **base,
        "mode": "pages",
        "page_count": count,
        "pages": {"start": start, "end": end},
        "pages_returned": returned,
        "truncated_to_max_pages": truncated,
        "next_start": end + 1 if ((truncated or byte_truncated) and end < count) else None,
        "page_numbering": (
            f"The attached document contains original pages {start}-{end} only; "
            f"its page k is original page {start} + k - 1."
        ),
    }
    if byte_truncated:
        payload["truncated_to_byte_budget"] = True
        payload["byte_budget_note"] = (
            f"Pages {start}-{requested_end} would not fit in the {_megabytes(max_document_bytes or 0)} "
            f"that page reads can return in this step, so this read stops at page {end}. "
            f"Read from page {end + 1} in your next step."
        )
    block = {"document": {"format": "pdf", "name": name, "source": {"bytes": sliced}}}
    return DocumentReadResult(
        mode="pages", payload=payload, document_block=block,
        pages_returned=returned, bytes_returned=len(sliced), format="pdf",
    )


@serialized_pdfium
def _pdf_pages_text(raw: bytes, pages: Tuple[int, int], limit: int, base: Dict[str, Any]) -> DocumentReadResult:
    """``_pdf_pages`` for a text-only model: the same range and the same
    ``max_pages`` cap, returned as each page's text layer in the payload."""
    pdf = _open_pdf(raw)
    try:
        return _pages_text(len(pdf), lambda i: _pdf_page_text(pdf, i), pages, limit, base)
    finally:
        pdf.close()


_TEXT_ONLY_NOTE = (
    "The current model reads text only, so these are the pages' text layers; "
    "page images, charts and scanned pages are not included."
)


def _pages_text(
    count: int,
    page_text: Callable[[int], str],
    pages: Tuple[int, int],
    limit: int,
    base: Dict[str, Any],
    note: str = _TEXT_ONLY_NOTE,
) -> DocumentReadResult:
    start, end, truncated = _resolve_range(pages, count, limit)
    texts = [{"page": i + 1, "text": page_text(i)} for i in range(start - 1, end)]
    returned = end - start + 1
    payload = {
        **base,
        "mode": "pages_text",
        "page_count": count,
        "pages": {"start": start, "end": end},
        "pages_returned": returned,
        "truncated_to_max_pages": truncated,
        "next_start": end + 1 if (truncated and end < count) else None,
        "note": note,
        "page_texts": texts,
    }
    return DocumentReadResult(
        mode="pages_text", payload=payload, pages_returned=returned,
        bytes_returned=sum(len(t["text"].encode("utf-8")) for t in texts), format="pdf",
    )


@serialized_pdfium
def _pdf_pattern(raw: bytes, pattern: str, base: Dict[str, Any]) -> DocumentReadResult:
    regex, note = _compile(pattern)
    pdf = _open_pdf(raw)
    try:
        return _scan_pages(len(pdf), lambda i: _pdf_page_text(pdf, i), pattern, regex, note, base)
    finally:
        pdf.close()


def _scan_pages(
    count: int,
    page_text: Callable[[int], str],
    pattern: str,
    regex: "re.Pattern[str]",
    note: Optional[str],
    base: Dict[str, Any],
) -> DocumentReadResult:
    """Pattern mode over a PDF's pages, from the document or its text layer."""
    # The clock starts after the open: lazily importing the pypdfium2 native
    # library and parsing the document are setup, not scanning, and charging
    # them to the scan's budget can report a timeout on pages never examined.
    clock = _Budget(DOCUMENT_READ_PATTERN_BUDGET_SECONDS)
    pages_searched = 0
    matches: List[Dict[str, Any]] = []
    pages_matched: List[int] = []
    for index in range(count):
        if len(matches) >= DOCUMENT_READ_MAX_MATCHES or clock.out_of_time():
            break
        pages_searched = index + 1
        lines = page_text(index).splitlines()
        page_no = index + 1
        hit = False
        for entry in _grep_lines(lines, regex, DOCUMENT_READ_MAX_MATCHES - len(matches), clock, across_wraps=True):
            entry["page"] = page_no
            matches.append(entry)
            hit = True
        if hit:
            pages_matched.append(page_no)
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


@serialized_pdfium
def _pdf_index(raw: bytes, base: Dict[str, Any]) -> DocumentReadResult:
    pdf = _open_pdf(raw)
    try:
        return _page_index(len(pdf), lambda i: _pdf_page_text(pdf, i), base)
    finally:
        pdf.close()


def _page_index(count: int, page_text: Callable[[int], str], base: Dict[str, Any]) -> DocumentReadResult:
    index: List[Dict[str, Any]] = []
    for i in range(min(count, DOCUMENT_READ_INDEX_PAGES)):
        snippet = ""
        for line in page_text(i).splitlines():
            line = line.strip()
            if line:
                snippet = line[:_SNIPPET_CHARS]
                break
        index.append({"page": i + 1, "snippet": snippet})
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


def annotate_docx_for_model(raw: bytes) -> bytes:
    """``raw`` with its tracked changes written out as text, while the flag is on.

    Returns ``raw`` itself when the flag is off or the document has no tracked
    changes, so every other document reads exactly as before.
    """
    from apis.shared.feature_flags import word_tracked_changes_enabled

    if not word_tracked_changes_enabled():
        return raw
    from apis.shared.kb_backend.docx_revisions import annotate_tracked_changes

    return annotate_tracked_changes(raw) or raw


def docx_paragraphs(raw: bytes) -> List[str]:
    """Paragraph text of a .docx, in document order. Tables contribute their
    cell paragraphs; runs are joined; tabs and line breaks become whitespace.
    Good enough for pattern search and bounded reading, not a renderer.

    With ``WORD_TRACKED_CHANGES_ENABLED`` on, tracked changes are written out as
    ``[deleted: …]`` / ``[inserted: …]`` first. Without it ``w:delText`` is not
    read, so a deletion vanishes and an insertion reads as settled text. The
    upload digest numbers paragraphs through this same function, so its outline
    and ``document_read`` agree only while app-api and the Runtime have the flag
    set the same way.

    A native chart is written out on the paragraph that anchors it, as
    ``office_charts.chart_line`` renders it, so ``pattern`` finds a value a
    chart plots where the chart sits."""
    raw = annotate_docx_for_model(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
            charts = load_charts(archive, "word/document.xml")
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
        if charts:
            lines = [chart_line(charts.get(rid), alt=alt) for rid, alt in _paragraph_charts(para)]
            text = " ".join(t for t in [text, *lines] if t)
        if text:
            paragraphs.append(text)
    return paragraphs


_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_WP_DOCPR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"


def _paragraph_charts(para: ElementTree.Element) -> List[Tuple[str, str]]:
    """``(relationship id, alt text)`` per chart this paragraph anchors itself:
    not one inside a nested paragraph (a text box's, counted on its own), nor
    the picture Word saves as an ``mc:Fallback`` for a new-style chart."""
    found: List[Tuple[str, str]] = []

    def walk(node: ElementTree.Element) -> None:
        for child in node:
            if child.tag in (_MC_FALLBACK, f"{_W_NS}p"):
                continue
            if child.tag == f"{_W_NS}drawing":
                rid = chart_reference(child)
                if rid is not None:
                    docpr = next(child.iter(_WP_DOCPR), None)
                    alt = ((docpr.get("descr") or docpr.get("title") or "") if docpr is not None else "").strip()
                    found.append((rid, alt))
                    continue
            walk(child)

    walk(para)
    return found


def _docx_read(raw: bytes, pattern: Optional[str], offset: int, base: Dict[str, Any]) -> DocumentReadResult:
    return _docx_read_paragraphs(docx_paragraphs(raw), pattern, offset, base)


def _docx_read_paragraphs(
    paragraphs: List[str], pattern: Optional[str], offset: int, base: Dict[str, Any]
) -> DocumentReadResult:
    """Pattern and text modes over a DOCX's paragraphs, from the document or
    its text layer (which stores ``docx_paragraphs`` verbatim)."""
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
    *,
    across_wraps: bool = False,
) -> List[Dict[str, Any]]:
    """Matching lines with ``_CONTEXT_LINES`` of context, 1-indexed.

    ``across_wraps`` is for a PDF page, whose lines are where the layout
    wrapped, not where the author broke the text: a match that runs across a
    line break ("Major" / "incident: at …") is also found, reported at the line
    it starts on with both lines as its text. Without it a phrase was invisible
    to ``pattern`` whenever the wrap fell inside it (attachment_delivery's
    ``whole_summarise_pdf_150k_on_demand``: Sonnet 4.6 missed the same
    incident in 3 of 3 samples).
    """
    out: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        if len(out) >= budget or (clock is not None and clock.out_of_time()):
            break
        text = line.strip()
        match = regex.search(text)
        if not match and across_wraps and i + 1 < len(lines):
            joined = f"{text} {lines[i + 1].strip()}"
            spanning = next(
                (m for m in regex.finditer(joined) if m.start() < len(text) < m.end()), None
            )
            if spanning is not None:
                text, match = joined, spanning
        if not match:
            continue
        before = [ln.strip()[:_LINE_CHARS] for ln in lines[max(0, i - _CONTEXT_LINES): i]]
        after = [ln.strip()[:_LINE_CHARS] for ln in lines[i + 1: i + 1 + _CONTEXT_LINES]]
        out.append({"line": i + 1, "text": _excerpt(text, match), "before": before, "after": after})
    return out


def _excerpt(text: str, match: "re.Match[str]") -> str:
    """The matched line, at most ``_LINE_CHARS``, cut around the match.

    A Word paragraph or an unwrapped text line is one "line" and runs to
    thousands of characters. Cutting it from the start returned text that did
    not contain the match whenever the match sat past the first ``_LINE_CHARS``,
    so the model saw a hit it could not read and gave up ("not stated"). Found
    by the large-document quality harness: four Word-document facts at the end
    of long paragraphs were answered 1 time in 12.
    """
    if len(text) <= _LINE_CHARS:
        return text
    span = match.end() - match.start()
    start = max(0, match.start() - max(0, _LINE_CHARS - span) // 2)
    start = min(start, len(text) - _LINE_CHARS)
    end = start + _LINE_CHARS
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _text_pattern(text: str, pattern: str, base: Dict[str, Any], unit: str) -> DocumentReadResult:
    return _lines_pattern(text.splitlines(), pattern, base, unit)


def _lines_pattern(lines: List[str], pattern: str, base: Dict[str, Any], unit: str) -> DocumentReadResult:
    """Pattern mode over numbered lines, from the document or its text layer."""
    regex, note = _compile(pattern)
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


# ---------------------------------------------------------------------------
# Provenance: one document's text as the units it is numbered by
# ---------------------------------------------------------------------------


def _units_text(fmt: str, raw: bytes) -> List[str]:
    """One string per page / paragraph / line, from the document's bytes.

    The same three extractors the retrieval modes above use, so a quote is
    verified against exactly the text ``document_read`` would have shown — not
    a second, differently-cleaned rendering of the same file. HTML is
    rendered by ``text_lines``, because that is what ``pattern`` mode searches.
    """
    if fmt == "pdf":
        with pdfium_lock():
            pdf = _open_pdf(raw)
            try:
                return [_pdf_page_text(pdf, i) for i in range(len(pdf))]
            finally:
                pdf.close()
    if fmt == "docx":
        return docx_paragraphs(raw)
    return text_lines(fmt, raw)


def text_lines(fmt: str, raw: bytes) -> List[str]:
    """The lines ``pattern`` mode searches in a txt, md or html document.

    HTML is rendered first (``html_markdown``, the renderer the Converse
    projection uses). Its raw lines are markup, and a minified page is a
    single line, so a pattern used to find only the first match in the whole
    document, cut to ``_LINE_CHARS`` around it: a fact beside an earlier
    lookalike could never be reached. Measured by the ``attachment_delivery``
    eval's ``arm_html_30k_on_demand`` cell (0/3 on Haiku 5.5 before this).
    """
    text = raw.decode("utf-8", errors="replace")
    if fmt == "html":
        from apis.shared.files.html_markdown import html_to_markdown

        text = html_to_markdown(text)
    return text.splitlines()


async def document_provenance_units(
    user_id: str, session_id: str, upload_id: str
) -> Tuple[FileMetadata, str, List[str]]:
    """``(meta, fmt, unit texts)`` for one of the session's documents.

    The document side of ``docs/specs/source-provenance-verification.md``. A
    *prepared* document is read from the text layer the prepare Lambda already
    wrote — ``text.jsonl``'s records are its persisted page markers, so this is
    one S3 GET and no parse. Anything else is fetched and extracted, the same
    work a ``document_read`` call does, under the same source-size bound.

    Never on the pre-first-token path: the only caller is a tool the model
    invokes after it has read a document and formed an answer.

    An empty list (a scanned PDF with no text layer) is returned rather than
    raised on, so the caller can say "nothing is traced here" instead of
    failing the turn.
    """
    _require_identity(user_id, session_id)
    if not upload_id:
        raise WorkspaceValidationError("upload_id is required")

    meta = await _read_through(await _get_owned_ready_file(user_id, upload_id), reader="document_read")
    fmt = document_format_for(meta.mime_type, meta.filename)
    if fmt is None:
        raise DocumentReadError(
            f"'{meta.filename}' is not a readable document, so there is nothing to "
            "verify a quote against."
        )

    if large_document.is_prepared(meta):
        return meta, fmt, await _fetch_text_layer(meta)

    if meta.size_bytes > _MAX_DOCUMENT_BYTES:
        raise DocumentReadError(_unreadable_message(meta))
    raw = await _fetch_bytes(meta)
    return meta, fmt, await asyncio.to_thread(_units_text, fmt, raw)
