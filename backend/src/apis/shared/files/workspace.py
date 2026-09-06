"""Session workspace service — bounded list/read/write over the user-files store.

Backs the ``workspace_list`` / ``workspace_read`` / ``workspace_write`` agent
tools (see ``agents/builtin_tools/workspace_tools.py`` and
``docs/specs/session-workspace-tools.md``). Every operation goes through the
DynamoDB user-files table (`FileMetadata` + `FileUploadRepository`) — the table
is the source of truth, never a raw S3 listing — so workspace files appear in
the SPA Files panel and participate in quota accounting exactly like uploads.

Hard rules encoded here:
* Reads and writes are bounded (`WORKSPACE_READ_MAX_BYTES`,
  `WORKSPACE_WRITE_MAX_BYTES`) — file bytes never flow through the model
  unbounded, and binary files move by reference (presigned URL) only.
* Identity is caller-supplied and mandatory: a missing ``user_id`` /
  ``session_id`` raises instead of defaulting (a silent default would collapse
  sessions into a shared namespace).
* Ownership is enforced by the table's key shape (``PK = USER#{userId}``);
  no operation accepts a model-supplied S3 key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .models import FileMetadata, FileStatus
from .repository import get_file_upload_repository

logger = logging.getLogger(__name__)

# Per-call byte cap for text reads; continuation via `offset`.
WORKSPACE_READ_MAX_BYTES = int(
    os.environ.get("WORKSPACE_READ_MAX_BYTES", 48 * 1024)  # 48KB
)
# Per-call byte cap for writes. Model-generated text is inherently small; the
# cap bounds runaway loops, not legitimate deliverables.
WORKSPACE_WRITE_MAX_BYTES = int(
    os.environ.get("WORKSPACE_WRITE_MAX_BYTES", 1024 * 1024)  # 1MB
)
# A tool result is a per-turn payload too — cap listings.
WORKSPACE_LIST_MAX_ENTRIES = int(os.environ.get("WORKSPACE_LIST_MAX_ENTRIES", 100))

# Same ceiling the upload flow enforces (apis/app_api/files/service.py).
_USER_QUOTA_BYTES = int(
    os.environ.get("FILE_UPLOAD_USER_QUOTA_BYTES", 1024 * 1024 * 1024)  # 1GB
)

_DOWNLOAD_URL_TTL = 60 * 60  # 1 hour, matches word_document_tool

# MIME types whose content may be returned inline as text.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = frozenset({"application/json"})

# MIME types workspace_write accepts, with their canonical extension.
WRITABLE_MIME_TYPES: Dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
}

# Filename: single path segment, no traversal, sane characters.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,120}$")


class WorkspaceError(Exception):
    """Base for workspace failures surfaced conversationally by the tools."""


class WorkspaceStorageNotConfiguredError(WorkspaceError):
    """S3_USER_FILES_BUCKET_NAME is not set on the runtime."""


class WorkspaceFileNotFoundError(WorkspaceError):
    """No READY file with that upload_id belongs to this user."""


class WorkspaceQuotaExceededError(WorkspaceError):
    """The write would push the user past their storage quota."""


class WorkspaceValidationError(WorkspaceError):
    """Bad filename / MIME type / size / offset."""


def is_text_mime(mime_type: str) -> bool:
    """True when the MIME type's content can be returned inline as text."""
    mt = (mime_type or "").lower().split(";")[0].strip()
    return mt.startswith(_TEXT_MIME_PREFIXES) or mt in _TEXT_MIME_EXACT


def _require_identity(user_id: str, session_id: Optional[str] = None) -> None:
    """Fail loudly on missing identity — never default to a shared namespace."""
    if not user_id:
        raise WorkspaceError("workspace called without a user_id")
    if session_id is not None and not session_id:
        raise WorkspaceError("workspace called without a session_id")


def _bucket() -> str:
    bucket = os.environ.get("S3_USER_FILES_BUCKET_NAME")
    if not bucket:
        raise WorkspaceStorageNotConfiguredError(
            "S3_USER_FILES_BUCKET_NAME is not set on the runtime"
        )
    return bucket


_s3_client = None
_bucket_region: Optional[str] = None


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _resolve_bucket_region(bucket: str) -> str:
    """Discover the user-files bucket's real region (see word_document_tool:
    a client pinned to the wrong region fails PutObject with
    PermanentRedirect; ``head_bucket`` reports the true region either way).
    """
    global _bucket_region
    if _bucket_region:
        return _bucket_region

    region = None
    probe = boto3.client("s3", region_name=_region())
    try:
        resp = probe.head_bucket(Bucket=bucket)
        region = (
            resp.get("ResponseMetadata", {})
            .get("HTTPHeaders", {})
            .get("x-amz-bucket-region")
        )
    except ClientError as exc:
        region = (
            exc.response.get("ResponseMetadata", {})
            .get("HTTPHeaders", {})
            .get("x-amz-bucket-region")
        )
        if not region:
            logger.warning(f"Could not resolve region for bucket {bucket}: {exc}")
    except Exception as exc:  # pragma: no cover - network edge
        logger.warning(f"Could not resolve region for bucket {bucket}: {exc}")
    _bucket_region = region or _region()
    return _bucket_region


