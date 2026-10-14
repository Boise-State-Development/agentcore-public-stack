"""The folder manifest: what a turn tells the model about an attached folder.

``docs/specs/drive-folder-attachments.md`` §4.4. A folder rides a turn as one
``<folder>`` block in the user message, listing the files attached so far with
the ``upload_id`` each is read by. No file content is in it: documents are read
through ``document_read``, spreadsheets and decks through their own tools.

**Prompt-cache contract (the large-document spec's §4B.5, applied to folders).**
The block is rendered once, into the persisted user message, and is a pure
function of the row as read on that turn. A folder still expanding renders what
is attached so far; a later turn that attaches the folder again renders the
newer row in *its* message, and the earlier message is never rewritten. Entries
keep the row's order (the walk's listing order), so the same row always gives
the same bytes.

**Bounded.** At most ``FOLDER_MANIFEST_MAX_ENTRIES`` lines per folder, then a
count of the rest; ``document_read`` with no arguments lists every file.

Folder and file names are the user's own. They reach the model (that is the
point) but never a log line or a metric: :func:`record_folder_manifests` emits
counts only.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Sequence

from apis.shared.files.folder_attachments import (
    ENTRY_ATTACHED,
    FOLDER_MANIFEST_MAX_ENTRIES,
    FOLDER_STATUS_READY,
    FolderAttachment,
    FolderFileEntry,
)
from apis.shared.files.models import get_file_format

logger = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")


def _clean(text: str) -> str:
    """One line of the user's text: control characters and line breaks become a
    space, so a file name can never open a new line in the block."""
    return _CONTROL.sub(" ", text or "").strip()


def _body(text: str) -> str:
    return _clean(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(text: str) -> str:
    return _body(text).replace('"', "&quot;")


def human_size(size_bytes: Optional[int]) -> str:
    """``812 B`` / ``84 KB`` / ``1.2 MB``: whole KB below a megabyte, one decimal above."""
    if size_bytes is None:
        return "size unknown"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{round(size_bytes / 1024)} KB"
    mb = round(size_bytes / (1024 * 1024), 1)
    return f"{mb:.1f}".removesuffix(".0") + " MB"


def entry_format(entry: FolderFileEntry) -> str:
    """The short type a model reads the entry by: ``pdf``, ``docx``, ``xlsx``…"""
    fmt = get_file_format((entry.mime_type or "").lower().split(";")[0].strip())
    if fmt:
        return fmt
    _, dot, ext = (entry.name or "").rpartition(".")
    return ext.lower() if dot and ext else "file"


def attached_entries(folder: FolderAttachment) -> List[FolderFileEntry]:
    """The entries the model can read, in the row's (listing) order."""
    return [e for e in folder.files if e.status == ENTRY_ATTACHED and e.upload_id]


def render_folder_manifest(folder: FolderAttachment, *, include_handle: bool) -> str:
    """One folder's block. Pure: the same row renders the same text.

    ``include_handle=False`` omits each ``upload_id`` when this turn does not
    carry ``document_read``, for the same reason the reference stub does:
    advertising a handle for a tool the model does not have invites a call
    that cannot be made.
    """
    entries = attached_entries(folder)
    counts = folder.counts
    skipped = counts.skipped_type + counts.skipped_size + counts.failed
    attrs = [
        f'name="{_attr(folder.folder_name)}"',
        f'source="{_attr(folder.connector_name or folder.connector_id)}"',
        f'status="{_attr(folder.status)}"',
        f'files="{len(entries)}"',
    ]
    if folder.status != FOLDER_STATUS_READY:
        attrs.append(f'listed="{counts.listed}"')
    if skipped:
        attrs.append(f'skipped="{skipped}"')
    if counts.truncated:
        attrs.append('truncated="true"')
    if folder.error:
        attrs.append(f'error="{_attr(folder.error)}"')
    head = "<folder " + " ".join(attrs)
    if not entries:
        return head + "/>"

    lines = [head + ">"]
    for entry in entries[:FOLDER_MANIFEST_MAX_ENTRIES]:
        parts = [_body(entry.relative_path or entry.name)]
        if include_handle:
            parts.append(f"upload_id={_body(entry.upload_id or '')}")
        parts.append(human_size(entry.size_bytes))
        parts.append(entry_format(entry))
        lines.append("  " + " — ".join(parts))
    more = len(entries) - FOLDER_MANIFEST_MAX_ENTRIES
    if more > 0:
        tail = f"  … and {more} more"
        if include_handle:
            tail += "; call `document_read` with no arguments to list them all"
        lines.append(tail)
    lines.append("</folder>")
    return "\n".join(lines)


