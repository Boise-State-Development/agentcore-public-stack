"""
File Resolver Service

Resolves file upload IDs to FileContent objects with base64-encoded bytes.
Used by chat endpoints to fetch files from S3 before passing to agent.

Metadata first: the row is read before the object, and a reference-class row
(``attachment_plan.delivers_by_reference``) comes back as its metadata alone, with no
``get_object``. A 50 MB GET plus its base64 on the turn path would cost seconds
before the first token, for bytes the turn never sends
(``docs/specs/large-document-attachments.md`` §4B.1).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .repository import get_file_upload_repository
from .attachment_plan import delivers_by_reference
from .models import FileMetadata, FileStatus

logger = logging.getLogger(__name__)


@dataclass
class ResolvedFileContent:
    """
    Resolved file content with base64-encoded bytes.
    Compatible with FileContent from inference_api.chat.models.
    """
    filename: str
    content_type: str
    bytes: str  # base64-encoded; empty for a reference
    # The row of a reference-class upload, which is resolved without its bytes.
    # ``None`` for every file whose bytes were fetched.
    reference: Optional[FileMetadata] = None
    # The row this file was resolved from, for every upload (bytes or not). The
    # route reads it only when an inline-class document falls out of the turn's
    # combined inline budget and goes by reference instead: the row is already
    # in hand, so that costs no second read.
    meta: Optional[FileMetadata] = None
    # An image's compact copy (``image_downscale``), fetched beside its high
    # copy so the planner can send either with no read after it decides.
    # ``None`` when the row has no separate compact copy.
    compact_bytes: Optional[str] = None
    compact_content_type: Optional[str] = None


class FileResolverError(Exception):
    """Error resolving file content."""
    pass


class FileResolver:
    """
    Resolves file upload IDs to FileContent objects.

    Fetches file metadata from DynamoDB and content from S3,
    then encodes as base64 for the agent.
    """

    def __init__(self, s3_client=None):
        self._s3_client = s3_client or boto3.client("s3")
        self._file_repository = get_file_upload_repository()

    async def resolve_files(
        self,
        user_id: str,
        upload_ids: List[str],
        max_files: Optional[int] = 5
    ) -> List[ResolvedFileContent]:
        """
        Resolve upload IDs to file content objects.

        Args:
            user_id: Owner user ID (for authorization)
            upload_ids: List of upload IDs to resolve
            max_files: Backstop on how many IDs are fetched. ``None`` fetches
                every ID given. The chat route applies the per-message cap
                *before* calling this so it can tell the user which files
                were left out; this cap only exists so no caller can fan out
                an unbounded number of S3 reads by accident.

        Returns:
            List of ResolvedFileContent objects with base64-encoded bytes

        Raises:
            FileResolverError: If file not found or access denied
        """
        resolved_files = []

        if max_files is not None and len(upload_ids) > max_files:
            logger.warning(
                "resolve_files truncating %d upload ID(s) to %d",
                len(upload_ids),
                max_files,
            )
            upload_ids = upload_ids[:max_files]

        # Concurrently, in attachment order: twenty files read one after another
        # cost twenty round trips before the first token.
        results = await asyncio.gather(
            *(self._resolve_single_file(user_id, upload_id) for upload_id in upload_ids),
            return_exceptions=True,
        )
        for upload_id, result in zip(upload_ids, results):
            if isinstance(result, BaseException):
                logger.warning(f"Failed to resolve file {upload_id}: {result}")
                # Continue with other files rather than failing entirely
                continue
            if result:
                resolved_files.append(result)

        return resolved_files

    def _get_bytes(self, bucket: str, key: str) -> bytes:
        response = self._s3_client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    async def _resolve_single_file(
        self,
        user_id: str,
        upload_id: str
    ) -> Optional[ResolvedFileContent]:
        """Resolve a single file upload ID."""

        # Get file metadata
        file_meta = await self._file_repository.get_file(user_id, upload_id)

        if not file_meta:
            logger.warning(f"File {upload_id} not found for user {user_id}")
            return None

        if file_meta.status != FileStatus.READY:
            logger.warning(f"File {upload_id} not ready: {file_meta.status}")
            return None

        if delivers_by_reference(file_meta):
            return ResolvedFileContent(
                filename=file_meta.filename,
                content_type=file_meta.mime_type,
                bytes="",
                reference=file_meta,
                meta=file_meta,
            )

        # An image's model copy when the upload stored one (``image_downscale``):
        # the bytes every turn sends for it, byte-identical on every read.
        key, content_type = model_image_source(file_meta)
        compact_key, compact_type = model_image_source(file_meta, "compact")
        fetch_compact = compact_key != key

        # Fetch content from S3, off the event loop; both copies at once.
        try:
            reads = [asyncio.to_thread(self._get_bytes, file_meta.s3_bucket, key)]
            if fetch_compact:
                reads.append(asyncio.to_thread(self._get_bytes, file_meta.s3_bucket, compact_key))
            fetched = await asyncio.gather(*reads, return_exceptions=True)
        except ClientError as e:
            logger.error(f"Failed to fetch file {upload_id} from S3: {e}")
            return None
        if isinstance(fetched[0], BaseException):
            logger.error(f"Failed to fetch file {upload_id} from S3: {fetched[0]}")
            return None
        compact = None
        if fetch_compact and not isinstance(fetched[1], BaseException):
            compact = base64.b64encode(fetched[1]).decode("utf-8")

        return ResolvedFileContent(
            filename=file_meta.filename,
            content_type=content_type,
            bytes=base64.b64encode(fetched[0]).decode("utf-8"),
            meta=file_meta,
            compact_bytes=compact,
            compact_content_type=compact_type if compact else None,
        )


def model_image_source(file_meta: FileMetadata, variant: str = "high") -> tuple:
    """``(key, mime_type)`` of the bytes a model is sent for this row.

    ``variant`` is ``high`` (the default copy) or ``compact`` (the small copy
    the planner sends when the high one does not fit, ``image_downscale``).
    An image's stored copy when its row records one; the original for every
    other row (and for an image uploaded under the ``legacy`` profile). A row
    without a compact copy answers ``compact`` with its high copy.
    """
    record = file_meta.model_image if isinstance(file_meta.model_image, dict) else None
    if not record:
        return file_meta.s3_key, file_meta.mime_type
    if variant == "compact" and isinstance(record.get("compact"), dict):
        compact = record["compact"]
        if compact.get("key") and compact.get("mimeType"):
            return str(compact["key"]), str(compact["mimeType"])
    if record.get("key") and record.get("mimeType"):
        return str(record["key"]), str(record["mimeType"])
    return file_meta.s3_key, file_meta.mime_type


# Global instance
_resolver_instance: Optional[FileResolver] = None


def get_file_resolver() -> FileResolver:
    """Get or create global FileResolver instance."""
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = FileResolver()
    return _resolver_instance