def _s3():
    """SigV4 S3 client pinned to the user-files bucket's actual region."""
    global _s3_client
    if _s3_client is None:
        region = _resolve_bucket_region(_bucket())
        _s3_client = boto3.client(
            "s3",
            region_name=region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3_client


def _entry(meta: FileMetadata, include_session: bool) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "upload_id": meta.upload_id,
        "filename": meta.filename,
        "mime_type": meta.mime_type,
        "size_bytes": meta.size_bytes,
        "source": meta.source,
        "created_at": meta.created_at.isoformat(),
        "readable": is_text_mime(meta.mime_type),
    }
    if include_session:
        entry["session_id"] = meta.session_id
    return entry


async def list_workspace_files(
    user_id: str, session_id: str, scope: str = "session"
) -> Dict[str, Any]:
    """List the user's READY files — this conversation or all conversations.

    DynamoDB-only (never an S3 listing). Output is capped at
    ``WORKSPACE_LIST_MAX_ENTRIES`` newest-first entries with a ``truncated``
    flag.
    """
    _require_identity(user_id, session_id)
    if scope not in ("session", "user"):
        raise WorkspaceValidationError(
            f"Unknown scope '{scope}' — use 'session' or 'user'"
        )

    repo = get_file_upload_repository()
    truncated = False

    if scope == "session":
        files = await repo.list_session_files(session_id, status=FileStatus.READY)
        # The GSI partition is the session; enforce ownership explicitly.
        files = [m for m in files if m.user_id == user_id]
        if len(files) > WORKSPACE_LIST_MAX_ENTRIES:
            files = files[:WORKSPACE_LIST_MAX_ENTRIES]
            truncated = True
    else:
        files, next_cursor = await repo.list_user_files(
            user_id, limit=WORKSPACE_LIST_MAX_ENTRIES, status=FileStatus.READY
        )
        truncated = next_cursor is not None

    return {
        "scope": scope,
        "files": [_entry(m, include_session=scope == "user") for m in files],
        "count": len(files),
        "truncated": truncated,
    }


async def _get_owned_ready_file(user_id: str, upload_id: str) -> FileMetadata:
    meta = await get_file_upload_repository().get_file(user_id, upload_id)
    if meta is None:
        raise WorkspaceFileNotFoundError(
            f"No file with id '{upload_id}' found in your workspace"
        )
    status = meta.status if isinstance(meta.status, str) else meta.status.value
    if status != FileStatus.READY.value:
        raise WorkspaceFileNotFoundError(
            f"No file with id '{upload_id}' found in your workspace"
        )
    return meta


def _ranged_get(bucket: str, key: str, offset: int, length: int) -> bytes:
    """Blocking ranged S3 GET — only `length` bytes ever enter memory."""
    resp = _s3().get_object(
        Bucket=bucket, Key=key, Range=f"bytes={offset}-{offset + length - 1}"
    )
    return resp["Body"].read()


async def read_workspace_file(
    user_id: str, upload_id: str, offset: int = 0
) -> Dict[str, Any]:
    """Read a file's content (text, bounded) or mint a reference (binary).

    Text MIME types return up to ``WORKSPACE_READ_MAX_BYTES`` UTF-8 bytes from
    ``offset`` with ``truncated`` + ``next_offset`` for continuation. All other
    types return metadata plus a short-lived presigned GET URL — never base64.
    """
    _require_identity(user_id)
    if offset < 0:
        raise WorkspaceValidationError("offset must be >= 0")

    meta = await _get_owned_ready_file(user_id, upload_id)
    base = {
        "upload_id": meta.upload_id,
        "filename": meta.filename,
        "mime_type": meta.mime_type,
        "size_bytes": meta.size_bytes,
        "source": meta.source,
    }

    if not is_text_mime(meta.mime_type):
        url = await asyncio.to_thread(
            _s3().generate_presigned_url,
            "get_object",
            Params={"Bucket": meta.s3_bucket, "Key": meta.s3_key},
            ExpiresIn=_DOWNLOAD_URL_TTL,
        )
        return {
            **base,
            "encoding": "reference",
            "download_url": url,
            "note": (
                "Binary file — content is not returned inline. Use the URL for "
                "delivery, analyze_spreadsheet for tabular data, or the code "
                "interpreter for byte-level processing."
            ),
        }

    if offset >= meta.size_bytes:
        raise WorkspaceValidationError(
            f"offset {offset} is beyond the end of the file "
            f"({meta.size_bytes} bytes)"
        )

    data = await asyncio.to_thread(
        _ranged_get, meta.s3_bucket, meta.s3_key, offset, WORKSPACE_READ_MAX_BYTES
    )
    end = offset + len(data)
    truncated = end < meta.size_bytes
    return {
        **base,
        "encoding": "text",
        "content": data.decode("utf-8", errors="replace"),
        "offset": offset,
        "truncated": truncated,
        "next_offset": end if truncated else None,
    }


