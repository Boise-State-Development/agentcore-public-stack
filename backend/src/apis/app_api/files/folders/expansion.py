"""The expansion task: walk an attached folder and import its readable files.

``docs/specs/drive-folder-attachments.md`` §4.2, §4.3, §4.5, §4.9. Runs in
app-api as a fire-and-forget task (the KB import pattern,
``documents/services/import_service.py``) with the user's OAuth token held in
memory for its lifetime and never logged.

Two phases. The **walk** lists the folder breadth-first through the adapter's
``browse``, following subfolders and tracking each file's path from the root,
until ``FOLDER_ATTACH_MAX_FILES`` entries are listed or the tree is exhausted.
A file is attachable when the type its bytes arrive as (an export's type for
a native document) is in the chat allowlist and has an on-demand reader:
document class (``document_read``), tabular (``analyze_spreadsheet``) or a
deck (``read_powerpoint_presentation``). Images have no reader and are
counted ``skipped_type``, never fetched. The **download** phase then imports
each listed file in listing order, at most ``FOLDER_ATTACH_CONCURRENCY`` at a
time, through ``FileUploadService.import_file`` with ``delivery="reference"``
for document class (prepared and read through sidecars whatever its size)
and ``inline`` for spreadsheets and decks (their tools read the original).

Fail-open throughout. One file's failure marks that entry and continues; the
first quota refusal stops the task and leaves the rest ``pending`` for
``resume``; a token the provider rejects ends the row ``failed`` with
``needs_consent``. Progress reaches the row every few files so the card
moves, and a row the user removed mid-task ends the task at its next write.

Never raises: the task's outcome is the row's status.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Set, Tuple

from apis.shared.feature_flags import large_document_attachments_enabled
from apis.shared.files.document_read import is_document_class
from apis.shared.files.folder_attachments import (
    ENTRY_ATTACHED,
    ENTRY_FAILED,
    ENTRY_PENDING,
    ENTRY_REMOVED,
    ENTRY_SKIPPED_SIZE,
    ERROR_LISTING_FAILED,
    ERROR_NEEDS_CONSENT,
    ERROR_QUOTA_EXCEEDED,
    FOLDER_ATTACH_CONCURRENCY,
    FOLDER_ATTACH_MAX_BYTES,
    FOLDER_ATTACH_MAX_FILES,
    FOLDER_ATTACH_MAX_PAGES,
    FOLDER_PROGRESS_EVERY,
    FOLDER_STATUS_FAILED,
    FOLDER_STATUS_PARTIAL,
    FOLDER_STATUS_READY,
    FolderAttachment,
    FolderCounts,
    FolderFileEntry,
    RefreshSummary,
    record_folder_expansion,
    record_folder_refresh,
)
from apis.shared.files.folder_repository import FolderAttachmentRepository
from apis.shared.files.models import (
    ALLOWED_EXTENSIONS,
    DELIVERY_INLINE,
    DELIVERY_REFERENCE,
    is_allowed_mime_type,
)
from apis.shared.timestamps import utc_now_iso

from apis.app_api.file_sources.adapter import FileSourceAdapter
from apis.app_api.file_sources.models import (
    FileEntry,
    FileEntryType,
    FileSourceAuthError,
    FileSourceError,
    FileSourceTooLargeError,
)
from apis.app_api.files.service import (
    FileNotFoundError as UploadNotFoundError,
    FileTooLargeError,
    FileUploadService,
    InvalidFileTypeError,
    QuotaExceededError,
)

logger = logging.getLogger(__name__)


def folder_file_source(connector_id: str, folder_attach_id: str) -> str:
    """The ``source`` written on a folder file's row: ``connector:{id}:folder:{fid}``."""
    return f"connector:{connector_id}:folder:{folder_attach_id}"


