"""
File Upload Service

Business logic for file uploads with S3 pre-signed URLs and quota management.
"""

import asyncio
import json
import os
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from apis.shared.security.log_sanitize import scrub_log
from apis.shared.timestamps import to_iso

from apis.shared.files.content_disposition import build_content_disposition
from apis.shared.files.models import (
    DELIVERY_INLINE,
    Delivery,
    FileMetadata,
    FileStatus,
    PresignRequest,
    PresignResponse,
    CompleteUploadResponse,
    PreviewUrlResponse,
    SheetPreviewResponse,
    SHEET_PREVIEW_MIME_TYPES,
    TextSnippetResponse,
    ThumbnailResponse,
    THUMBNAIL_SUPPORTED_MIME_TYPES,
    FileResponse,
    FileListResponse,
    QuotaResponse,
    is_allowed_mime_type,
    is_reference_document,
    ALLOWED_EXTENSIONS,
    ALLOWED_MIME_TYPES,
)
from apis.shared.files.attachment_plan import (
    UploadLimits,
    current_limits_profile,
    upload_download_ceiling,
    upload_limits,
    upload_size_limit,
)
from .sheet_preview import (
    MAX_WORKBOOK_BYTES,
    WorkbookTooLargeError,
    WorkbookUnreadableError,
    read_workbook_preview,
)
from .thumbnails import (
    ThumbnailPending,
    ThumbnailRenderer,
    ThumbnailRenderError,
    ThumbnailUnsupportedError,
    get_thumbnail_renderer,
)


# MIME types that are safe to decode as UTF-8 text for inline previews.
TEXT_PREVIEW_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
    }
)
TEXT_SNIPPET_MAX_BYTES = 2048
from apis.shared.files.repository import FileUploadRepository, get_file_upload_repository

logger = logging.getLogger(__name__)


class FileUploadError(Exception):
    """Base exception for file upload errors."""
    pass


class QuotaExceededError(FileUploadError):
    """Raised when user's storage quota is exceeded."""

    def __init__(
        self, current_usage: int, max_allowed: int, required_space: int
    ):
        self.current_usage = current_usage
        self.max_allowed = max_allowed
        self.required_space = required_space
        super().__init__(
            f"Storage quota exceeded: {current_usage}/{max_allowed} bytes, "
            f"need {required_space} more bytes"
        )


class InvalidFileTypeError(FileUploadError):
    """Raised when file type is not allowed."""

    def __init__(self, mime_type: str):
        self.mime_type = mime_type
        allowed = ", ".join(ALLOWED_MIME_TYPES.values())
        super().__init__(
            f"Invalid file type: {mime_type}. Allowed types: {allowed}"
        )


class FileTooLargeError(FileUploadError):
    """Raised when file exceeds size limit."""

    def __init__(self, size_bytes: int, max_size: int):
        self.size_bytes = size_bytes
        self.max_size = max_size
        super().__init__(
            f"File too large: {size_bytes} bytes. Maximum: {max_size} bytes"
        )


class UploadSizeMismatchError(FileUploadError):
    """Raised when the stored object's size differs from the declared size."""

    def __init__(self, declared_bytes: int, stored_bytes: Optional[int]):
        self.declared_bytes = declared_bytes
        self.stored_bytes = stored_bytes
        super().__init__(
            f"Uploaded size {stored_bytes} bytes does not match declared size "
            f"{declared_bytes} bytes"
        )


class ImageUnreadableUploadError(FileUploadError):
    """Raised when an image over the general limit cannot be read to downscale it."""

    def __init__(self, filename: str):
        self.filename = filename
        super().__init__(
            f"{filename} could not be read as an image. Try saving it as a JPEG or PNG and attaching it again."
        )


class FileNotFoundError(FileUploadError):
    """Raised when file is not found."""
    pass


