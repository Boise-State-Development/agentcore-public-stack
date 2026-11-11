"""Document handles in place of bytes in the message persisted to AgentCore Memory.

``docs/specs/unified-attachment-delivery.md`` §6.2 (phase 4a). While the
``token`` limits profile is active (``persist_document_handles``), the user
message written to Memory carries, for each attached document, a handle in the
block's ``source``::

    {"document": {"format": "pdf", "name": "report_pdf",
                  "source": {"attachmentHandle": {"version": 1, "uploadId": "...",
                                                  "sizeBytes": 3145728, "class": "pdf"}}}}

instead of ``{"bytes": ...}``. The live ``agent.messages`` keeps the bytes, so a
warm agent sends exactly what it sent before.

**Why restore is unchanged.** Restore never sends a persisted document's bytes
to the model: ``rehydrate_documents`` replaces every attached document with its
digest (matched to the session's upload rows on the block's name, format and
byte size) or, unmatched, a placeholder naming the same three. The handle keeps
those three facts, so restore matches the same row and renders the same bytes.
The only reader of the bytes was the lazy digest, for a row with no digest yet;
it now reads the row's stored S3 object, the bytes the block was built from.

**What keeps its bytes.** ``document_read`` slices inside tool results are
re-sent on restore and bounded by the tool's cap, so they are persisted as
before. Images are phase 4's second half (they are re-sent on restore and need
the stored, downscaled object).

**Old messages.** A message persisted with bytes still restores the old way; the
reader accepts both shapes for as long as Memory keeps such events (§9.1).

Numbers and ids only. No I/O here.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: The ``document.source`` key that marks a handle.
HANDLE_KEY = "attachmentHandle"
HANDLE_VERSION = 1

#: Sessions kept in process; a session's entry is a few ids per file.
_MAX_SESSIONS = 512
_MAX_FILES_PER_SESSION = 64


@dataclass(frozen=True)
class UploadRef:
    """An upload a turn attached, with the attribute names ``FileMetadata`` uses,
    so the restore path's ``match_document`` matches a block to it."""

    upload_id: str
    filename: str
    size_bytes: int
    file_format: str


def handle_of(document: Any) -> Optional[Dict[str, Any]]:
    """The handle on a ``document`` payload, or ``None`` for bytes or anything else."""
    if not isinstance(document, dict):
        return None
    source = document.get("source")
    handle = source.get(HANDLE_KEY) if isinstance(source, dict) else None
    return handle if isinstance(handle, dict) else None


def handle_size(handle: Dict[str, Any]) -> int:
    """The document's byte size, as the bytes it replaced would have measured."""
    try:
        return max(0, int(handle.get("sizeBytes") or 0))
    except (TypeError, ValueError):
        return 0


def document_class(fmt: str) -> str:
    """``pdf`` or ``document``: the planner's class for a document block."""
    return "pdf" if (fmt or "").lower() == "pdf" else "document"


def handle_block(document: Dict[str, Any], upload_id: Optional[str]) -> Dict[str, Any]:
    """The persisted form of one attached ``document`` payload that holds bytes."""
    raw = (document.get("source") or {}).get("bytes")
    fmt = str(document.get("format") or "")
    handle: Dict[str, Any] = {
        "version": HANDLE_VERSION,
        "sizeBytes": len(raw) if isinstance(raw, (bytes, bytearray)) else 0,
        "class": document_class(fmt),
    }
    if upload_id:
        handle["uploadId"] = str(upload_id)
    return {"document": {"format": document.get("format"), "name": document.get("name"), "source": {HANDLE_KEY: handle}}}


def persisted_message(
    message: Dict[str, Any],
    resolve: Callable[[str, str, int], Optional[str]],
) -> Dict[str, Any]:
    """``message`` as it is written to Memory: each attached document's bytes
    replaced by a handle. Never mutates ``message``; returns it unchanged when
    nothing was replaced.

    ``resolve(name, format, size)`` names the upload id a block came from, or
    ``None`` (a direct attachment with no row; restore then writes the same
    placeholder it writes today). Only top-level blocks of a user message are
    attachments; tool results are left alone.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return message
    content = message.get("content")
    if not isinstance(content, list):
        return message
    out: Optional[List[Any]] = None
    for i, block in enumerate(content):
        if not isinstance(block, dict) or set(block) != {"document"}:
            continue
        document = block.get("document")
        source = document.get("source") if isinstance(document, dict) else None
        raw = source.get("bytes") if isinstance(source, dict) else None
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            upload_id = resolve(str(document.get("name", "")), str(document.get("format") or "").lower(), len(raw))
        except Exception:  # noqa: BLE001 - an unresolved handle restores like an unmatched block
            upload_id = None
        if out is None:
            out = list(content)
        out[i] = handle_block(document, upload_id)
    if out is None:
        return message
    return {**message, "content": out}


def strip_handles_for_display(document: Dict[str, Any]) -> Dict[str, Any]:
    """A handle as the message API returns it: the name and format clients read."""
    return {"format": document.get("format") or "txt", "name": document.get("name") or "document"}


# ---------------------------------------------------------------------------
# The uploads a session's turns attached, by (user, session)
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_SESSIONS: "OrderedDict[Tuple[str, str], Dict[str, UploadRef]]" = OrderedDict()


def remember_session_uploads(user_id: Optional[str], session_id: Optional[str], rows: Iterable[Any]) -> int:
    """Keep the ids of ``rows`` (file rows the turn already resolved) for
    ``(user, session)``, so the persisted message can name them. Never raises."""
    if not user_id or not session_id:
        return 0
    kept: List[UploadRef] = []
    try:
        for row in rows or ():
            if row is None or getattr(row, "user_id", user_id) not in (None, user_id):
                continue
            upload_id = getattr(row, "upload_id", None)
            if not upload_id:
                continue
            try:
                fmt = str(getattr(row, "file_format", None) or "").lower()
            except Exception:  # noqa: BLE001 - an odd MIME type has no format
                fmt = ""
            kept.append(
                UploadRef(
                    upload_id=str(upload_id),
                    filename=str(getattr(row, "filename", "") or ""),
                    size_bytes=int(getattr(row, "size_bytes", 0) or 0),
                    file_format=fmt,
                )
            )
    except Exception:  # noqa: BLE001
        return 0
    if not kept:
        return 0
    key = (str(user_id), str(session_id))
    with _LOCK:
        entry = _SESSIONS.pop(key, None) or {}
        for ref in kept:
            entry.pop(ref.upload_id, None)
            entry[ref.upload_id] = ref
        while len(entry) > _MAX_FILES_PER_SESSION:
            entry.pop(next(iter(entry)))
        _SESSIONS[key] = entry
        while len(_SESSIONS) > _MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    return len(kept)


def session_uploads(user_id: Optional[str], session_id: Optional[str]) -> List[UploadRef]:
    """The uploads kept for ``(user, session)``, newest first."""
    if not user_id or not session_id:
        return []
    with _LOCK:
        entry = _SESSIONS.get((str(user_id), str(session_id)))
        return list(reversed(list(entry.values()))) if entry else []


def clear_session_uploads() -> None:
    """Forget every session's uploads (tests)."""
    with _LOCK:
        _SESSIONS.clear()


__all__ = [
    "HANDLE_KEY",
    "HANDLE_VERSION",
    "UploadRef",
    "clear_session_uploads",
    "document_class",
    "handle_block",
    "handle_of",
    "handle_size",
    "persisted_message",
    "remember_session_uploads",
    "session_uploads",
    "strip_handles_for_display",
]
