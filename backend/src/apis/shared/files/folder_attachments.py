"""A Google Drive folder attached to a conversation: the folder row.

``docs/specs/drive-folder-attachments.md`` §4.2. A folder is **one**
attachment. Its files are ordinary session file rows, imported by app-api's
expansion task and delivered by reference whatever their size (§4.3); the
folder row records the walk: what was listed, what was attached, what was
skipped and why, and each file's path relative to the folder root, which the
file rows do not carry.

The row lives in the user-files table beside the file rows::

    PK=USER#{user}      SK=FOLDER#{folder_attach_id}
    GSI1PK=CONV#{session}  GSI1SK=FOLDER#{folder_attach_id}

so the session partition finds it with the files, and the file listings key
on the ``FILE#`` prefix to leave it out. It is shared code because the
Runtime reads it too (the manifest, PR-B); only app-api writes it.

Content note: ``folderName``, ``folderPath`` and each entry's ``name`` and
``relativePath`` are the user's own names for their files. They never reach
a log line or a metric; :func:`record_folder_expansion` emits counts only.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.timestamps import from_iso, utc_now_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caps (spec §4.5) and timing (§4.9)
# ---------------------------------------------------------------------------

#: Attachable files the walk lists before it stops and marks the row
#: ``truncated``. Deliberately below the user quota so one folder cannot
#: consume a user's whole storage.
FOLDER_ATTACH_MAX_FILES = int(os.environ.get("FOLDER_ATTACH_MAX_FILES", 100))

#: Running total of attached bytes per folder; files past it are
#: ``skipped_size``.
FOLDER_ATTACH_MAX_BYTES = int(os.environ.get("FOLDER_ATTACH_MAX_BYTES", 250 * 1024 * 1024))

#: Downloads in flight per expansion.
FOLDER_ATTACH_CONCURRENCY = int(os.environ.get("FOLDER_ATTACH_CONCURRENCY", 3))

#: Browse pages one walk may fetch. A root like "My Drive" can hold thousands
#: of folders with few readable files; this bounds the listing itself, not
#: just the attachable count.
FOLDER_ATTACH_MAX_PAGES = int(os.environ.get("FOLDER_ATTACH_MAX_PAGES", 200))

#: Seconds after which an ``expanding`` row whose task has gone quiet (an
#: app-api restart lost it) reads as stale, so the SPA calls ``resume``.
FOLDER_EXPANSION_TIMEOUT = int(os.environ.get("FOLDER_EXPANSION_TIMEOUT", 15 * 60))

#: Files between progress writes to the row, so the card's count moves.
FOLDER_PROGRESS_EVERY = 5

#: Row lifetime, as file rows.
FOLDER_TTL_SECONDS = 365 * 24 * 60 * 60

#: Folders one message may carry (spec §4.5). Each counts as ONE attachment
#: against ``MAX_FILES_PER_MESSAGE``, whatever its file count (§9 D1).
FOLDERS_PER_MESSAGE = int(os.environ.get("FOLDER_ATTACH_MAX_PER_MESSAGE", 2))

#: Entries a folder's manifest lists before it says how many more there are
#: (spec §4.4). About 30 tokens each: 50 entries is ~1,500 tokens, paid once in
#: the turn that attaches the folder and then cached as history.
FOLDER_MANIFEST_MAX_ENTRIES = int(os.environ.get("FOLDER_MANIFEST_MAX_ENTRIES", 50))

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

FOLDER_STATUS_EXPANDING = "expanding"
FOLDER_STATUS_READY = "ready"
FOLDER_STATUS_PARTIAL = "partial"
FOLDER_STATUS_FAILED = "failed"
FolderStatus = Literal["expanding", "ready", "partial", "failed"]

#: One entry per attachable file the walk listed. ``pending`` is the state
#: between listing and download, and what a quota stop leaves behind for
#: ``resume`` to retry.
ENTRY_PENDING = "pending"
ENTRY_ATTACHED = "attached"
ENTRY_FAILED = "failed"
ENTRY_SKIPPED_SIZE = "skipped_size"
#: The user deleted the file (Files page, a file card): its row is gone, so it
#: is not readable, and neither resume nor refresh brings it back.
ENTRY_REMOVED = "removed"
EntryStatus = Literal["pending", "attached", "failed", "skipped_size", "removed"]

#: Why a row ended ``failed`` or ``partial`` short of its files. Codes, never
#: provider messages.
ERROR_NEEDS_CONSENT = "needs_consent"
ERROR_LISTING_FAILED = "listing_failed"
ERROR_QUOTA_EXCEEDED = "quota_exceeded"


#: Recent folders the composer's picker offers
#: (``docs/specs/composer-project-folder-picker.md`` §3.3): how many distinct
#: folders by default and at most, and how many rows one listing reads at most
#: to find them, so a folder attached to many conversations cannot make the
#: read unbounded.
RECENT_FOLDERS_DEFAULT_LIMIT = 8
RECENT_FOLDERS_MAX_LIMIT = 20
RECENT_FOLDERS_MAX_ROWS = 100


def folder_attach_id_from_source(source: Optional[str]) -> Optional[str]:
    """The folder a file row came from: its ``source`` is
    ``connector:{connector_id}:folder:{folder_attach_id}``."""
    if not source or ":folder:" not in source:
        return None
    return source.rsplit(":folder:", 1)[1] or None


def new_folder_attach_id() -> str:
    """A timestamp-prefixed id (``{ms_hex}_{uuid16}``), the upload-id shape."""
    timestamp_hex = format(int(datetime.now(timezone.utc).timestamp() * 1000), "x")
    return f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FolderCounts(BaseModel):
    """What the walk found, by outcome (spec §4.2)."""

    model_config = ConfigDict(populate_by_name=True)

    listed: int = Field(0, description="File entries seen in the walk, every type")
    attached: int = Field(0, description="Files imported as session files")
    skipped_type: int = Field(0, alias="skippedType", description="Files with no on-demand reader (images, unknown types)")
    skipped_size: int = Field(0, alias="skippedSize", description="Files over their class cap or the folder byte cap")
    failed: int = Field(0, description="Files whose download or import failed")
    truncated: bool = Field(False, description="The walk stopped at a cap before the tree was exhausted")


class FolderFileEntry(BaseModel):
    """One attachable file the walk listed, and what became of it."""

    model_config = ConfigDict(populate_by_name=True)

    provider_file_id: str = Field(..., alias="providerFileId")
    name: str = Field(..., description="Filename as it downloads (an export's extension included)")
    relative_path: str = Field(..., alias="relativePath", description="Path from the folder root, `/`-joined")
    mime_type: str = Field(..., alias="mimeType", description="The type the bytes arrive as")
    size_bytes: Optional[int] = Field(None, alias="sizeBytes", description="Provider-reported size, when known")
    upload_id: Optional[str] = Field(None, alias="uploadId", description="The session file row, once attached")
    status: EntryStatus = Field(ENTRY_PENDING)
    error: Optional[str] = Field(None, description="Short reason, when failed or skipped")
    # Change detection for refresh (spec §4.6), the KB-sync rule: the provider's
    # version stamp as listed (Drive `version`), then a sha256 of the bytes
    # attached, which absorbs version bumps that changed no content.
    etag: Optional[str] = Field(None, description="Provider version stamp when attached")
    content_hash: Optional[str] = Field(None, alias="contentHash", description="sha256 of the bytes attached")
    #: Still attached and readable, but no longer in the folder at the last refresh.
    removed_from_source: bool = Field(False, alias="removedFromSource")


class RefreshSummary(BaseModel):
    """What the last refresh did, for the card's line (spec §4.6)."""

    model_config = ConfigDict(populate_by_name=True)

    unchanged: int = 0
    updated: int = 0
    added: int = 0
    removed_from_source: int = Field(0, alias="removedFromSource")
    failed: int = 0


