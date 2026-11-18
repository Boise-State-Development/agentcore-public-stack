"""A skill file's text taken from a file attached to the conversation (skill-creator spec,
"Writing a file from an attachment").

``my_skill_write_file(from_attachment=…)`` names an upload instead of retyping it, and
the approval card describes the same text before the user decides. Both call
:func:`resolve_attachment_text`, so the card's hash is the hash of what the tool writes.

* **Whose files.** Only a ``READY`` row in this conversation's file index whose owner
  is the caller resolves: the ``(session, user)`` pair, never the session alone. A
  shared file (a handoff reference) reads through to its original.
* **What text.** A ``.docx`` is rendered by ``docx_markdown`` (the documents-as-text
  renderer, deterministic); a text file is decoded as UTF-8 and kept unchanged, so the
  stored bytes are the uploaded bytes. Anything else is refused.

Every failure is an :class:`AttachmentSourceError` whose message is written for the
model to read and act on. The functions are blocking (boto3 under async signatures),
so callers run them off the event loop.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional

from apis.shared.files.models import FileMetadata, FileStatus

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})

#: The largest ``.docx`` read (``document_read``'s own source cap). The rendered text
#: is then held to the bundle's 1 MiB file cap like any other write.
MAX_DOCX_SOURCE_BYTES = 8 * 1024 * 1024


class AttachmentSourceError(Exception):
    """The attachment cannot be used; the message says why and what to do."""


@dataclass(frozen=True)
class AttachmentText:
    """The text a write from an attachment stores, and where it came from."""

    upload_id: str
    filename: str
    source_format: str  # "docx" or "text"
    source_bytes: int
    text: str

    @property
    def converted(self) -> bool:
        return self.source_format == "docx"

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _extension(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _source_format(meta: FileMetadata) -> Optional[str]:
    from apis.shared.files.workspace import is_text_mime

    mime = (meta.mime_type or "").lower().split(";")[0].strip()
    if mime == _DOCX_MIME or _extension(meta.filename) == ".docx":
        return "docx"
    if _extension(meta.filename) in _TEXT_EXTENSIONS or is_text_mime(mime):
        return "text"
    return None


def _is_ready(meta: FileMetadata) -> bool:
    status = meta.status if isinstance(meta.status, str) else meta.status.value
    return status == FileStatus.READY.value


def _session_files(user_id: str, session_id: str) -> List[FileMetadata]:
    from apis.shared.files.repository import get_file_upload_repository

    rows = get_file_upload_repository().list_session_files_sync(session_id, FileStatus.READY)
    # The session index is keyed by conversation; the owner check is ours to make.
    return [m for m in rows if m.user_id == user_id and _is_ready(m)]


def _find(files: List[FileMetadata], ref: str) -> FileMetadata:
    by_id = [m for m in files if m.upload_id == ref]
    if by_id:
        return by_id[0]
    by_name = [m for m in files if m.filename == ref] or [
        m for m in files if m.filename.lower() == ref.lower()
    ]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        ids = ", ".join(f"{m.upload_id} ({m.size_bytes:,} bytes)" for m in by_name)
        raise AttachmentSourceError(
            f"More than one file in this conversation is named '{ref}': {ids}. "
            "Pass the upload id of the one to use."
        )
    names = ", ".join(sorted({m.filename for m in files})) or "none"
    raise AttachmentSourceError(
        f"No file named or with the id '{ref}' is attached to this conversation "
        f"(attached: {names}). Ask the user to attach it, or pass content instead."
    )


def _read_through(meta: FileMetadata) -> FileMetadata:
    from apis.shared.files.linked_files import linked_file_gone_message, readable_view

    from .tools import _run

    view = _run(readable_view(meta, reader="my_skill_write_file"))
    if view is None:
        raise AttachmentSourceError(f"'{meta.filename}': {linked_file_gone_message(meta)}.")
    return view


def _fetch(meta: FileMetadata) -> bytes:
    from apis.shared.files.workspace import _s3

    return _s3().get_object(Bucket=meta.s3_bucket, Key=meta.s3_key)["Body"].read()


def resolve_attachment_text(
    user_id: str, session_id: str, ref: str, *, max_bytes: int
) -> AttachmentText:
    """The text ``ref`` (an upload id or filename in this conversation) would write.

    ``max_bytes`` is the bundle's per-file cap, checked on the text that would be
    stored. Raises :class:`AttachmentSourceError`.
    """
    ref = (ref or "").strip()
    if not user_id or not session_id:
        raise AttachmentSourceError("This conversation has no attachments to read from.")
    meta = _find(_session_files(user_id, session_id), ref)
    fmt = _source_format(meta)
    if fmt is None:
        raise AttachmentSourceError(
            f"'{meta.filename}' is a {meta.mime_type or 'binary'} file. Only text files "
            "(.md, .txt) and Word documents (.docx) can be written into a skill file; "
            "ask the user for one of those."
        )
    view = _read_through(meta)
    source_cap = MAX_DOCX_SOURCE_BYTES if fmt == "docx" else max_bytes
    if view.size_bytes > source_cap:
        raise AttachmentSourceError(
            f"'{meta.filename}' is {view.size_bytes:,} bytes and the limit for this kind "
            f"of source is {source_cap:,}. Split it into smaller files."
        )
    raw = _fetch(view)
    if fmt == "docx":
        from apis.shared.files.docx_markdown import DocxRenderError, docx_to_markdown

        try:
            text = docx_to_markdown(raw)
        except DocxRenderError as exc:
            raise AttachmentSourceError(f"'{meta.filename}' could not be read: {exc}") from exc
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttachmentSourceError(
                f"'{meta.filename}' is not valid UTF-8 text, so it cannot be copied as is."
            ) from exc
    result = AttachmentText(
        upload_id=meta.upload_id,
        filename=meta.filename,
        source_format=fmt,
        source_bytes=view.size_bytes,
        text=text,
    )
    if result.size > max_bytes:
        raise AttachmentSourceError(
            f"'{meta.filename}' gives {result.size:,} bytes of text and the limit for a "
            f"skill file is {max_bytes:,}. Split it into smaller files."
        )
    return result


@dataclass(frozen=True)
class LineChange:
    added: int
    removed: int
    diff: str


def line_change(old: str, new: str, *, filename: str) -> LineChange:
    """Lines added and removed from ``old`` to ``new``, with a unified diff."""
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{filename} (current)",
            tofile=f"{filename} (new)",
            lineterm="",
            n=2,
        )
    )
    body = [line for line in lines if not line.startswith(("---", "+++"))]
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    return LineChange(added=added, removed=removed, diff="\n".join(lines))
