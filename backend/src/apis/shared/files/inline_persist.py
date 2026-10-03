"""Persist inline attachment bytes as session files.

The SPA never sends file bytes on a turn: it uploads first (presigned PUT via
app-api) and sends ``file_upload_ids``, so every attachment it makes already
has an S3 object and a ``FileMetadata`` row. Headless callers — the harness,
scheduled runs, API clients, the smoke matrix — post base64 ``files`` instead.

That is fine for anything the turn sends inline to Bedrock. It is a silent
hole for the classes the turn *diverts*: spreadsheets and decks are dropped
from the prompt with a note saying the Spreadsheet Analysis / PowerPoint tools
can reach them, but those tools look files up through
``FileUploadRepository.list_session_files`` — and an inline file was never
written anywhere. The model then reports the file "is not accessible", and the
note that promised otherwise is already in the conversation.

This module closes the hole by giving an inline diverted file exactly what an
upload would have: the same S3 key layout, the same metadata row, ``READY``
status, the quota increment. Validation mirrors app-api's upload service
(allowlisted MIME, per-class size cap, user quota) so an inline caller cannot
bypass limits the SPA path enforces. ``source`` distinguishes the provenance
for display; it is never an access input.

Cost on the turn path: one S3 put per diverted inline file, run off the event
loop and in parallel across files, inside the ``preamble.files`` stage. It
only runs when the request carries inline bytes of a diverted class, which the
SPA never does — attended turns pay nothing.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol, Sequence, Tuple

import boto3

from apis.shared.files.models import (
    FileMetadata,
    FileStatus,
    is_allowed_mime_type,
    is_presentation_file,
)
from apis.shared.files.repository import FileUploadRepository, get_file_upload_repository

logger = logging.getLogger(__name__)

#: Provenance recorded on rows this module writes (``FileMetadata.source``).
INLINE_SOURCE = "inline"


class InlineFile(Protocol):
    """What the turn hands us: the three fields of ``FileContent``."""

    filename: str
    content_type: str
    bytes: str  # base64


@dataclass(frozen=True)
class PersistFailure:
    """One inline file that was not persisted, and why (user-facing)."""

    file: Any
    reason: str


# Same env names and defaults as ``apis.app_api.files.service.FileUploadService``.
# The inference container cannot import app-api (``tests/architecture``), so the
# limits are read here under the same contract rather than imported.
def _max_size_for(filename: str, mime_type: str) -> int:
    if is_presentation_file(filename, mime_type):
        return int(os.environ.get("FILE_UPLOAD_MAX_SIZE_BYTES_PRESENTATION", 25 * 1024 * 1024))
    return int(os.environ.get("FILE_UPLOAD_MAX_SIZE_BYTES", 4 * 1024 * 1024))


def _user_quota_bytes() -> int:
    return int(os.environ.get("FILE_UPLOAD_USER_QUOTA_BYTES", 1024 * 1024 * 1024))


def _bucket_name() -> str:
    return os.environ.get("S3_USER_FILES_BUCKET_NAME", "user-files")


def new_upload_id() -> str:
    """Timestamp-prefixed id, byte-compatible with app-api's upload ids."""
    timestamp_hex = format(int(datetime.now(timezone.utc).timestamp() * 1000), "x")
    return f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"


def s3_key_for(user_id: str, session_id: str, upload_id: str, filename: str) -> str:
    """The upload service's key layout, so tools and lifecycle rules see one shape."""
    return f"user-files/{user_id}/{session_id}/{upload_id}/{filename}"


def _decode(file: InlineFile) -> Optional[bytes]:
    try:
        return base64.b64decode(file.bytes or "", validate=True)
    except (binascii.Error, ValueError):
        return None


