"""List a file-source folder: breadth-first, subfolders included, bounded.

One walk for the two callers that need a folder's files: app-api's folder
attachment task (``files/folders/expansion.py``,
``docs/specs/drive-folder-attachments.md`` §4.2) and the kb-sync worker's
``drive_folder`` runs (``docs/specs/assistant-kb-sync.md`` §11). Both list the
same files the same way, under the same caps, so a project saved from a folder
attachment and kept in sync with the folder holds what the attachment held.

Request-context free on purpose: it takes the adapter, the token and the caps
as arguments, and imports nothing that pulls FastAPI, so the kb-sync Lambda
image can carry it.

Content note: names and paths are the user's own; nothing here logs them.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional, Set, Tuple

from apis.shared.files.models import ALLOWED_EXTENSIONS, is_allowed_mime_type

from apis.app_api.file_sources.adapter import FileSourceAdapter
from apis.app_api.file_sources.models import (
    FileEntry,
    FileEntryType,
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
)

logger = logging.getLogger(__name__)

#: The token was rejected: the caller needs a fresh consent.
WALK_AUTH = "auth"
#: The root folder itself is gone (or no longer shared with the token's user).
WALK_NOT_FOUND = "not_found"
#: Any other listing failure, part way or at the start.
WALK_FAILED = "failed"


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


@dataclass
class WalkedFile:
    """One accepted file: the provider's entry, its path from the root, and
    the MIME type it downloads as."""

    entry: FileEntry
    relative_path: str
    mime_type: str


@dataclass
class FolderWalk:
    """What a walk found.

    ``listed`` counts every file seen (accepted or not); ``truncated`` means a
    cap stopped the walk, so files past it were never seen; ``error`` is one of
    ``WALK_*`` when a listing call failed. A walk that is truncated or errored
    proves nothing about the files it did not reach.
    """

    files: List[WalkedFile] = field(default_factory=list)
    listed: int = 0
    skipped_type: int = 0
    truncated: bool = False
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.error is None and not self.truncated


async def walk_folder(
    adapter: FileSourceAdapter,
    access_token: str,
    root_id: str,
    *,
    max_files: int,
    max_pages: int,
    skip_ids: Optional[Set[str]] = None,
    accept: Callable[[FileEntry], Optional[str]] = attachable_mime_for,
) -> FolderWalk:
    """Breadth-first over ``browse``, one page at a time, bounded by the caps.

    ``max_files`` bounds the accepted files returned; ``max_pages`` bounds the
    listing calls, so a root holding thousands of folders with few readable
    files cannot make the walk unbounded. A file in ``skip_ids`` is counted as
    listed and not returned. A 404 on the root's first page is
    ``WALK_NOT_FOUND``; on a subfolder (removed mid-walk) it is ``WALK_FAILED``.
    """
    walk = FolderWalk()
    skip = skip_ids or set()
    queue: Deque[Tuple[str, str]] = deque([(root_id, "")])
    pages = 0

    while queue:
        folder_id, prefix = queue.popleft()
        cursor: Optional[str] = None
        while True:
            if pages >= max_pages:
                walk.truncated = True
                return walk
            try:
                page = await adapter.browse(access_token, folder_id, cursor)
            except FileSourceAuthError:
                walk.error = WALK_AUTH
                return walk
            except FileSourceNotFoundError:
                walk.error = WALK_NOT_FOUND if pages == 0 else WALK_FAILED
                return walk
            except FileSourceError as e:
                logger.warning("Folder listing failed: %s", type(e).__name__)
                walk.error = WALK_FAILED
                return walk
            pages += 1

            for entry in page.entries:
                if entry.type == FileEntryType.FOLDER:
                    queue.append((entry.id, f"{prefix}{entry.name}/"))
                    continue
                walk.listed += 1
                if entry.id in skip:
                    continue
                mime = accept(entry)
                if mime is None:
                    walk.skipped_type += 1
                    continue
                if len(walk.files) >= max_files:
                    walk.truncated = True
                    return walk
                walk.files.append(WalkedFile(entry=entry, relative_path=f"{prefix}{entry.name}", mime_type=mime))

            cursor = page.next_cursor
            if not cursor:
                break
    return walk