def attachable_mime_for(entry: FileEntry) -> Optional[str]:
    """The accepted MIME type a browse entry downloads as, or ``None``.

    The composer's rule server-side, narrowed to what has an on-demand reader:
    the downloaded type (``download_mime_type`` for an export, else the
    provider's) must be in the chat allowlist, resolved from the name's
    extension when the provider reported none, and not an image. Everything
    else in the allowlist is document class, tabular or a deck, and each has
    a tool that reads it on demand.
    """
    if entry.type != FileEntryType.FILE or not entry.selectable:
        return None
    mime = (entry.download_mime_type or entry.mime_type or "").lower().split(";")[0].strip()
    resolved: Optional[str] = mime if is_allowed_mime_type(mime) else None
    if resolved is None:
        lower = (entry.name or "").lower()
        for ext, candidate in ALLOWED_EXTENSIONS.items():
            if lower.endswith(ext):
                resolved = candidate
                break
    if resolved is None or resolved.startswith("image/"):
        return None
    return resolved


def delivery_for(filename: str, mime_type: str) -> str:
    """``reference`` for a document-class file while the prepare pipeline is
    on (spec §4.3), ``inline`` otherwise. Spreadsheets and decks are never
    prepared: their tools read the original on demand."""
    if large_document_attachments_enabled() and is_document_class(mime_type, filename):
        return DELIVERY_REFERENCE
    return DELIVERY_INLINE


@dataclass
class ExpansionDeps:
    """What one expansion needs. ``access_token`` lives here and nowhere else."""

    adapter: FileSourceAdapter
    access_token: str
    repository: FolderAttachmentRepository
    upload_service: FileUploadService
    max_files: int = FOLDER_ATTACH_MAX_FILES
    max_bytes: int = FOLDER_ATTACH_MAX_BYTES
    concurrency: int = FOLDER_ATTACH_CONCURRENCY
    max_pages: int = FOLDER_ATTACH_MAX_PAGES
    progress_every: int = FOLDER_PROGRESS_EVERY

    def __repr__(self) -> str:  # never show the token, even by accident
        return f"ExpansionDeps(adapter={self.adapter.metadata.key!r}, max_files={self.max_files})"


@dataclass
class _Walk:
    """The listing phase's result."""

    entries: List[FolderFileEntry] = field(default_factory=list)
    listed: int = 0
    skipped_type: int = 0
    truncated: bool = False
    error: Optional[str] = None


@dataclass
class _Progress:
    """Mutable state shared by the download tasks."""

    entries: List[FolderFileEntry]
    listed: int
    skipped_type: int
    truncated: bool
    bytes_attached: int = 0
    completed: int = 0
    stop_reason: Optional[str] = None
    row_gone: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def counts(self) -> FolderCounts:
        return FolderCounts(
            listed=self.listed,
            attached=sum(1 for e in self.entries if e.status == ENTRY_ATTACHED),
            skipped_type=self.skipped_type,
            skipped_size=sum(1 for e in self.entries if e.status == ENTRY_SKIPPED_SIZE),
            failed=sum(1 for e in self.entries if e.status == ENTRY_FAILED),
            truncated=self.truncated,
        )