def folder_guidance(
    folders: Sequence[FolderAttachment],
    *,
    document_read_available: bool,
    spreadsheet_tools: bool,
    presentation_tools: bool,
) -> str:
    """The one line after the blocks that tells the model how to read them.

    Directive on purpose, as the reference guidance is: on dev, Haiku 4.5
    answered "I don't see that file" for a non-inline document until told to
    call the tool by name.
    """
    if not folders:
        return ""
    entries = [e for f in folders for e in attached_entries(f)]
    if not entries:
        return (
            "_The attached folder has no readable files yet (still being added, or none could be read). "
            "Tell the user what its status says rather than guessing at its contents._"
        )
    from apis.shared.files.document_read import is_document_class
    from apis.shared.files.models import is_presentation_file, is_tabular_file

    has_docs = any(is_document_class(e.mime_type, e.name) for e in entries)
    has_sheets = any(is_tabular_file(e.name, e.mime_type) for e in entries)
    has_decks = any(is_presentation_file(e.name, e.mime_type) for e in entries)

    whose = "these folders'" if len(folders) > 1 else "the folder's"
    parts = [f"_{whose[0].upper()}{whose[1:]} files are attached, but their content is not in this message."]
    if has_docs:
        if document_read_available:
            parts.append(
                "To answer from a document in it, call `document_read` with that file's `upload_id`: "
                "first with `pattern` to find the relevant passages or pages, then read them "
                "(`page_range` for a PDF). To find which file holds something, use `pattern` on the likeliest files."
            )
        else:
            parts.append("Reading its documents is not available in this conversation.")
    if has_sheets and spreadsheet_tools:
        parts.append("Read its spreadsheets with `list_spreadsheets` and `analyze_spreadsheet`.")
    if has_decks and presentation_tools:
        parts.append("Read its presentations with `read_powerpoint_presentation`.")
    return " ".join(parts) + "_"


def render_folders_section(
    folders: Sequence[FolderAttachment],
    *,
    document_read_available: bool,
    spreadsheet_tools: bool = False,
    presentation_tools: bool = False,
) -> str:
    """Every folder's block, in request order, then the guidance line."""
    if not folders:
        return ""
    blocks = [render_folder_manifest(f, include_handle=document_read_available) for f in folders]
    guidance = folder_guidance(
        folders,
        document_read_available=document_read_available,
        spreadsheet_tools=spreadsheet_tools,
        presentation_tools=presentation_tools,
    )
    return "\n\n".join(blocks + ([guidance] if guidance else []))


def record_folder_manifests(folders: Sequence[FolderAttachment], rendered: str) -> None:
    """One content-free ``FolderManifest`` EMF record per folder on a turn (spec §6).

    In ``AgentCoreStack/Compaction`` beside ``AttachmentReference``: the folder's
    status, how many entries it lists, whether the list was cut, and the
    section's character count (a proxy for its tokens). Never a name, a path or
    an id. Never raises.
    """
    if not folders:
        return
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        chars = len(rendered)
        for folder in folders:
            listed = len(attached_entries(folder))
            emit_emf_metrics(
                "AgentCoreStack/Compaction",
                metrics={
                    "FolderManifest": 1,
                    "FolderManifestEntries": min(listed, FOLDER_MANIFEST_MAX_ENTRIES),
                    "FolderManifestChars": chars,
                },
                properties={
                    "status": folder.status,
                    "cut": "true" if listed > FOLDER_MANIFEST_MAX_ENTRIES else "false",
                },
                units={"FolderManifest": "Count", "FolderManifestEntries": "Count", "FolderManifestChars": "Count"},
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("FolderManifest EMF skipped: %s", e)
