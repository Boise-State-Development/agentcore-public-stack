"""Tell a project turn's model which project files it cannot see yet.

Retrieval and full-context mode both read a document only once its ``DOC#`` row
is ``complete``, so a file still being prepared, or one that failed, is simply
absent from the turn. The model then answers as though the project had no such
file. On prod (2026-11-03) a project's 11 files were all still ``provisioning``
when its owner asked for a policy drafted from them; the assistant only found
out because it happened to call ``project_files``, and a turn where it does not
would have drafted from nothing without saying so.

:func:`load_file_status_note` returns one short ``<project_files_status>`` block
naming those files, or ``None`` when every file is ready. The route appends it to
the persisted user message, the same channel as ``<project_member>``: never the
system prompt or ``toolConfig``, so no cached prefix moves, and a turn whose
files are all ready is byte-for-byte what it was without this module. The note
is a record of that turn, so a later turn carries its own (or none, once the
files are ready and arrive through retrieval or full context).

Bounded: at most :data:`MAX_LISTED_NAMES` names per group, each cut to
:data:`MAX_NAME_CHARS`, and the rest counted.

Scope is the retrieval path's: the caller's grant must name this assistant
before anything is read, and any failure returns ``None`` rather than raising,
because a missing status line is a worse answer but never a broken turn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, List, Mapping, Optional

from apis.shared.assistants.kb_access import KbAccess
from apis.shared.assistants.kb_documents import list_kb_documents
from apis.shared.feature_flags import project_file_readiness_enabled

logger = logging.getLogger(__name__)

#: ``DOC#`` statuses of a file that is on its way to ``complete``. ``deleting``
#: is neither pending nor failed: the user removed it, so it is not mentioned.
PENDING_STATUSES = frozenset({"provisioning", "uploading", "chunking", "embedding"})
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

#: Names listed per group before the rest are counted ("and 4 more").
MAX_LISTED_NAMES = 10

#: Longest file name written into the note.
MAX_NAME_CHARS = 120


def _name(doc: Mapping[str, Any]) -> str:
    """A file's name on one line, unable to open or close a tag."""
    raw = str(doc.get("filename") or doc.get("documentId") or "unnamed file")
    plain = " ".join(raw.split()).replace("<", "").replace(">", "")
    return plain[:MAX_NAME_CHARS]


def _names(docs: List[Mapping[str, Any]]) -> str:
    listed = [_name(doc) for doc in docs[:MAX_LISTED_NAMES]]
    extra = len(docs) - len(listed)
    text = ", ".join(listed)
    return f"{text}, and {extra} more" if extra > 0 else text


def _files(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def render_file_status_note(documents: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """The note for these ``DOC#`` rows, or ``None`` when none is pending or failed.

    Pure, so the wording is testable without a table. Files are listed in upload
    order, the order the Files page shows.
    """
    rows = sorted(
        (d for d in documents if str(d.get("status") or "") != "deleting"),
        key=lambda d: (str(d.get("createdAt") or ""), str(d.get("documentId") or "")),
    )
    pending = [d for d in rows if str(d.get("status") or "") in PENDING_STATUSES]
    failed = [d for d in rows if str(d.get("status") or "") == STATUS_FAILED]
    if not pending and not failed:
        return None

    total = len(rows)
    lines: List[str] = []
    if pending:
        verb = "is" if len(pending) == 1 else "are"
        lines.append(
            f"{_files(len(pending))} of the project's {total} {verb} still being prepared and "
            f"cannot be searched or read yet: {_names(pending)}. Files are usually ready within "
            f"a few minutes of upload."
        )
    if failed:
        verb = "was" if len(failed) == 1 else "were"
        lines.append(
            f"{_files(len(failed))} {verb} not added and will not become available: "
            f"{_names(failed)}. The person can upload "
            f"{'it' if len(failed) == 1 else 'them'} again from the project's Files page."
        )
    lines.append(
        "Answer from the files that are available. If the request depends on one listed "
        "here, say which, and do not present an answer as drawing on it. Do not call "
        "project_files to read a listed file."
        if pending
        else "Answer from the files that are available, and say so if the request depends on "
        "one listed here."
    )
    return "<project_files_status>\n" + "\n".join(lines) + "\n</project_files_status>"


async def load_file_status_note(assistant_id: str, *, access: Optional[KbAccess]) -> Optional[str]:
    """This turn's note for ``assistant_id``'s files, or ``None``. Never raises.

    ``access`` is required and keyword-only, as for the retrieval path: a missing
    or mismatched grant returns ``None`` before anything is read.
    """
    if not project_file_readiness_enabled():
        return None
    if access is None or not access.may_read or access.assistant_id != assistant_id:
        return None
    try:
        documents = await asyncio.to_thread(list_kb_documents, assistant_id)
    except Exception:
        logger.warning("kb_readiness assistant=%s listing failed; no status note", assistant_id, exc_info=True)
        return None
    note = render_file_status_note(documents)
    if note:
        logger.info("kb_readiness assistant=%s note=on docs=%d", assistant_id, len(documents))
    return note
