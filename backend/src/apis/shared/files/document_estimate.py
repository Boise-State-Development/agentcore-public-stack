"""One token estimator for a document, read from the file row's facts.

``docs/specs/unified-attachment-delivery.md`` §4.1 and §9.1 (phase 2). Compaction
counted a document block as bytes/4 while offload counted pages x
``PDF_PAGE_TOKEN_ESTIMATE``. On dev on 2026-11-07 (Haiku 5.5, a 24-page,
134 KB PDF) the attached document cost about 91k input tokens, about 3.8k a
page: 1,560 for the page image and the rest for its dense text layer. bytes/4
said 33.6k, and the page formula 36k. The row facts say ``tokens.text`` 29,954
and ``tokens.native`` 36,000, which together are 66k.

**The formula.** One function, :func:`facts_tokens`:

* **PDF, delivered natively:** ``tokens.text + tokens.native``. Version 1 facts
  (#1966) wrote ``tokens.native`` as the image channel only (pages x
  ``PDF_PAGE_TOKEN_ESTIMATE``, floored by bytes/4). §4.1 defines a native PDF
  as text *plus* page images, which is what Bedrock bills, so the text layer is
  added here.
* **Anything else, or a block delivered as text:** ``tokens.text``. Bedrock
  extracts a .docx, HTML or text file to text whether it arrives as a document
  block or as text (``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED``), so the text is
  the cost. bytes/4 is about 9x low for a .docx, because the file is zipped.
* **A ``document_read`` slice** covers ``k`` of ``pages`` pages and costs the
  same share of the whole.

A row without current facts falls back to
:func:`~apis.shared.files.document_tokens.estimate_document_tokens` from the
bytes in hand: rows written before #1966, facts still being prepared, or a
document that never had a row.

**How facts reach the estimate.** The turn already reads the rows of the files it
attaches, for the planner. :func:`remember_session_documents` keeps their facts
in process, keyed by ``(user, session)``. Facts are immutable per set of bytes,
so whichever agent serves the session reads the same values. No read is added
before the first token. The session manager can fill a miss after the turn
(:func:`remember_session_documents` with the rows it loads), and so can the
offload when it loads rows anyway. Numbers only: no filename leaves this
module except as the match key.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.shared.files.document_tokens import estimate_document_tokens

#: Facts versions this reader understands.
_FACTS_VERSIONS = (1,)

#: Sessions kept in process. A session's entry is a handful of numbers per file.
_MAX_SESSIONS = 512
_MAX_FILES_PER_SESSION = 64


@dataclass(frozen=True)
class DocumentTokenFacts:
    """A document's token facts, with the fields a block is matched on.

    The attribute names are the ones ``FileMetadata`` uses (``upload_id``,
    ``filename``, ``size_bytes``, ``file_format``), so the restore path's
    ``match_document`` matches a block to these exactly as it does to rows.
    """

    upload_id: str
    filename: str
    size_bytes: int
    file_format: str
    text_tokens: int
    native_tokens: int
    pages: Optional[int] = None


def _int(value: Any) -> Optional[int]:
    """DynamoDB returns numbers as ``Decimal``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def token_facts(meta: Any) -> Optional[DocumentTokenFacts]:
    """The token facts on a file row, or ``None`` when it carries none this reader knows."""
    facts = getattr(meta, "facts", None)
    if not isinstance(facts, dict) or _int(facts.get("version")) not in _FACTS_VERSIONS:
        return None
    tokens = facts.get("tokens")
    if not isinstance(tokens, dict):
        return None
    text = _int(tokens.get("text"))
    native = _int(tokens.get("native"))
    upload_id = getattr(meta, "upload_id", None)
    if text is None or native is None or not upload_id:
        return None
    try:
        fmt = str(getattr(meta, "file_format", None) or "").lower()
    except Exception:  # noqa: BLE001 - a row with an odd MIME type has no format
        fmt = ""
    return DocumentTokenFacts(
        upload_id=str(upload_id),
        filename=str(getattr(meta, "filename", "") or ""),
        size_bytes=_int(getattr(meta, "size_bytes", 0)) or 0,
        file_format=fmt,
        text_tokens=max(0, text),
        native_tokens=max(0, native),
        pages=_int(facts.get("pages")),
    )


def facts_tokens(
    facts: DocumentTokenFacts,
    *,
    pages_covered: Optional[int] = None,
    as_text: bool = False,
) -> int:
    """Estimated tokens of ``facts``'s document (see the module docstring).

    ``pages_covered`` is the page count of a ``document_read`` slice; the
    estimate is that share of the whole document.
    """
    if facts.file_format == "pdf" and not as_text:
        whole = facts.text_tokens + facts.native_tokens
    else:
        whole = facts.text_tokens
    if pages_covered is not None and facts.pages and facts.pages > 0:
        covered = max(0, min(int(pages_covered), facts.pages))
        return int(round(whole * covered / facts.pages))
    return int(whole)


def document_tokens(
    fmt: Any,
    raw: Any,
    facts: Optional[DocumentTokenFacts] = None,
    *,
    pages_covered: Optional[int] = None,
    as_text: bool = False,
) -> int:
    """The estimate for one document block: its row's facts when known, else the bytes.

    Never raises. With no facts this is ``estimate_document_tokens(fmt, raw)``,
    which parses a PDF's page count (a slice's own pages, for a slice).
    """
    if facts is not None:
        return facts_tokens(facts, pages_covered=pages_covered, as_text=as_text)
    try:
        return int(estimate_document_tokens(fmt, raw))
    except Exception:  # noqa: BLE001 - estimator must never raise
        return 0


# ---------------------------------------------------------------------------
# In-process facts, by (user, session)
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_SESSIONS: "OrderedDict[Tuple[str, str], Dict[str, DocumentTokenFacts]]" = OrderedDict()


def remember_session_documents(user_id: Optional[str], session_id: Optional[str], rows: Iterable[Any]) -> int:
    """Keep the token facts of ``rows`` (file rows) for ``(user, session)``.

    Rows without facts are skipped, so a row read before its facts landed never
    replaces facts read later. Returns how many were kept. Never raises.
    """
    if not user_id or not session_id:
        return 0
    kept: List[DocumentTokenFacts] = []
    try:
        for row in rows or ():
            if getattr(row, "user_id", user_id) not in (None, user_id):
                continue
            item = token_facts(row)
            if item is not None:
                kept.append(item)
    except Exception:  # noqa: BLE001
        return 0
    if not kept:
        return 0
    key = (str(user_id), str(session_id))
    with _LOCK:
        entry = _SESSIONS.pop(key, None) or {}
        for item in kept:
            entry.pop(item.upload_id, None)
            entry[item.upload_id] = item
        while len(entry) > _MAX_FILES_PER_SESSION:
            entry.pop(next(iter(entry)))
        _SESSIONS[key] = entry
        while len(_SESSIONS) > _MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    return len(kept)


def session_document_facts(user_id: Optional[str], session_id: Optional[str]) -> List[DocumentTokenFacts]:
    """The facts kept for ``(user, session)``, newest first."""
    if not user_id or not session_id:
        return []
    with _LOCK:
        entry = _SESSIONS.get((str(user_id), str(session_id)))
        return list(reversed(list(entry.values()))) if entry else []


def clear_session_documents() -> None:
    """Forget every session's facts (tests)."""
    with _LOCK:
        _SESSIONS.clear()


__all__ = [
    "DocumentTokenFacts",
    "clear_session_documents",
    "document_tokens",
    "facts_tokens",
    "remember_session_documents",
    "session_document_facts",
    "token_facts",
]