class RecentFolder(BaseModel):
    """A source folder the user has attached before, for the picker's
    "Recent folders" rows (composer-project-folder-picker spec §3.3).

    One per ``(connector_id, provider_folder_id)``, named from its newest row.
    Carries what a new attach needs and nothing of the conversation it came
    from: no session id, no files.
    """

    model_config = ConfigDict(populate_by_name=True)

    connector_id: str = Field(..., alias="connectorId")
    connector_name: Optional[str] = Field(None, alias="connectorName")
    provider_folder_id: str = Field(..., alias="providerFolderId")
    folder_name: str = Field(..., alias="folderName")
    folder_path: Optional[str] = Field(None, alias="folderPath")
    last_attached_at: str = Field(..., alias="lastAttachedAt")


class FolderAttachment(BaseModel):
    """The folder row (spec §4.2)."""

    model_config = ConfigDict(populate_by_name=True)

    folder_attach_id: str = Field(..., alias="folderAttachId")
    session_id: str = Field(..., alias="sessionId")
    user_id: str = Field(..., alias="userId")
    connector_id: str = Field(..., alias="connectorId")
    connector_name: Optional[str] = Field(
        None, alias="connectorName", description="The connector's display name when attached (the manifest's `source`)"
    )
    provider_folder_id: str = Field(..., alias="providerFolderId")
    folder_name: str = Field(..., alias="folderName")
    folder_path: Optional[str] = Field(None, alias="folderPath", description="Breadcrumb path from the browser, display only")
    status: FolderStatus = Field(FOLDER_STATUS_EXPANDING)
    error: Optional[str] = Field(None, description="needs_consent | listing_failed | quota_exceeded")
    counts: FolderCounts = Field(default_factory=FolderCounts)
    files: List[FolderFileEntry] = Field(default_factory=list)
    snapshot_at: Optional[str] = Field(None, alias="snapshotAt", description="When the listing was taken (refresh, §4.6)")
    #: True while a refresh (not a first expansion) is walking the folder.
    refreshing: bool = False
    refreshed_at: Optional[str] = Field(None, alias="refreshedAt")
    last_refresh: Optional[RefreshSummary] = Field(None, alias="lastRefresh")
    created_at: str = Field(default_factory=utc_now_iso, alias="createdAt")
    updated_at: str = Field(default_factory=utc_now_iso, alias="updatedAt")
    ttl: Optional[int] = Field(None, description="Unix epoch for TTL expiration")

    @property
    def attached_provider_ids(self) -> set:
        """Provider file ids already attached: what ``resume`` skips (§4.9)."""
        return {f.provider_file_id for f in self.files if f.status == ENTRY_ATTACHED}

    @property
    def settled_provider_ids(self) -> set:
        """Attached, or removed by the user: what neither resume nor refresh re-imports."""
        return {f.provider_file_id for f in self.files if f.status in (ENTRY_ATTACHED, ENTRY_REMOVED)}

    def is_stale(self, now: Optional[datetime] = None, timeout_seconds: int = FOLDER_EXPANSION_TIMEOUT) -> bool:
        """An ``expanding`` row nothing has touched for the timeout.

        The task writes progress every few files, so a quiet row past the
        timeout means the task is gone (an app-api restart, §4.9) and the SPA
        should call ``resume``.
        """
        if self.status != FOLDER_STATUS_EXPANDING:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            touched = from_iso(self.updated_at)
        except (TypeError, ValueError):
            return True
        if touched.tzinfo is None:
            touched = touched.replace(tzinfo=timezone.utc)
        return (now - touched).total_seconds() > timeout_seconds

    def to_dynamo_item(self) -> Dict[str, Any]:
        ttl_value = self.ttl
        if ttl_value is None:
            ttl_value = int(from_iso(self.created_at).timestamp()) + FOLDER_TTL_SECONDS
        item = self.model_dump(by_alias=True, exclude_none=True)
        item["ttl"] = ttl_value
        item.update(
            {
                "PK": f"USER#{self.user_id}",
                "SK": f"FOLDER#{self.folder_attach_id}",
                "GSI1PK": f"CONV#{self.session_id}",
                "GSI1SK": f"FOLDER#{self.folder_attach_id}",
                "entityType": "FOLDER",
            }
        )
        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "FolderAttachment":
        data = {k: v for k, v in _plain(item).items() if k not in {"PK", "SK", "GSI1PK", "GSI1SK", "entityType"}}
        return cls.model_validate(data)