class InlineAttachmentPersister:
    """Write inline attachment bytes to S3 and register them for the session."""

    def __init__(
        self,
        *,
        repository: Optional[FileUploadRepository] = None,
        s3_client: Any = None,
        bucket_name: Optional[str] = None,
    ) -> None:
        self._repository = repository or get_file_upload_repository()
        self._bucket = bucket_name or _bucket_name()
        self._s3 = s3_client or boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))

    async def persist(
        self,
        *,
        user_id: str,
        session_id: str,
        files: Sequence[InlineFile],
        source: str = INLINE_SOURCE,
    ) -> Tuple[List[FileMetadata], List[PersistFailure]]:
        """Persist ``files`` for ``session_id``. Never raises.

        Returns ``(persisted, failures)`` in attachment order. A failure is
        per-file: one bad file does not take the others down, and nothing
        here fails the turn — the caller decides what to tell the user.
        """
        if not files:
            return [], []

        # One quota read for the batch, charged cumulatively so five files
        # that each fit cannot together overshoot the cap.
        try:
            quota = await self._repository.get_user_quota(user_id)
            used = int(quota.total_bytes)
        except Exception:  # noqa: BLE001 — a quota read failure must not block the turn
            logger.warning("inline persist: quota lookup failed; proceeding without the cap", exc_info=True)
            used = 0
        cap = _user_quota_bytes()

        plans: List[Tuple[Any, Optional[bytes], Optional[str]]] = []
        for f in files:
            content_type = (f.content_type or "").lower()
            if not is_allowed_mime_type(content_type):
                plans.append((f, None, f"its type `{content_type or 'unknown'}` is not accepted"))
                continue
            raw = _decode(f)
            if raw is None:
                plans.append((f, None, "its contents were not valid base64"))
                continue
            limit = _max_size_for(f.filename, content_type)
            if len(raw) > limit:
                plans.append((f, None, f"it is larger than the {limit // (1024 * 1024)} MB limit"))
                continue
            if used + len(raw) > cap:
                plans.append((f, None, "your file storage quota is full"))
                continue
            used += len(raw)
            plans.append((f, raw, None))

        results = await asyncio.gather(
            *(self._store_one(user_id, session_id, f, raw, source) for f, raw, why in plans if raw is not None),
            return_exceptions=False,
        )
        stored = iter(results)

        persisted: List[FileMetadata] = []
        failures: List[PersistFailure] = []
        for f, raw, why in plans:
            if raw is None:
                failures.append(PersistFailure(file=f, reason=why or "it could not be stored"))
                continue
            meta = next(stored)
            if meta is None:
                failures.append(PersistFailure(file=f, reason="it could not be stored"))
            else:
                persisted.append(meta)
        if persisted:
            logger.info("inline persist: stored %d file(s) for session %s as %s", len(persisted), session_id, source)
        if failures:
            logger.warning(
                "inline persist: %d file(s) not stored: %s",
                len(failures),
                [(x.file.filename, x.reason) for x in failures],
            )
        return persisted, failures

    async def _store_one(self, user_id: str, session_id: str, f: InlineFile, raw: bytes, source: str) -> Optional[FileMetadata]:
        upload_id = new_upload_id()
        key = s3_key_for(user_id, session_id, upload_id, f.filename)
        content_type = (f.content_type or "").lower()
        try:
            # boto3 is synchronous; keep the S3 round trip off the event loop so
            # the stream's head-of-turn work is not blocked behind it.
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=raw,
                ContentType=content_type,
            )
        except Exception:  # noqa: BLE001
            logger.error("inline persist: S3 put failed for %s", f.filename, exc_info=True)
            return None

        meta = FileMetadata(
            upload_id=upload_id,
            user_id=user_id,
            session_id=session_id,
            filename=f.filename,
            mime_type=content_type,
            size_bytes=len(raw),
            s3_key=key,
            s3_bucket=self._bucket,
            status=FileStatus.READY,
            source=source,
        )
        try:
            await self._repository.create_file(meta)
        except Exception:  # noqa: BLE001
            logger.error("inline persist: metadata write failed for %s", f.filename, exc_info=True)
            # Do not leave an orphan object that no row points at.
            try:
                await asyncio.to_thread(self._s3.delete_object, Bucket=self._bucket, Key=key)
            except Exception:  # noqa: BLE001
                logger.warning("inline persist: orphan cleanup failed for %s", key, exc_info=True)
            return None
        try:
            await self._repository.increment_quota(user_id, len(raw))
        except Exception:  # noqa: BLE001 — the file is usable; the counter can drift by one file
            logger.warning("inline persist: quota increment failed for %s", upload_id, exc_info=True)
        return meta


_persister: Optional[InlineAttachmentPersister] = None


def get_inline_attachment_persister() -> InlineAttachmentPersister:
    """Process-wide persister (one S3 client), overridable in tests."""
    global _persister
    if _persister is None:
        _persister = InlineAttachmentPersister()
    return _persister