def _validate_filename(filename: str, mime_type: str) -> str:
    """Sanitize the filename and reconcile its extension with the MIME type.

    Returns the final filename. A missing extension gets the MIME type's
    canonical one appended; a mismatched extension is rejected.
    """
    if mime_type not in WRITABLE_MIME_TYPES:
        allowed = ", ".join(sorted(WRITABLE_MIME_TYPES))
        raise WorkspaceValidationError(
            f"Unsupported mime_type '{mime_type}'. Workspace writes are "
            f"text-only: {allowed}. Use the dedicated document tools for "
            "binary formats."
        )
    if not filename or not _FILENAME_RE.match(filename) or ".." in filename:
        raise WorkspaceValidationError(
            f"Invalid filename '{filename}'. Use a single name (no path "
            "separators) with letters, numbers, dots, hyphens, underscores, "
            "or spaces."
        )

    expected_ext = WRITABLE_MIME_TYPES[mime_type]
    root, dot, ext = filename.rpartition(".")
    if not root:
        return f"{filename}{expected_ext}"
    if f".{ext.lower()}" != expected_ext:
        raise WorkspaceValidationError(
            f"Filename extension '.{ext}' does not match mime_type "
            f"'{mime_type}' (expected '{expected_ext}')"
        )
    return filename


async def write_workspace_file(
    user_id: str,
    session_id: str,
    filename: str,
    content: str,
    mime_type: str = "text/plain",
    source: str = "agent",
) -> Dict[str, Any]:
    """Write a text file into the current session's workspace.

    Mirrors the canonical agent write path
    (``word_document_tool._store_document``): put_object under the session's
    prefix → READY ``FileMetadata`` row → quota increment → presigned
    download URL. Each write is a new ``upload_id`` (no in-place overwrite);
    listings return newest-first, so a same-name write supersedes.
    """
    _require_identity(user_id, session_id)
    final_name = _validate_filename(filename, mime_type)

    data = content.encode("utf-8")
    if len(data) > WORKSPACE_WRITE_MAX_BYTES:
        raise WorkspaceValidationError(
            f"Content is {len(data)} bytes; the per-write limit is "
            f"{WORKSPACE_WRITE_MAX_BYTES} bytes"
        )

    repo = get_file_upload_repository()
    quota = await repo.get_user_quota(user_id)
    if quota.total_bytes + len(data) > _USER_QUOTA_BYTES:
        raise WorkspaceQuotaExceededError(
            f"Storage quota exceeded ({quota.total_bytes} of "
            f"{_USER_QUOTA_BYTES} bytes used)"
        )

    bucket = _bucket()
    timestamp_hex = format(int(datetime.now(timezone.utc).timestamp() * 1000), "x")
    upload_id = f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"
    s3_key = f"user-files/{user_id}/{session_id}/{upload_id}/{final_name}"

    await asyncio.to_thread(
        _s3().put_object,
        Bucket=bucket,
        Key=s3_key,
        Body=data,
        ContentType=mime_type,
    )

    metadata = FileMetadata(
        upload_id=upload_id,
        user_id=user_id,
        session_id=session_id,
        filename=final_name,
        mime_type=mime_type,
        size_bytes=len(data),
        s3_key=s3_key,
        s3_bucket=bucket,
        status=FileStatus.READY,
        source=source,
    )
    await repo.create_file(metadata)
    await repo.increment_quota(user_id, len(data))

    download_url = await asyncio.to_thread(
        _s3().generate_presigned_url,
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": s3_key,
            "ResponseContentType": mime_type,
            "ResponseContentDisposition": f'attachment; filename="{final_name}"',
        },
        ExpiresIn=_DOWNLOAD_URL_TTL,
    )

    logger.info(
        f"[workspace_write] {len(data)} bytes → {final_name} "
        f"(upload_id={upload_id}, source={source})"
    )
    return {
        "upload_id": upload_id,
        "filename": final_name,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "size_kb": f"{len(data) / 1024:.1f} KB",
        "download_url": download_url,
    }