async def expand_folder(folder: FolderAttachment, deps: ExpansionDeps) -> Optional[FolderAttachment]:
    """Walk ``folder`` and import its files; returns the row as it ended.

    ``None`` when the row was removed while the task ran. Never raises.
    """
    started = time.monotonic()
    try:
        return await _expand(folder, deps, started)
    except Exception:  # noqa: BLE001 - the task's outcome is the row's status
        logger.error("Folder expansion failed unexpectedly", exc_info=True)
        try:
            await deps.repository.update(
                folder.user_id, folder.folder_attach_id,
                status=FOLDER_STATUS_FAILED, error=ERROR_LISTING_FAILED,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not mark the folder row failed", exc_info=True)
        return None


async def _expand(folder: FolderAttachment, deps: ExpansionDeps, started: float) -> Optional[FolderAttachment]:
    # Resume (spec §4.9): what is already attached is kept and skipped by
    # provider file id, and so is a file the user removed; everything else is
    # listed and downloaded afresh.
    kept = [e for e in folder.files if e.status in (ENTRY_ATTACHED, ENTRY_REMOVED)]
    skip_ids = folder.settled_provider_ids
    attached = sum(1 for e in kept if e.status == ENTRY_ATTACHED)

    walk = await _walk_folder(folder.provider_folder_id, deps, skip_ids, keep=attached)
    progress = _Progress(
        entries=kept + walk.entries,
        listed=walk.listed,
        skipped_type=walk.skipped_type,
        truncated=walk.truncated,
        bytes_attached=sum(e.size_bytes or 0 for e in kept if e.status == ENTRY_ATTACHED),
    )

    if walk.error == ERROR_NEEDS_CONSENT:
        return await _finish(folder, deps, progress, FOLDER_STATUS_FAILED, ERROR_NEEDS_CONSENT, started)

    # The listing reaches the row before any download, so the card can show
    # "Adding 0 of 38" at once and a restart has a list to resume from.
    row = await deps.repository.update(
        folder.user_id, folder.folder_attach_id,
        counts=progress.counts(), files=progress.entries, snapshot_at=utc_now_iso(),
    )
    if row is None:
        return None

    pending = [e for e in progress.entries if e.status == ENTRY_PENDING]
    if pending:
        semaphore = asyncio.Semaphore(max(1, deps.concurrency))
        await asyncio.gather(*(_process_entry(folder, deps, progress, entry, semaphore) for entry in pending))
        if progress.row_gone:
            return None

    if progress.stop_reason == ERROR_NEEDS_CONSENT:
        status, error = FOLDER_STATUS_FAILED, ERROR_NEEDS_CONSENT
    elif progress.stop_reason == ERROR_QUOTA_EXCEEDED:
        status, error = FOLDER_STATUS_PARTIAL, ERROR_QUOTA_EXCEEDED
    elif walk.error == ERROR_LISTING_FAILED:
        status = FOLDER_STATUS_PARTIAL if progress.entries else FOLDER_STATUS_FAILED
        error = ERROR_LISTING_FAILED
    else:
        counts = progress.counts()
        short = counts.failed or counts.skipped_size or counts.skipped_type or counts.truncated
        status, error = (FOLDER_STATUS_PARTIAL if short else FOLDER_STATUS_READY), None
    return await _finish(folder, deps, progress, status, error, started)


async def _finish(
    folder: FolderAttachment,
    deps: ExpansionDeps,
    progress: _Progress,
    status: str,
    error: Optional[str],
    started: float,
) -> Optional[FolderAttachment]:
    counts = progress.counts()
    row = await deps.repository.update(
        folder.user_id, folder.folder_attach_id,
        status=status, counts=counts, files=progress.entries,
        error=error, clear_error=error is None,
    )
    record_folder_expansion(
        outcome=status if error is None else f"{status}:{error}",
        counts=counts,
        bytes_attached=progress.bytes_attached,
        ms=int((time.monotonic() - started) * 1000),
    )
    return row


# ---------------------------------------------------------------------------
# Phase 1: the walk
# ---------------------------------------------------------------------------


async def _walk_folder(root_id: str, deps: ExpansionDeps, skip_ids: Set[str], *, keep: int) -> _Walk:
    """Breadth-first over ``browse``, one page at a time, bounded by the caps."""
    walk = _Walk()
    queue: Deque[Tuple[str, str]] = deque([(root_id, "")])
    pages = 0
    room = deps.max_files - keep

    while queue:
        folder_id, prefix = queue.popleft()
        cursor: Optional[str] = None
        while True:
            if pages >= deps.max_pages:
                walk.truncated = True
                return walk
            try:
                page = await deps.adapter.browse(deps.access_token, folder_id, cursor)
            except FileSourceAuthError:
                walk.error = ERROR_NEEDS_CONSENT
                return walk
            except FileSourceError as e:
                logger.warning("Folder listing failed: %s", type(e).__name__)
                walk.error = ERROR_LISTING_FAILED
                return walk
            pages += 1

            for entry in page.entries:
                if entry.type == FileEntryType.FOLDER:
                    queue.append((entry.id, f"{prefix}{entry.name}/"))
                    continue
                walk.listed += 1
                if entry.id in skip_ids:
                    continue
                mime = attachable_mime_for(entry)
                if mime is None:
                    walk.skipped_type += 1
                    continue
                if len(walk.entries) >= room:
                    walk.truncated = True
                    return walk
                walk.entries.append(_new_entry(entry, prefix, mime, deps))

            cursor = page.next_cursor
            if not cursor:
                break
    return walk


def _new_entry(entry: FileEntry, prefix: str, mime: str, deps: ExpansionDeps) -> FolderFileEntry:
    """A pending entry, or one already ``skipped_size`` when the provider's
    reported size is over the file's class cap (nothing is fetched)."""
    item = FolderFileEntry(
        provider_file_id=entry.id,
        name=entry.name,
        relative_path=f"{prefix}{entry.name}",
        mime_type=mime,
        size_bytes=entry.size_bytes,
        etag=entry.etag,
    )
    if entry.size_bytes is not None:
        cap = deps.upload_service.max_size_for(entry.name, mime)
        if entry.size_bytes > cap:
            item.status = ENTRY_SKIPPED_SIZE
            item.error = f"over the {cap // (1024 * 1024)} MB limit for its type"
    return item


# ---------------------------------------------------------------------------
# Phase 2: download and import
# ---------------------------------------------------------------------------


async def _process_entry(
    folder: FolderAttachment,
    deps: ExpansionDeps,
    progress: _Progress,
    entry: FolderFileEntry,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        if progress.stop_reason or progress.row_gone:
            return  # left pending for resume
        await _import_entry(folder, deps, progress, entry)
        if entry.status == ENTRY_PENDING:
            return  # a stop (quota, consent) is not this file's outcome
        await _note_progress(folder, deps, progress)


async def _import_entry(
    folder: FolderAttachment, deps: ExpansionDeps, progress: _Progress, entry: FolderFileEntry
) -> None:
    if entry.size_bytes is not None and progress.bytes_attached + entry.size_bytes > deps.max_bytes:
        _skip_size(entry, deps)
        return

    try:
        downloaded = await deps.adapter.download(
            deps.access_token, entry.provider_file_id, max_bytes=deps.upload_service.max_download_bytes()
        )
    except FileSourceAuthError:
        progress.stop_reason = ERROR_NEEDS_CONSENT
        return
    except FileSourceTooLargeError:
        _skip_size(entry, deps)
        return
    except FileSourceError as e:
        _fail(entry, "download failed", e)
        return
    except Exception as e:  # noqa: BLE001
        _fail(entry, "download failed", e)
        return

    size = len(downloaded.content)
    if progress.bytes_attached + size > deps.max_bytes:
        _skip_size(entry, deps)
        return

    try:
        meta = await deps.upload_service.import_file(
            user_id=folder.user_id,
            session_id=folder.session_id,
            filename=downloaded.filename,
            mime_type=downloaded.content_type,
            content=downloaded.content,
            source=folder_file_source(folder.connector_id, folder.folder_attach_id),
            delivery=delivery_for(downloaded.filename, downloaded.content_type),
        )
    except QuotaExceededError:
        progress.stop_reason = ERROR_QUOTA_EXCEEDED
        return
    except InvalidFileTypeError as e:
        _fail(entry, "unsupported type", e)
        return
    except FileTooLargeError:
        _skip_size(entry, deps)
        return
    except Exception as e:  # noqa: BLE001
        _fail(entry, "could not be stored", e)
        return

    # The real name is known only after download: an export carries its own
    # extension. The path keeps its folders and takes the downloaded name.
    parent = entry.relative_path[: len(entry.relative_path) - len(entry.name)]
    entry.name = meta.filename
    entry.relative_path = f"{parent}{meta.filename}"
    entry.mime_type = meta.mime_type
    entry.size_bytes = meta.size_bytes
    entry.upload_id = meta.upload_id
    entry.status = ENTRY_ATTACHED
    entry.error = None
    entry.content_hash = _sha256(downloaded.content)
    progress.bytes_attached += meta.size_bytes


def _skip_size(entry: FolderFileEntry, deps: ExpansionDeps) -> None:
    entry.status = ENTRY_SKIPPED_SIZE
    entry.error = f"over the folder's {deps.max_bytes // (1024 * 1024)} MB total or its type's limit"


def _fail(entry: FolderFileEntry, reason: str, exc: BaseException) -> None:
    logger.warning("Folder file import failed: %s (%s)", reason, type(exc).__name__)
    entry.status = ENTRY_FAILED
    entry.error = reason


async def _note_progress(folder: FolderAttachment, deps: ExpansionDeps, progress: _Progress) -> None:
    """Write counts and entries to the row every few files (spec §4.2 step 2)."""
    async with progress.lock:
        progress.completed += 1
        if progress.completed % deps.progress_every != 0:
            return
        row = await deps.repository.update(
            folder.user_id, folder.folder_attach_id, counts=progress.counts(), files=progress.entries
        )
        if row is None:
            progress.row_gone = True


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Refresh (spec §4.6)
# ---------------------------------------------------------------------------


async def refresh_folder(folder: FolderAttachment, deps: ExpansionDeps) -> Optional[FolderAttachment]:
    """Re-list an attached folder and bring its files up to date.

    The KB-sync change rule (``kb_sync/worker.py``), two gates, cheapest first:
    the provider's version stamp as listed against the one recorded when the
    file was attached; when they differ, the bytes are downloaded and their
    sha256 compared, which absorbs a version bump that changed no content. A
    changed file is written to the **same upload id and key**
    (``FileUploadService.replace_file_content``) so the conversation's earlier
    citations stay valid; a new file is imported as the walk would; an attached
    file no longer in the folder keeps its row and is marked
    ``removed_from_source`` (only when the walk saw the whole folder: a capped
    or failed listing proves nothing about what it did not reach). A file the
    user removed stays removed.

    Returns the row as it ended, ``None`` when it was removed meanwhile. Never
    raises.
    """
    started = time.monotonic()
    try:
        return await _refresh(folder, deps, started)
    except Exception:  # noqa: BLE001 - the task's outcome is the row's status
        logger.error("Folder refresh failed unexpectedly", exc_info=True)
        try:
            attached = sum(1 for e in folder.files if e.status == ENTRY_ATTACHED)
            await deps.repository.update(
                folder.user_id, folder.folder_attach_id,
                status=FOLDER_STATUS_PARTIAL if attached else FOLDER_STATUS_FAILED,
                error=ERROR_LISTING_FAILED, refreshing=False,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not settle the folder row after a failed refresh", exc_info=True)
        return None


async def _refresh(folder: FolderAttachment, deps: ExpansionDeps, started: float) -> Optional[FolderAttachment]:
    walk = await _walk_folder(folder.provider_folder_id, deps, set(), keep=0)
    previous = {e.provider_file_id: e for e in folder.files}
    summary = RefreshSummary()
    entries: List[FolderFileEntry] = []
    to_check: List[Tuple[FolderFileEntry, FolderFileEntry]] = []
    listed: Set[str] = set()

    for listed_entry in walk.entries:
        listed.add(listed_entry.provider_file_id)
        prior = previous.get(listed_entry.provider_file_id)
        if prior is None or prior.status in (ENTRY_PENDING, ENTRY_FAILED, ENTRY_SKIPPED_SIZE):
            entries.append(listed_entry)  # new, or another try at one that never attached
        elif prior.status == ENTRY_REMOVED:
            entries.append(prior)
        else:
            prior.removed_from_source = False
            entries.append(prior)
            if prior.etag and listed_entry.etag and prior.etag == listed_entry.etag:
                summary.unchanged += 1
            else:
                to_check.append((prior, listed_entry))

    complete = walk.error is None and not walk.truncated
    for prior in folder.files:
        if prior.provider_file_id in listed or prior.status not in (ENTRY_ATTACHED, ENTRY_REMOVED):
            continue
        if prior.status == ENTRY_ATTACHED and complete and not prior.removed_from_source:
            prior.removed_from_source = True
            summary.removed_from_source += 1
        entries.append(prior)

    progress = _Progress(
        entries=entries,
        listed=walk.listed,
        skipped_type=walk.skipped_type,
        truncated=walk.truncated,
        bytes_attached=sum(e.size_bytes or 0 for e in entries if e.status == ENTRY_ATTACHED),
    )

    if walk.error != ERROR_NEEDS_CONSENT:
        semaphore = asyncio.Semaphore(max(1, deps.concurrency))
        added_before = {id(e) for e in entries if e.status == ENTRY_ATTACHED}
        jobs = [_check_changed(folder, deps, progress, summary, prior, listed_entry, semaphore) for prior, listed_entry in to_check]
        jobs += [
            _process_entry(folder, deps, progress, entry, semaphore)
            for entry in entries
            if entry.status == ENTRY_PENDING
        ]
        await asyncio.gather(*jobs)
        if progress.row_gone:
            return None
        summary.added = sum(1 for e in entries if e.status == ENTRY_ATTACHED and id(e) not in added_before)
        summary.failed += sum(1 for e in entries if e.status == ENTRY_FAILED and e.provider_file_id not in previous)

    attached = sum(1 for e in entries if e.status == ENTRY_ATTACHED)
    if walk.error == ERROR_NEEDS_CONSENT or progress.stop_reason == ERROR_NEEDS_CONSENT:
        status, error = (FOLDER_STATUS_PARTIAL if attached else FOLDER_STATUS_FAILED), ERROR_NEEDS_CONSENT
    elif progress.stop_reason == ERROR_QUOTA_EXCEEDED:
        status, error = FOLDER_STATUS_PARTIAL, ERROR_QUOTA_EXCEEDED
    elif walk.error == ERROR_LISTING_FAILED:
        status, error = (FOLDER_STATUS_PARTIAL if attached else FOLDER_STATUS_FAILED), ERROR_LISTING_FAILED
    else:
        counts = progress.counts()
        short = counts.failed or counts.skipped_size or counts.skipped_type or counts.truncated
        status, error = (FOLDER_STATUS_PARTIAL if short else FOLDER_STATUS_READY), None

    now = utc_now_iso()
    row = await deps.repository.update(
        folder.user_id, folder.folder_attach_id,
        status=status, counts=progress.counts(), files=entries,
        error=error, clear_error=error is None,
        snapshot_at=now, refreshing=False, refreshed_at=now, last_refresh=summary,
    )
    record_folder_refresh(
        outcome=status if error is None else f"{status}:{error}",
        summary=summary,
        ms=int((time.monotonic() - started) * 1000),
    )
    return row


async def _check_changed(
    folder: FolderAttachment,
    deps: ExpansionDeps,
    progress: _Progress,
    summary: RefreshSummary,
    prior: FolderFileEntry,
    listed_entry: FolderFileEntry,
    semaphore: asyncio.Semaphore,
) -> None:
    """Gate two: download, hash, and replace the bytes only when they changed."""
    async with semaphore:
        if progress.stop_reason or progress.row_gone:
            return
        try:
            downloaded = await deps.adapter.download(
                deps.access_token, prior.provider_file_id, max_bytes=deps.upload_service.max_download_bytes()
            )
        except FileSourceAuthError:
            progress.stop_reason = ERROR_NEEDS_CONSENT
            return
        except Exception as e:  # noqa: BLE001 - one file's failure keeps its old bytes
            logger.warning("Folder refresh download failed (%s); the file keeps its attached version", type(e).__name__)
            summary.failed += 1
            return

        digest = _sha256(downloaded.content)
        if prior.content_hash == digest:
            prior.etag = listed_entry.etag  # gate one passes next time
            summary.unchanged += 1
            return

        try:
            meta = await deps.upload_service.get_file(folder.user_id, prior.upload_id or "")
            if meta is None:
                raise UploadNotFoundError("gone")
            updated = await deps.upload_service.replace_file_content(meta, downloaded.content, meta.mime_type)
        except QuotaExceededError:
            progress.stop_reason = ERROR_QUOTA_EXCEEDED
            return
        except UploadNotFoundError:
            prior.status = ENTRY_REMOVED
            prior.error = "deleted by the user"
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("Folder refresh could not store a changed file (%s)", type(e).__name__)
            summary.failed += 1
            return

        prior.size_bytes = updated.size_bytes
        prior.etag = listed_entry.etag
        prior.content_hash = digest
        summary.updated += 1