def _plain(value: Any) -> Any:
    """DynamoDB's ``Decimal`` numbers back to ``int``/``float``, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Metric (spec §6)
# ---------------------------------------------------------------------------


def record_folder_refresh(*, outcome: str, summary: "RefreshSummary", ms: int) -> None:
    """One content-free ``FolderRefresh`` EMF record per refresh. Never raises."""
    logger.info(
        "folder_attachment_refreshed: outcome=%s unchanged=%d updated=%d added=%d removed_from_source=%d failed=%d ms=%d",
        outcome, summary.unchanged, summary.updated, summary.added, summary.removed_from_source, summary.failed, ms,
    )
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={
                "FolderRefresh": 1,
                "FolderRefreshUnchanged": int(summary.unchanged),
                "FolderRefreshUpdated": int(summary.updated),
                "FolderRefreshAdded": int(summary.added),
                "FolderRefreshRemovedFromSource": int(summary.removed_from_source),
                "FolderRefreshFailed": int(summary.failed),
                "FolderRefreshMs": int(ms),
            },
            properties={"outcome": outcome},
            units={
                "FolderRefresh": "Count",
                "FolderRefreshUnchanged": "Count",
                "FolderRefreshUpdated": "Count",
                "FolderRefreshAdded": "Count",
                "FolderRefreshRemovedFromSource": "Count",
                "FolderRefreshFailed": "Count",
                "FolderRefreshMs": "Milliseconds",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("FolderRefresh EMF skipped: %s", e)


def record_folder_expansion(*, outcome: str, counts: FolderCounts, bytes_attached: int, ms: int) -> None:
    """One content-free ``FolderAttachment`` EMF record per expansion.

    In ``AgentCoreStack/Compaction`` beside ``AttachmentReference``. Counts,
    bytes and wall time only: never a folder name, a path, an id or a user.
    Never raises.
    """
    logger.info(
        "folder_attachment_expanded: outcome=%s listed=%d attached=%d skipped_type=%d skipped_size=%d failed=%d truncated=%s bytes=%d ms=%d",
        outcome, counts.listed, counts.attached, counts.skipped_type, counts.skipped_size, counts.failed,
        counts.truncated, bytes_attached, ms,
    )
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={
                "FolderAttachment": 1,
                "FolderFilesListed": int(counts.listed),
                "FolderFilesAttached": int(counts.attached),
                "FolderFilesSkippedType": int(counts.skipped_type),
                "FolderFilesSkippedSize": int(counts.skipped_size),
                "FolderFilesFailed": int(counts.failed),
                "FolderBytesAttached": int(bytes_attached),
                "FolderExpansionMs": int(ms),
            },
            properties={"outcome": outcome, "truncated": "true" if counts.truncated else "false"},
            units={
                "FolderAttachment": "Count",
                "FolderFilesListed": "Count",
                "FolderFilesAttached": "Count",
                "FolderFilesSkippedType": "Count",
                "FolderFilesSkippedSize": "Count",
                "FolderFilesFailed": "Count",
                "FolderBytesAttached": "Bytes",
                "FolderExpansionMs": "Milliseconds",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("FolderAttachment EMF skipped: %s", e)