class FileUploadService:
    """
    Service for file upload operations.

    Provides:
    - Pre-signed URL generation for direct S3 uploads
    - File metadata management
    - Quota tracking and enforcement
    - File listing and deletion
    """

    # Sibling key, in the same per-upload S3 "folder" as the original.
    # Stored alongside the original so cleanup happens with the file.
    THUMBNAIL_KEY_NAME = "_thumb.png"

    # Strong refs to fire-and-forget digest builds. The event loop only holds
    # weak references to tasks, so a bare `create_task` can be collected
    # mid-run (the trap web_sources/deletion_service.py documents).
    _digest_tasks: Set["asyncio.Task[None]"] = set()

    def __init__(
        self,
        repository: Optional[FileUploadRepository] = None,
        s3_client=None,
        bucket_name: Optional[str] = None,
        max_file_size: Optional[int] = None,
        presentation_max_file_size: Optional[int] = None,
        spreadsheet_max_file_size: Optional[int] = None,
        max_files_per_message: Optional[int] = None,
        user_quota_bytes: Optional[int] = None,
        thumbnail_renderer: Optional[ThumbnailRenderer] = None,
        sqs_client=None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_file_upload_repository()
        # Built on first use: only a reference-class upload sends to SQS.
        self._sqs_client = sqs_client
        self._thumbnail_renderer = thumbnail_renderer or get_thumbnail_renderer()

        # S3 configuration
        # Use region from AWS_REGION env var to ensure presigned URLs use regional endpoint
        # This is critical for CORS - global endpoint redirects break CORS preflight
        # Force SigV4 signing and regional endpoint to avoid CORS issues with global endpoint
        region = os.environ.get("AWS_REGION", "us-west-2")
        s3_config = Config(
            signature_version='s3v4',
            s3={'addressing_style': 'virtual'},
        )
        self._s3_client = s3_client or boto3.client(
            "s3",
            region_name=region,
            config=s3_config,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
        )
        self.bucket_name = bucket_name or os.environ.get(
            "S3_USER_FILES_BUCKET_NAME", "user-files"
        )

        # Upload limits: the attachment planner's (``upload_limits``), the same
        # numbers ``GET /files/limits`` serves the SPA and the TUI. A
        # constructor value overrides one class (tests). Decks and spreadsheets
        # never go inline, so their ceilings are the tools' (25 MB), not
        # Bedrock's inline document limit.
        planner_limits = upload_limits()
        self.max_file_size = max_file_size or planner_limits.general_max_bytes
        self.presentation_max_file_size = (
            presentation_max_file_size or planner_limits.presentation_max_bytes
        )
        self.spreadsheet_max_file_size = (
            spreadsheet_max_file_size or planner_limits.spreadsheet_max_bytes
        )
        self.max_files_per_message = (
            max_files_per_message or current_limits_profile().max_files_per_message
        )
        self.user_quota_bytes = user_quota_bytes or int(
            os.environ.get("FILE_UPLOAD_USER_QUOTA_BYTES", 1024 * 1024 * 1024)  # 1GB
        )

        # Pre-signed URL expiration
        self.presign_expiration = 15 * 60  # 15 minutes
        self.preview_url_expiration = 10 * 60  # 10 minutes for GET previews

    def upload_limits(self) -> UploadLimits:
        """The planner's upload limits with this service's class overrides.

        Read per call: the large-document and folder flags are inputs to it.
        """
        from dataclasses import replace

        return replace(
            upload_limits(),
            general_max_bytes=self.max_file_size,
            presentation_max_bytes=self.presentation_max_file_size,
            spreadsheet_max_bytes=self.spreadsheet_max_file_size,
        )

    def max_size_for(self, filename: str, mime_type: str) -> int:
        """Return the upload size cap that applies to this file.

        The planner decides (``upload_size_limit``): decks and spreadsheets get
        their own caps, a document-class file gets the large-document ceiling
        while large-document attachments are on, everything else the general
        limit. Callers must use this rather than reading ``max_file_size``
        directly, or a file under its class's cap gets rejected by whichever
        gate was missed (presign and ``complete_upload`` both read it).
        """
        return upload_size_limit(filename, mime_type, self.upload_limits())

    # =========================================================================
    # Pre-signed URL Flow
    # =========================================================================

    async def request_presigned_url(
        self, user_id: str, request: PresignRequest
    ) -> PresignResponse:
        """
        Generate a pre-signed URL for file upload.

        This is Phase 1 of the upload flow:
        1. Validate file type and size
        2. Check user quota
        3. Generate ULID for upload
        4. Create pending metadata record
        5. Generate pre-signed URL

        Args:
            user_id: The uploading user's ID
            request: PresignRequest with file details

        Returns:
            PresignResponse with upload ID and pre-signed URL

        Raises:
            InvalidFileTypeError: If MIME type not allowed
            FileTooLargeError: If file exceeds size limit
            QuotaExceededError: If user quota would be exceeded
        """
        # Validate MIME type
        if not is_allowed_mime_type(request.mime_type):
            raise InvalidFileTypeError(request.mime_type)

        # Validate file size against the cap for this file's class
        size_limit = self.max_size_for(request.filename, request.mime_type)
        if request.size_bytes > size_limit:
            raise FileTooLargeError(request.size_bytes, size_limit)

        # Check quota
        quota = await self.repository.get_user_quota(user_id)
        projected_usage = quota.total_bytes + request.size_bytes
        if projected_usage > self.user_quota_bytes:
            raise QuotaExceededError(
                current_usage=quota.total_bytes,
                max_allowed=self.user_quota_bytes,
                required_space=request.size_bytes,
            )

        upload_id = self._new_upload_id()
        s3_key = self._s3_key_for(user_id, request.session_id, upload_id, request.filename)

        # Create pending metadata record
        file_meta = FileMetadata(
            upload_id=upload_id,
            user_id=user_id,
            session_id=request.session_id,
            filename=request.filename,
            mime_type=request.mime_type,
            size_bytes=request.size_bytes,
            s3_key=s3_key,
            s3_bucket=self.bucket_name,
            status=FileStatus.PENDING,
        )
        await self.repository.create_file(file_meta)

        # Generate pre-signed URL
        # Note: Don't include ContentLength in signed params - browsers set this
        # automatically and can't be overridden via XHR. Including it causes
        # SignatureDoesNotMatch errors if there's any size discrepancy.
        try:
            presigned_url = self._s3_client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": s3_key,
                    "ContentType": request.mime_type,
                },
                ExpiresIn=self.presign_expiration,
            )
        except ClientError as e:
            # Clean up metadata on failure
            await self.repository.delete_file(user_id, upload_id)
            logger.error(f"Failed to generate pre-signed URL: {e}")
            raise

        expires_at = to_iso(datetime.now(timezone.utc) + timedelta(seconds=self.presign_expiration))

        logger.info(f"Generated pre-signed URL for upload {upload_id} by user {user_id}")

        return PresignResponse(
            upload_id=upload_id,
            presigned_url=presigned_url,
            expires_at=expires_at,
        )

    async def complete_upload(
        self, user_id: str, upload_id: str
    ) -> CompleteUploadResponse:
        """
        Mark an upload as complete after successful S3 upload.

        This is Phase 2 of the upload flow:
        1. Verify file exists in pending state
        2. Verify S3 object exists (HEAD request)
        3. Verify the stored size is within the cap and matches the declared size
        4. Update status to READY
        5. Increment user quota

        Args:
            user_id: The uploading user's ID
            upload_id: The upload identifier

        Returns:
            CompleteUploadResponse with file details

        Raises:
            FileNotFoundError: If upload not found or not owned by user
            FileTooLargeError: If the stored object exceeds the size cap
            UploadSizeMismatchError: If the stored size differs from the declared size
            FileUploadError: If upload not in pending state
        """
        # Get file metadata
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            raise FileNotFoundError(f"Upload {upload_id} not found")

        if file_meta.status != FileStatus.PENDING:
            raise FileUploadError(
                f"Upload {upload_id} is already {file_meta.status}, cannot complete"
            )

        # Verify S3 object exists
        try:
            head = self._s3_client.head_object(
                Bucket=self.bucket_name,
                Key=file_meta.s3_key,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                # File not uploaded yet - mark as failed
                await self.repository.update_file_status(
                    user_id, upload_id, FileStatus.FAILED
                )
                raise FileNotFoundError(
                    f"S3 object not found for upload {upload_id}"
                )
            raise

        # The presigned PUT does not sign Content-Length, so the presign-time
        # size check only saw what the client declared. Hold the stored object
        # to the cap and to that declaration before it counts against quota.
        stored_size = head.get("ContentLength")
        size_limit = self.max_size_for(file_meta.filename, file_meta.mime_type)
        if stored_size is not None and stored_size > size_limit:
            await self._reject_upload(file_meta)
            raise FileTooLargeError(stored_size, size_limit)
        if stored_size != file_meta.size_bytes:
            await self._reject_upload(file_meta)
            raise UploadSizeMismatchError(file_meta.size_bytes, stored_size)

        # An image's model copy, before READY, so no turn reads it without one.
        try:
            await self._store_model_image(file_meta)
        except ImageUnreadableUploadError:
            await self._reject_upload(file_meta)
            raise

        # Update status to READY
        await self.repository.update_file_status(
            user_id, upload_id, FileStatus.READY
        )

        # Increment quota
        await self.repository.increment_quota(user_id, file_meta.size_bytes)

        await self._after_upload_ready(file_meta)

        logger.info("Completed file upload")

        return CompleteUploadResponse(
            upload_id=upload_id,
            status=FileStatus.READY.value,
            s3_uri=file_meta.s3_uri,
            filename=file_meta.filename,
            size_bytes=file_meta.size_bytes,
        )

    # =========================================================================
    # Import from a file source (the composer's "Add from Google Drive")
    # =========================================================================

    def max_download_bytes(self) -> int:
        """The largest size any upload class may be: the cap to download under.

        A file-source import is sized only once its bytes are in hand (an
        exported Google Doc has no size until it streams), so the adapter is
        told the widest cap and `import_file` applies the file's own class
        cap afterwards. Bounds memory to one over-cap file at most.
        """
        return upload_download_ceiling(self.upload_limits())

    async def import_file(
        self,
        user_id: str,
        session_id: str,
        filename: str,
        mime_type: str,
        content: bytes,
        source: str = "upload",
        delivery: Delivery = DELIVERY_INLINE,
    ) -> FileMetadata:
        """Store bytes fetched server-side as a ready attachment.

        The presign flow in one call, for a file whose bytes app-api already
        holds (downloaded from a connected file source): the same type, size
        and quota gates as `request_presigned_url`, then the object is PUT
        from here and the row goes straight to READY with the same post-ready
        step as `complete_upload` (digest or large-document preparation).

        A provider's MIME type is taken at its word when it is one we accept,
        and resolved from the filename's extension otherwise, exactly as the
        SPA does for a device upload whose browser reported no type.

        `delivery` is stored on the row (drive-folder spec §4.3): `reference`
        makes a document-class file reference-class whatever its size, so the
        post-ready step enqueues it for preparation instead of digesting it
        here. Callers pass it only while `LARGE_DOCUMENT_ATTACHMENTS_ENABLED`
        is on; without the prepare pipeline there is nothing to read a
        reference through.

        Raises:
            InvalidFileTypeError: neither the MIME type nor the extension is accepted
            FileTooLargeError: over the cap for its class
            QuotaExceededError: would exceed the user's storage quota
            FileUploadError: the object could not be stored
        """
        resolved_mime = self._resolve_import_mime_type(filename, mime_type)
        size_bytes = len(content)

        size_limit = self.max_size_for(filename, resolved_mime)
        if size_bytes > size_limit:
            raise FileTooLargeError(size_bytes, size_limit)

        quota = await self.repository.get_user_quota(user_id)
        if quota.total_bytes + size_bytes > self.user_quota_bytes:
            raise QuotaExceededError(
                current_usage=quota.total_bytes,
                max_allowed=self.user_quota_bytes,
                required_space=size_bytes,
            )

        upload_id = self._new_upload_id()
        s3_key = self._s3_key_for(user_id, session_id, upload_id, filename)
        file_meta = FileMetadata(
            upload_id=upload_id,
            user_id=user_id,
            session_id=session_id,
            filename=filename,
            mime_type=resolved_mime,
            size_bytes=size_bytes,
            s3_key=s3_key,
            s3_bucket=self.bucket_name,
            status=FileStatus.PENDING,
            source=source,
            delivery=delivery,
        )
        # PENDING first, as a presigned upload would be, so a PUT that fails
        # leaves a row the existing cleanup paths already understand.
        await self.repository.create_file(file_meta)

        try:
            await asyncio.to_thread(
                self._s3_client.put_object,
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=content,
                ContentType=resolved_mime,
            )
        except ClientError as e:
            logger.error(f"Failed to store imported file {upload_id}: {e}")
            await self.repository.update_file_status(user_id, upload_id, FileStatus.FAILED)
            raise FileUploadError("The file could not be stored. Try again.")

        try:
            await self._store_model_image(file_meta, raw=content)
        except ImageUnreadableUploadError:
            await self._reject_upload(file_meta)
            raise

        await self.repository.update_file_status(user_id, upload_id, FileStatus.READY)
        file_meta.status = FileStatus.READY
        await self.repository.increment_quota(user_id, size_bytes)
        await self._after_upload_ready(file_meta)

        logger.info("Imported file from a file source")
        return file_meta

    async def replace_file_content(
        self,
        file_meta: FileMetadata,
        content: bytes,
        mime_type: str,
    ) -> FileMetadata:
        """New bytes for an existing READY file, at the **same upload id and key**.

        A refreshed folder file (drive-folder spec §4.6): the conversation may
        cite this upload id, so the id, key, name and ``delivery`` stay and only
        the bytes change. Whatever described the old bytes goes (thumbnail,
        ``_derived/`` sidecars, digest, preparation) and the post-ready step runs
        again, so a reference document is re-prepared and, when indexed,
        re-ingested under its same KB document id. The quota moves by the size
        difference, never by a file.

        Raises:
            FileTooLargeError: the new bytes are over the file's class cap
            QuotaExceededError: the growth would exceed the user's quota
            FileNotFoundError: the row is gone or no longer READY
            FileUploadError: the object could not be stored
        """
        size_bytes = len(content)
        size_limit = self.max_size_for(file_meta.filename, file_meta.mime_type)
        if size_bytes > size_limit:
            raise FileTooLargeError(size_bytes, size_limit)

        delta = size_bytes - file_meta.size_bytes
        if delta > 0:
            quota = await self.repository.get_user_quota(file_meta.user_id)
            if quota.total_bytes + delta > self.user_quota_bytes:
                raise QuotaExceededError(
                    current_usage=quota.total_bytes,
                    max_allowed=self.user_quota_bytes,
                    required_space=delta,
                )

        try:
            await asyncio.to_thread(
                self._s3_client.put_object,
                Bucket=file_meta.s3_bucket,
                Key=file_meta.s3_key,
                Body=content,
                ContentType=file_meta.mime_type,
            )
        except ClientError as e:
            logger.error(f"Failed to store replaced content for {file_meta.upload_id}: {e}")
            raise FileUploadError("The file could not be stored. Try again.")

        self._delete_thumbnail_object(file_meta)
        self._delete_derived_objects(file_meta)
        updated = await self.repository.replace_file_content_fields(
            file_meta.user_id, file_meta.upload_id, size_bytes
        )
        if updated is None:
            raise FileNotFoundError(f"File '{file_meta.upload_id}' is gone")
        # A refreshed image gets a new model copy; an unreadable one is sent as is.
        try:
            updated = await self._store_model_image(updated, raw=content)
        except ImageUnreadableUploadError:
            logger.warning("A replaced image could not be read; it is sent as uploaded")
        if delta:
            await self.repository.adjust_quota_bytes(file_meta.user_id, delta)
        await self._after_upload_ready(updated)
        logger.info("Replaced a file's content in place")
        return updated

    @staticmethod
    def _resolve_import_mime_type(filename: str, mime_type: str) -> str:
        """An accepted MIME type for an imported file, or raise."""
        if is_allowed_mime_type(mime_type):
            return mime_type
        lower = filename.lower()
        for ext, resolved in ALLOWED_EXTENSIONS.items():
            if lower.endswith(ext):
                return resolved
        raise InvalidFileTypeError(mime_type or "unknown")

    @staticmethod
    def _new_upload_id() -> str:
        """A timestamp-prefixed id (`{ms_hex}_{uuid16}`) so ids sort by time."""
        timestamp_hex = format(int(datetime.now(timezone.utc).timestamp() * 1000), 'x')
        return f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _s3_key_for(user_id: str, session_id: str, upload_id: str, filename: str) -> str:
        return f"user-files/{user_id}/{session_id}/{upload_id}/{filename}"

    async def _store_model_image(
        self, file_meta: FileMetadata, raw: Optional[bytes] = None
    ) -> FileMetadata:
        """Plan and store an image's model copy, and record it on the row.

        Only under a profile that downscales (``token``); every other file and
        profile returns the row unchanged, so the ``legacy`` profile sends an
        image exactly as uploaded. The copy is deterministic
        (``image_downscale``): the same bytes always store the same object, at
        ``_derived/model-image.<ext>``.

        An image that cannot be decoded is still accepted when it is within
        the general upload limit (it is sent as uploaded, as before); over that
        limit there is nothing a model could be sent, so it raises
        :class:`ImageUnreadableUploadError` and the caller rejects the upload.
        """
        if not current_limits_profile().downscale_images:
            return file_meta
        if not (file_meta.mime_type or "").lower().startswith("image/"):
            return file_meta
        from apis.shared.files import image_downscale

        if raw is None:
            try:
                response = await asyncio.to_thread(
                    self._s3_client.get_object, Bucket=file_meta.s3_bucket, Key=file_meta.s3_key
                )
                raw = await asyncio.to_thread(response["Body"].read)
            except ClientError:
                logger.warning("Could not read an image to plan its model copy", exc_info=True)
                return file_meta

        try:
            info, copy = await asyncio.to_thread(image_downscale.plan_model_image, raw, file_meta.mime_type)
        except image_downscale.ImageUnreadableError:
            if len(raw) > self.max_file_size:
                raise ImageUnreadableUploadError(file_meta.filename)
            logger.info("An image could not be decoded; it is sent as uploaded")
            return file_meta

        if copy is None:
            record = image_downscale.model_image_record(
                key=file_meta.s3_key,
                mime_type=file_meta.mime_type,
                size_bytes=len(raw),
                width=info.width,
                height=info.height,
                source=info,
                downscaled=False,
            )
        else:
            key = image_downscale.model_image_key(file_meta.s3_key, copy.mime_type)
            try:
                await asyncio.to_thread(
                    self._s3_client.put_object,
                    Bucket=file_meta.s3_bucket,
                    Key=key,
                    Body=copy.data,
                    ContentType=copy.mime_type,
                )
            except ClientError:
                logger.error("Could not store an image's model copy", exc_info=True)
                if len(raw) > self.max_file_size:
                    raise FileUploadError("The image could not be prepared. Try again.")
                return file_meta
            record = image_downscale.model_image_record(
                key=key,
                mime_type=copy.mime_type,
                size_bytes=len(copy.data),
                width=copy.width,
                height=copy.height,
                source=info,
                downscaled=True,
            )
        updated = await self.repository.update_file_model_image(
            file_meta.user_id, file_meta.upload_id, record
        )
        file_meta.model_image = record
        logger.info(
            "Planned an image's model copy: %sx%s -> %sx%s, %s bytes",
            info.width,
            info.height,
            record["width"],
            record["height"],
            record["sizeBytes"],
        )
        return updated or file_meta

    async def _after_upload_ready(self, file_meta: FileMetadata) -> None:
        """What every READY upload gets, however its bytes arrived.

        DocumentDigest, off the request path (offload spec §4A / PR-2):
        documents get an outline + abstract persisted on their row so a
        later turn can carry the digest instead of the bytes. Non-documents
        (spreadsheets, decks, images) and the kill switch skip it. The
        response does not wait for it.

        A reference-class document (large-document spec §4A) is never read
        here: the preparation Lambda builds its digest along with the
        sidecars, so app-api never holds a 50 MB object in memory.

        Every other document is also sent to the same Lambda for its file
        facts only (unified-attachment-delivery spec §4.1, §7): no sidecars, no
        ``preparation`` and no indexing, so its delivery is unchanged.
        """
        if is_reference_document(file_meta):
            await self._enqueue_preparation(file_meta)
        else:
            self._schedule_digest(file_meta)
            await self._enqueue_facts(file_meta)
            await self._enqueue_preview(file_meta)

    # =========================================================================
    # Preview URL
    # =========================================================================

    async def get_preview_url(
        self, user_id: str, upload_id: str
    ) -> PreviewUrlResponse:
        """
        Generate a short-lived presigned GET URL for displaying a file.

        Used by the UI to render image thumbnails inline and to power the
        full-size lightbox. Only owners can generate preview URLs for their
        own files. Files must be in READY state.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            PreviewUrlResponse with a presigned GET URL

        Raises:
            FileNotFoundError: If file not found, not owned by user, or not ready
        """
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            raise FileNotFoundError(f"File {upload_id} not found")

        if file_meta.status != FileStatus.READY:
            raise FileNotFoundError(
                f"File {upload_id} is not ready (status: {file_meta.status})"
            )

        try:
            url = self._s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": file_meta.s3_key,
                    "ResponseContentType": file_meta.mime_type,
                    "ResponseContentDisposition": build_content_disposition(
                        "inline", file_meta.filename
                    ),
                },
                ExpiresIn=self.preview_url_expiration,
            )
        except ClientError as e:
            logger.error(f"Failed to generate preview URL: {e}")
            raise

        expires_at = to_iso(
            datetime.now(timezone.utc) + timedelta(seconds=self.preview_url_expiration)
        )

        return PreviewUrlResponse(
            upload_id=upload_id,
            url=url,
            expires_at=expires_at,
            mime_type=file_meta.mime_type,
            filename=file_meta.filename,
        )

    async def get_download_url(self, user_id: str, upload_id: str) -> str:
        """Mint a short-lived presigned GET URL that forces a download.

        Same ownership + READY gate as `get_preview_url`, but the response
        headers carry `Content-Disposition: attachment` so the browser saves
        the file instead of rendering it. Minted per request and immediately
        redirected to, so the caller never holds an expiring URL — that is the
        whole point of the `/files/{id}/download` route: a stable, durable link
        that can be persisted in a conversation and still work a week later.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            A presigned S3 GET URL with an attachment disposition.

        Raises:
            FileNotFoundError: If file not found, not owned by user, or not ready
        """
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            raise FileNotFoundError(f"File {upload_id} not found")

        if file_meta.status != FileStatus.READY:
            raise FileNotFoundError(
                f"File {upload_id} is not ready (status: {file_meta.status})"
            )

        try:
            return self._s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": file_meta.s3_key,
                    "ResponseContentType": file_meta.mime_type,
                    "ResponseContentDisposition": build_content_disposition(
                        "attachment", file_meta.filename
                    ),
                },
                ExpiresIn=self.preview_url_expiration,
            )
        except ClientError as e:
            logger.error(f"Failed to generate download URL: {e}")
            raise

    async def get_text_snippet(
        self, user_id: str, upload_id: str
    ) -> TextSnippetResponse:
        """
        Return a short UTF-8 text excerpt from the start of a file.

        Used by the UI to render a content peek inside the document-style
        attachment card. Only text-based MIME types are supported; other
        types return an empty snippet so the UI can fall back to a skeleton.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            TextSnippetResponse with the decoded snippet (possibly empty)

        Raises:
            FileNotFoundError: If file not found, not owned, or not ready
        """
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            raise FileNotFoundError(f"File {upload_id} not found")

        if file_meta.status != FileStatus.READY:
            raise FileNotFoundError(
                f"File {upload_id} is not ready (status: {file_meta.status})"
            )

        if file_meta.mime_type not in TEXT_PREVIEW_MIME_TYPES:
            return TextSnippetResponse(
                upload_id=upload_id,
                snippet="",
                truncated=False,
                mime_type=file_meta.mime_type,
            )

        # Read up to TEXT_SNIPPET_MAX_BYTES + 1 bytes so we can detect truncation.
        try:
            response = self._s3_client.get_object(
                Bucket=self.bucket_name,
                Key=file_meta.s3_key,
                Range=f"bytes=0-{TEXT_SNIPPET_MAX_BYTES}",
            )
            body = response["Body"].read()
        except ClientError as e:
            logger.warning(f"Failed to read snippet for {scrub_log(upload_id)}: {scrub_log(e)}")
            return TextSnippetResponse(
                upload_id=upload_id,
                snippet="",
                truncated=False,
                mime_type=file_meta.mime_type,
            )

        truncated = file_meta.size_bytes > TEXT_SNIPPET_MAX_BYTES
        excerpt = body[:TEXT_SNIPPET_MAX_BYTES]
        try:
            text = excerpt.decode("utf-8")
        except UnicodeDecodeError:
            # Trim trailing partial multi-byte sequence and retry; fall back to replace.
            text = excerpt.decode("utf-8", errors="replace")

        return TextSnippetResponse(
            upload_id=upload_id,
            snippet=text,
            truncated=truncated,
            mime_type=file_meta.mime_type,
        )

    # =========================================================================
    # Spreadsheet preview
    # =========================================================================

    async def get_sheet_preview(
        self, user_id: str, upload_id: str
    ) -> SheetPreviewResponse:
        """Read an .xlsx into rows the UI can draw in its data grid.

        The workbook never reaches the browser. Unlike the `.docx`,
        `.pptx` and `.csv` previews — which fetch the bytes through a
        presigned URL and parse them client-side — there is no
        client-side spreadsheet reader we are willing to ship, so the
        parse happens here and only values cross the wire.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            SheetPreviewResponse with one entry per visible worksheet

        Raises:
            FileNotFoundError: not found, not owned, or not ready
            ThumbnailUnsupportedError: MIME type is not a readable workbook
            WorkbookTooLargeError: past the reader's byte cap
            WorkbookUnreadableError: corrupt, encrypted, or not OOXML
        """
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            raise FileNotFoundError(f"File {upload_id} not found")

        if file_meta.status != FileStatus.READY:
            raise FileNotFoundError(
                f"File {upload_id} is not ready (status: {file_meta.status})"
            )

        if file_meta.mime_type not in SHEET_PREVIEW_MIME_TYPES:
            raise ThumbnailUnsupportedError(
                f"No spreadsheet reader for {file_meta.mime_type}"
            )

        # Checked before the download so an oversized workbook costs a
        # metadata read rather than a transfer into memory.
        if file_meta.size_bytes > MAX_WORKBOOK_BYTES:
            raise WorkbookTooLargeError(file_meta.size_bytes)

        try:
            response = self._s3_client.get_object(
                Bucket=self.bucket_name,
                Key=file_meta.s3_key,
            )
            data = response["Body"].read()
        except ClientError as e:
            logger.warning(
                f"Failed to read workbook {scrub_log(upload_id)}: {scrub_log(e)}"
            )
            raise FileNotFoundError(f"File {upload_id} could not be read")

        # openpyxl is CPU-bound and blocking, so it runs off the event
        # loop. A 20 MB workbook parses for long enough to stall every
        # other request on this worker if it does not.
        sheets = await asyncio.to_thread(read_workbook_preview, data)

        return SheetPreviewResponse(
            upload_id=upload_id,
            filename=file_meta.filename,
            sheets=sheets,
            truncated=any(sheet.truncated for sheet in sheets),
        )

    # =========================================================================
    # Thumbnails
    # =========================================================================

    def _thumbnail_s3_key(self, file_meta: FileMetadata) -> str:
        """
        Derive the sibling thumbnail key for an original file.

        Originals live at ``user-files/{user}/{session}/{upload_id}/{filename}``,
        thumbnails at ``user-files/{user}/{session}/{upload_id}/_thumb.png`` —
        same parent prefix so cleanup paths can find both.
        """
        base, _, _ = file_meta.s3_key.rpartition("/")
        return f"{base}/{self.THUMBNAIL_KEY_NAME}"

    async def get_or_create_thumbnail(
        self, user_id: str, upload_id: str
    ) -> ThumbnailResponse:
        """
        Return a presigned URL for a PNG thumbnail of the file's first page.

        Serves the prepare Lambda's ``_derived/thumb.png``, or the
        ``_thumb.png`` this service rendered before that existed. When neither
        exists it queues a render (``ThumbnailPending``) rather than running
        PDFium here; only a deployment without the prepare queue still renders
        in-process.

        Args:
            user_id: The owner's user ID.
            upload_id: The upload identifier.

        Returns:
            ThumbnailResponse with a presigned GET URL and ``cached`` flag.

        Raises:
            FileNotFoundError: File not found, not owned, or not ready.
            ThumbnailUnsupportedError: MIME type has no registered renderer.
            ThumbnailPending: Not rendered yet; the prepare Lambda has been asked.
            ThumbnailRenderError: The file was unreadable / corrupt / encrypted.
        """
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            raise FileNotFoundError(f"File {upload_id} not found")

        if file_meta.status != FileStatus.READY:
            raise FileNotFoundError(
                f"File {upload_id} is not ready (status: {file_meta.status})"
            )

        if file_meta.mime_type not in THUMBNAIL_SUPPORTED_MIME_TYPES:
            raise ThumbnailUnsupportedError(
                f"No thumbnail renderer for {file_meta.mime_type}"
            )

        thumb_key = await self._find_thumbnail(file_meta)
        cached = thumb_key is not None
        if thumb_key is None:
            if await self._queue_thumbnail(file_meta):
                raise ThumbnailPending(f"Thumbnail for {upload_id} is being rendered")
            # No prepare queue in this deployment: render here, as before.
            thumb_key = self._thumbnail_s3_key(file_meta)
            await self._render_and_store_thumbnail(file_meta, thumb_key)

        # Generate the presigned URL for the thumbnail. Use the same
        # expiration window as preview URLs so the UI's caching expectations
        # line up.
        try:
            url = self._s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": thumb_key,
                    "ResponseContentType": "image/png",
                },
                ExpiresIn=self.preview_url_expiration,
            )
        except ClientError as e:
            logger.error(f"Failed to generate thumbnail presigned URL: {e}")
            raise

        expires_at = to_iso(
            datetime.now(timezone.utc) + timedelta(seconds=self.preview_url_expiration)
        )

        return ThumbnailResponse(
            upload_id=upload_id,
            url=url,
            expires_at=expires_at,
            cached=cached,
        )

    async def _find_thumbnail(self, file_meta: FileMetadata) -> Optional[str]:
        """The key of a stored thumbnail, or ``None``.

        The prepare Lambda's ``_derived/thumb.png`` first, then the
        ``_thumb.png`` this service rendered for files uploaded before it.
        """
        from apis.shared.files.pdf_thumbnail import thumbnail_key

        for key in (thumbnail_key(file_meta.s3_key), self._thumbnail_s3_key(file_meta)):
            try:
                await asyncio.to_thread(self._s3_client.head_object, Bucket=self.bucket_name, Key=key)
                return key
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                    logger.error(f"HEAD failed for thumbnail {key}: {e}")
                    raise
        return None

    async def _queue_thumbnail(self, file_meta: FileMetadata) -> bool:
        """Ask the prepare Lambda to render this file's first page.

        True when this deployment has the prepare queue, whether or not the
        send succeeded: with the queue, app-api never runs PDFium itself, and
        the next view asks again.
        """
        from apis.shared.feature_flags import large_document_attachments_enabled

        queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
        if not large_document_attachments_enabled() or not queue_url:
            return False
        try:
            body = json.dumps(
                {"action": "thumbnail", "user_id": file_meta.user_id, "upload_id": file_meta.upload_id}
            )
            await asyncio.to_thread(self._sqs().send_message, QueueUrl=queue_url, MessageBody=body)
        except Exception:  # noqa: BLE001 - the card keeps its skeleton
            logger.warning("Thumbnail render not queued", exc_info=True)
        return True

    @staticmethod
    def _thumbnail_source_key(file_meta: FileMetadata) -> str:
        """The object page 1 is rendered from.

        The original, except for a reference-class document (up to 50 MB),
        whose first page shard is read instead: a few hundred KB rather than
        the whole original in app-api's memory. Until preparation has written
        the shards there is nothing small to read, so the card keeps its
        skeleton (``ThumbnailRenderError`` → 422) rather than pulling 50 MB.
        """
        from apis.shared.files import large_document

        if not is_reference_document(file_meta):
            return file_meta.s3_key
        if not large_document.is_prepared(file_meta):
            raise ThumbnailRenderError("Large document is not prepared yet")
        shards = large_document.shard_ranges(
            large_document.prepared_page_count(file_meta),
            large_document.prepared_shard_pages(file_meta),
        )
        if not shards:
            raise ThumbnailRenderError("Large document has no page shards")
        start, end = shards[0]
        return large_document.shard_key(file_meta.s3_key, start, end)

    async def _render_and_store_thumbnail(
        self, file_meta: FileMetadata, thumb_key: str
    ) -> None:
        """Stream the original (or a large document's first page shard) from
        S3, rasterize page 1, store the PNG."""
        source_key = self._thumbnail_source_key(file_meta)
        try:
            response = self._s3_client.get_object(
                Bucket=self.bucket_name,
                Key=source_key,
            )
            raw = response["Body"].read()
        except ClientError as e:
            logger.error(f"Failed to read source for thumbnail {file_meta.upload_id}: {e}")
            raise ThumbnailRenderError(f"Failed to read source: {e}") from e

        # Run the CPU-bound render off the event loop so we don't stall the
        # request worker. pypdfium2 releases the GIL for the heavy bits.
        loop = asyncio.get_event_loop()
        png_bytes = await loop.run_in_executor(
            None,
            self._thumbnail_renderer.render,
            file_meta.mime_type,
            raw,
        )

        try:
            self._s3_client.put_object(
                Bucket=self.bucket_name,
                Key=thumb_key,
                Body=png_bytes,
                ContentType="image/png",
            )
        except ClientError as e:
            logger.error(f"Failed to write thumbnail {thumb_key}: {e}")
            raise

        logger.info(
            f"Rendered thumbnail for upload {file_meta.upload_id} "
            f"({len(png_bytes)} bytes)"
        )

    # =========================================================================
    # DocumentDigest (docs/specs/document-context-offload.md §4A, PR-2)
    # =========================================================================

    def _schedule_digest(self, file_meta: FileMetadata) -> Optional["asyncio.Task[None]"]:
        """Queue a digest build for a document upload; ``None`` when skipped.

        Skipped for non-documents, when ``DOCUMENT_DIGEST_ENABLED=false``, or
        when no event loop is running (a synchronous caller). Never raises.
        """
        try:
            from apis.shared.files.document_digest import document_digest_enabled
            from apis.shared.files.document_read import is_document_class

            if not document_digest_enabled():
                return None
            if not is_document_class(file_meta.mime_type, file_meta.filename):
                return None
            task = asyncio.get_running_loop().create_task(self._generate_and_store_digest(file_meta))
        except Exception:  # noqa: BLE001 - scheduling must never fail the upload
            logger.warning("Document digest not scheduled", exc_info=True)
            return None
        self._digest_tasks.add(task)
        task.add_done_callback(self._digest_tasks.discard)
        return task

    async def _generate_and_store_digest(self, file_meta: FileMetadata) -> None:
        """Read the original, build the digest, persist it. Never raises."""
        from apis.shared.files.document_digest import build_digest, record_digest

        try:
            response = await asyncio.to_thread(
                self._s3_client.get_object, Bucket=self.bucket_name, Key=file_meta.s3_key
            )
            raw = await asyncio.to_thread(response["Body"].read)
            digest = await build_digest(
                raw=raw,
                mime_type=file_meta.mime_type,
                filename=file_meta.filename,
                upload_id=file_meta.upload_id,
            )
            record_digest(digest)
            stored = await self.repository.update_file_digest(
                file_meta.user_id, file_meta.upload_id, digest.to_item()
            )
            if stored is None:
                logger.info("Document digest discarded: file row no longer exists")
        except Exception:  # noqa: BLE001 - a digest is never worth an error
            logger.warning("Document digest build failed", exc_info=True)

    # =========================================================================
    # Large-document preparation (docs/specs/large-document-attachments.md §4C)
    # =========================================================================

    def _sqs(self):
        if self._sqs_client is None:
            self._sqs_client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        return self._sqs_client

    async def _enqueue_preparation(self, file_meta: FileMetadata) -> None:
        """Queue a reference-class upload for the preparation Lambda. Never raises.

        ``preparation = pending`` is written *before* the message is sent, so
        the Lambda's ``ready`` can never be overwritten by it. A send that
        fails marks the row ``failed``; ``document_read`` then falls back to
        the original where it can, and the upload itself still succeeds.
        """
        from apis.shared.feature_flags import large_document_attachments_enabled
        from apis.shared.files.large_document import STATUS_FAILED, STATUS_PENDING, preparation_record

        queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
        if not large_document_attachments_enabled() or not queue_url:
            # Unreachable while the presign cap is the inline limit; logged so a
            # misconfiguration (flag on without the queue) is visible.
            logger.warning("Large document not queued for preparation: feature off or queue unset")
            return
        try:
            stored = await self.repository.update_file_preparation(
                file_meta.user_id, file_meta.upload_id, preparation_record(status=STATUS_PENDING)
            )
            if stored is None:
                return
            body = json.dumps({"user_id": file_meta.user_id, "upload_id": file_meta.upload_id})
            await asyncio.to_thread(self._sqs().send_message, QueueUrl=queue_url, MessageBody=body)
        except Exception as e:  # noqa: BLE001 - preparation is never worth a failed upload
            logger.warning("Large document preparation not queued", exc_info=True)
            try:
                await self.repository.update_file_preparation(
                    file_meta.user_id, file_meta.upload_id,
                    preparation_record(status=STATUS_FAILED, error=type(e).__name__),
                )
            except Exception:  # noqa: BLE001
                logger.warning("Could not record the failed preparation", exc_info=True)

    async def _enqueue_facts(self, file_meta: FileMetadata) -> None:
        """Queue a document under the inline limit for its file facts. Never raises.

        Only while large-document attachments are on and the queue exists;
        otherwise the row simply has no facts, which every reader handles.
        Nothing is written to the row here: a lost message leaves it without
        facts, the same state as the flag off.
        """
        from apis.shared.feature_flags import large_document_attachments_enabled
        from apis.shared.files.document_read import is_document_class

        queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
        if not large_document_attachments_enabled() or not queue_url:
            return
        if not is_document_class(file_meta.mime_type, file_meta.filename):
            return
        try:
            body = json.dumps(
                {"action": "facts", "user_id": file_meta.user_id, "upload_id": file_meta.upload_id}
            )
            await asyncio.to_thread(self._sqs().send_message, QueueUrl=queue_url, MessageBody=body)
        except Exception:  # noqa: BLE001 - facts are never worth a failed upload
            logger.warning("File facts not queued", exc_info=True)

    @staticmethod
    def _has_preview_format(file_meta: FileMetadata) -> bool:
        from apis.shared.files.attachment_preview import preview_format

        return preview_format(file_meta.filename, file_meta.mime_type) is not None

    async def _enqueue_preview(self, file_meta: FileMetadata) -> None:
        """Queue a spreadsheet, CSV or deck for its card preview. Never raises.

        A Word file's preview is written by its facts or preparation pass;
        these formats are not document-class, so nothing else reaches the
        Lambda for them. Same queue and gate as the facts message.
        """
        from apis.shared.feature_flags import large_document_attachments_enabled
        from apis.shared.files.attachment_preview import preview_format
        from apis.shared.files.document_read import is_document_class

        queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
        if not large_document_attachments_enabled() or not queue_url:
            return
        if preview_format(file_meta.filename, file_meta.mime_type) is None:
            return
        if is_document_class(file_meta.mime_type, file_meta.filename):
            return  # its facts pass writes the preview
        try:
            body = json.dumps(
                {"action": "preview", "user_id": file_meta.user_id, "upload_id": file_meta.upload_id}
            )
            await asyncio.to_thread(self._sqs().send_message, QueueUrl=queue_url, MessageBody=body)
        except Exception:  # noqa: BLE001 - a preview is never worth a failed upload
            logger.warning("Attachment preview not queued", exc_info=True)

    async def get_previews(self, user_id: str, upload_ids: List[str]) -> Dict[str, Optional[dict]]:
        """The card preview of each of ``upload_ids``, or ``None``.

        Owner-scoped: an id that is not the caller's, not ready or has no
        preview format answers ``None``, the same as one not written yet, so
        the answer says nothing about another user's files. Rows and objects
        are read concurrently.
        """
        from apis.shared.files.attachment_preview import decode_preview, preview_format, preview_key

        async def one(upload_id: str) -> Optional[dict]:
            meta = await self.repository.get_file(user_id, upload_id)
            if meta is None or meta.status != FileStatus.READY:
                return None
            if preview_format(meta.filename, meta.mime_type) is None:
                return None
            try:
                response = await asyncio.to_thread(
                    self._s3_client.get_object, Bucket=self.bucket_name, Key=preview_key(meta.s3_key)
                )
                return decode_preview(response["Body"].read())
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                    logger.warning("Attachment preview not read", exc_info=True)
                return None

        unique = list(dict.fromkeys(upload_ids))
        results = await asyncio.gather(*(one(upload_id) for upload_id in unique))
        return dict(zip(unique, results))

    def _delete_derived_objects(self, file_meta: FileMetadata) -> None:
        """Best-effort delete of a prepared document's ``_derived/`` sidecars.

        Only a reference-class file (its preparation), a PDF (its first-page
        thumbnail) or a Word, spreadsheet, CSV or deck file (its card preview)
        can have any, so every other file skips the LIST. A preparation still running when the row is deleted cleans up
        after itself: its final row write is conditional on the row, and it
        deletes the prefix when that write fails.
        """
        if not (
            file_meta.preparation
            or is_reference_document(file_meta)
            or file_meta.mime_type == "application/pdf"  # its _derived/thumb.png
            or self._has_preview_format(file_meta)  # its _derived/preview.json
            or (file_meta.model_image or {}).get("downscaled")  # its _derived/model-image.*
        ):
            return
        from apis.shared.files.large_document import derived_prefix

        prefix = derived_prefix(file_meta.s3_key)
        try:
            paginator = self._s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if keys:
                    self._s3_client.delete_objects(
                        Bucket=self.bucket_name, Delete={"Objects": keys, "Quiet": True}
                    )
        except ClientError:
            logger.warning("Failed to delete derived objects for a prepared document", exc_info=True)

    def _delete_thumbnail_object(self, file_meta: FileMetadata) -> None:
        """
        Best-effort delete of the thumbnail sibling.

        S3 ``delete_object`` is idempotent — a missing key is not an error.
        We swallow other errors so a broken thumbnail never blocks deletion
        of the underlying file.
        """
        thumb_key = self._thumbnail_s3_key(file_meta)
        try:
            self._s3_client.delete_object(Bucket=self.bucket_name, Key=thumb_key)
        except ClientError as e:
            logger.warning(f"Failed to delete thumbnail {thumb_key}: {e}")

    async def _reject_upload(self, file_meta: FileMetadata) -> None:
        """
        Fail a pending upload whose stored object did not pass verification.

        Marks the row FAILED and best-effort deletes the S3 object. No
        thumbnail can exist yet: thumbnails render only for READY files.
        """
        await self.repository.update_file_status(
            file_meta.user_id, file_meta.upload_id, FileStatus.FAILED
        )
        try:
            self._s3_client.delete_object(
                Bucket=self.bucket_name, Key=file_meta.s3_key
            )
        except ClientError:
            logger.warning("Failed to delete rejected upload object", exc_info=True)
        logger.warning("Rejected upload: stored object failed size verification")

    # =========================================================================
    # File Management
    # =========================================================================

    async def get_file(self, user_id: str, upload_id: str) -> Optional[FileMetadata]:
        """
        Get a file's metadata.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            FileMetadata if found, None otherwise
        """
        return await self.repository.get_file(user_id, upload_id)

    async def delete_file(self, user_id: str, upload_id: str, *, from_folder_removal: bool = False) -> bool:
        """
        Delete a file (S3 object and metadata).

        A file a folder attached is marked ``removed`` in its folder row
        (drive-folder spec §4.9), so the manifest stops listing it and neither
        resume nor refresh brings it back. ``from_folder_removal`` skips that
        when the folder itself is being removed: its row is about to go too.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier
            from_folder_removal: Called while removing the whole folder

        Returns:
            True if deleted, False if not found
        """
        # Get file metadata first
        file_meta = await self.repository.get_file(user_id, upload_id)
        if not file_meta:
            return False

        # Delete S3 object
        try:
            self._s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=file_meta.s3_key,
            )
        except ClientError as e:
            logger.warning("Failed to delete S3 object", exc_info=True)
            # Continue with metadata deletion even if S3 fails

        # Best-effort delete of the thumbnail sibling, if one exists, and of a
        # prepared document's sidecars.
        self._delete_thumbnail_object(file_meta)
        self._delete_derived_objects(file_meta)

        # Delete metadata
        deleted = await self.repository.delete_file(user_id, upload_id)

        # Decrement quota if file was in READY state
        if deleted and file_meta.status == FileStatus.READY:
            await self.repository.decrement_quota(user_id, file_meta.size_bytes)

        if deleted and not from_folder_removal:
            await self._mark_removed_from_folder(file_meta)

        logger.info("Deleted file")
        return True

    async def _mark_removed_from_folder(self, file_meta: FileMetadata) -> None:
        """A folder file the user deleted: its entry becomes ``removed``. Never raises."""
        from apis.shared.files.folder_attachments import (
            ENTRY_ATTACHED,
            ENTRY_REMOVED,
            folder_attach_id_from_source,
        )

        folder_attach_id = folder_attach_id_from_source(file_meta.source)
        if not folder_attach_id:
            return
        try:
            from apis.shared.files.folder_repository import get_folder_attachment_repository

            repository = get_folder_attachment_repository()
            folder = await repository.get(file_meta.user_id, folder_attach_id)
            if folder is None:
                return
            changed = False
            for entry in folder.files:
                if entry.upload_id == file_meta.upload_id and entry.status == ENTRY_ATTACHED:
                    entry.status = ENTRY_REMOVED
                    entry.error = "deleted by the user"
                    changed = True
            if not changed:
                return
            folder.counts.attached = sum(1 for e in folder.files if e.status == ENTRY_ATTACHED)
            await repository.update(file_meta.user_id, folder_attach_id, counts=folder.counts, files=folder.files)
        except Exception:  # noqa: BLE001 - the delete already happened
            logger.warning("Could not mark a deleted file removed in its folder", exc_info=True)

    async def list_user_files(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
        sort_by: str = "date",
        sort_order: str = "desc",
    ) -> FileListResponse:
        """
        List files for a user.

        Args:
            user_id: The user identifier
            session_id: Optional filter by session
            limit: Maximum number of files
            cursor: Pagination cursor
            sort_by: Sort field (date, size, type)
            sort_order: Sort order (asc, desc)

        Returns:
            FileListResponse with files and pagination
        """
        if session_id:
            # Use GSI for session-based query
            files = await self.repository.list_session_files(
                session_id, status=FileStatus.READY
            )
            # Filter to user's files only (safety check)
            files = [f for f in files if f.user_id == user_id]
        else:
            # Query by user
            files, next_cursor = await self.repository.list_user_files(
                user_id, limit=limit, cursor=cursor, status=FileStatus.READY
            )

        # Apply sorting (DynamoDB only supports date-based sorting natively via SK)
        # For size/type sorting, we need to sort in application layer
        files = self._sort_files(files, sort_by, sort_order)

        # Apply pagination for session-based queries (already paginated for user queries)
        if session_id:
            return FileListResponse(
                files=[FileResponse.from_metadata(f) for f in files[:limit]],
                next_cursor=None if len(files) <= limit else "more",
                total_count=len(files),
            )
        else:
            return FileListResponse(
                files=[FileResponse.from_metadata(f) for f in files],
                next_cursor=next_cursor,
                total_count=None,  # Not available with pagination
            )

    def _sort_files(
        self,
        files: List[FileMetadata],
        sort_by: str,
        sort_order: str,
    ) -> List[FileMetadata]:
        """
        Sort files by the specified field and order.

        Args:
            files: List of files to sort
            sort_by: Sort field (date, size, type)
            sort_order: Sort order (asc, desc)

        Returns:
            Sorted list of files
        """
        reverse = sort_order == "desc"

        if sort_by == "size":
            return sorted(files, key=lambda f: f.size_bytes, reverse=reverse)
        elif sort_by == "type":
            return sorted(files, key=lambda f: f.mime_type, reverse=reverse)
        else:  # date (default)
            return sorted(files, key=lambda f: f.created_at, reverse=reverse)

    async def get_session_files(self, session_id: str) -> List[FileMetadata]:
        """
        Get all ready files for a session.

        Used when building chat requests with file attachments.

        Args:
            session_id: The session/conversation ID

        Returns:
            List of FileMetadata for ready files
        """
        return await self.repository.list_session_files(
            session_id, status=FileStatus.READY
        )

    # =========================================================================
    # Cascade Delete Operations
    # =========================================================================

    async def delete_session_files(self, session_id: str) -> int:
        """
        Delete all files for a session (cascade delete).

        Deletes both S3 objects and DynamoDB metadata for all files
        in the session, and decrements user quotas accordingly.

        Args:
            session_id: The session/conversation identifier

        Returns:
            Number of files deleted
        """
        # Get all files for this session first
        files = await self.repository.list_session_files(session_id, status=None)

        # A folder attachment's row shares the session partition under its own
        # prefix (drive-folder spec §4.2); its files are among ``files`` and
        # the row itself goes here. Best-effort, like every step below.
        try:
            from apis.shared.files.folder_repository import get_folder_attachment_repository

            await get_folder_attachment_repository().delete_session_folders(session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to cascade delete folder attachments for session {session_id}: {e}")

        if not files:
            logger.info(f"No files to cascade delete for session {session_id}")
            return 0

        deleted_count = 0

        for file_meta in files:
            try:
                # Delete S3 object
                try:
                    self._s3_client.delete_object(
                        Bucket=self.bucket_name,
                        Key=file_meta.s3_key,
                    )
                except ClientError as e:
                    logger.warning(f"Failed to delete S3 object for {file_meta.upload_id}: {e}")
                    # Continue with metadata deletion

                # Best-effort delete of the thumbnail sibling, if one exists,
                # and of a prepared document's sidecars.
                self._delete_thumbnail_object(file_meta)
                self._delete_derived_objects(file_meta)

                # Delete metadata
                deleted = await self.repository.delete_file(
                    file_meta.user_id, file_meta.upload_id
                )

                # Decrement quota if file was ready
                if deleted and file_meta.status == FileStatus.READY:
                    await self.repository.decrement_quota(
                        file_meta.user_id, file_meta.size_bytes
                    )

                deleted_count += 1
                logger.debug(f"Cascade deleted file {file_meta.upload_id}")

            except Exception as e:
                logger.warning(
                    f"Failed to cascade delete file {file_meta.upload_id}: {e}"
                )

        logger.info(f"Cascade deleted {deleted_count}/{len(files)} files for session {session_id}")
        return deleted_count

    # =========================================================================
    # Quota Management
    # =========================================================================

    async def get_user_quota(self, user_id: str) -> QuotaResponse:
        """
        Get user's quota status.

        Args:
            user_id: The user identifier

        Returns:
            QuotaResponse with usage and limits
        """
        quota = await self.repository.get_user_quota(user_id)
        return QuotaResponse(
            used_bytes=quota.total_bytes,
            max_bytes=self.user_quota_bytes,
            file_count=quota.file_count,
        )


# Global service instance
_service_instance: Optional[FileUploadService] = None


def get_file_upload_service() -> FileUploadService:
    """Get or create the global FileUploadService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = FileUploadService()
    return _service_instance
