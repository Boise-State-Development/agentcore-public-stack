"""
File Upload Service

Business logic for file uploads with S3 pre-signed URLs and quota management.
"""

import os
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from .models import (
    FileMetadata,
    FileStatus,
    UserFileQuota,
    PresignRequest,
    PresignResponse,
    CompleteUploadResponse,
    FileResponse,
    FileListResponse,
    QuotaResponse,
    is_allowed_mime_type,
    ALLOWED_MIME_TYPES,
)
from .repository import FileUploadRepository, get_file_upload_repository

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

    def __init__(
        self,
        repository: Optional[FileUploadRepository] = None,
        s3_client=None,
        bucket_name: Optional[str] = None,
        max_file_size: Optional[int] = None,
        max_files_per_message: Optional[int] = None,
        user_quota_bytes: Optional[int] = None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_file_upload_repository()

        # S3 configuration
        self._s3_client = s3_client or boto3.client("s3")
        self.bucket_name = bucket_name or os.environ.get(
            "S3_USER_FILES_BUCKET_NAME", "user-files"
        )

        # Upload limits from environment
        self.max_file_size = max_file_size or int(
            os.environ.get("FILE_UPLOAD_MAX_SIZE_BYTES", 4 * 1024 * 1024)  # 4MB
        )
        self.max_files_per_message = max_files_per_message or int(
            os.environ.get("FILE_UPLOAD_MAX_FILES_PER_MESSAGE", 5)
        )
        self.user_quota_bytes = user_quota_bytes or int(
            os.environ.get("FILE_UPLOAD_USER_QUOTA_BYTES", 1024 * 1024 * 1024)  # 1GB
        )

        # Pre-signed URL expiration
        self.presign_expiration = 15 * 60  # 15 minutes

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

        # Validate file size
        if request.size_bytes > self.max_file_size:
            raise FileTooLargeError(request.size_bytes, self.max_file_size)

        # Check quota
        quota = await self.repository.get_user_quota(user_id)
        projected_usage = quota.total_bytes + request.size_bytes
        if projected_usage > self.user_quota_bytes:
            raise QuotaExceededError(
                current_usage=quota.total_bytes,
                max_allowed=self.user_quota_bytes,
                required_space=request.size_bytes,
            )

        # Generate upload ID (timestamp-prefixed UUID for sortable uniqueness)
        # Format: {timestamp_hex}_{uuid} for chronological sorting
        timestamp_hex = format(int(datetime.utcnow().timestamp() * 1000), 'x')
        upload_id = f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"

        # Build S3 key
        s3_key = f"user-files/{user_id}/{request.session_id}/{upload_id}/{request.filename}"

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

        expires_at = (datetime.utcnow() + timedelta(seconds=self.presign_expiration)).isoformat() + "Z"

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
        2. Optionally verify S3 object exists (HEAD request)
        3. Update status to READY
        4. Increment user quota

        Args:
            user_id: The uploading user's ID
            upload_id: The upload identifier

        Returns:
            CompleteUploadResponse with file details

        Raises:
            FileNotFoundError: If upload not found or not owned by user
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

        # Optional: Verify S3 object exists
        try:
            self._s3_client.head_object(
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

        # Update status to READY
        updated = await self.repository.update_file_status(
            user_id, upload_id, FileStatus.READY
        )

        # Increment quota
        await self.repository.increment_quota(user_id, file_meta.size_bytes)

        logger.info(f"Completed upload {upload_id} for user {user_id}")

        return CompleteUploadResponse(
            upload_id=upload_id,
            status=FileStatus.READY.value,
            s3_uri=file_meta.s3_uri,
            filename=file_meta.filename,
            size_bytes=file_meta.size_bytes,
        )

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

    async def delete_file(self, user_id: str, upload_id: str) -> bool:
        """
        Delete a file (S3 object and metadata).

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

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
            logger.warning(f"Failed to delete S3 object for {upload_id}: {e}")
            # Continue with metadata deletion even if S3 fails

        # Delete metadata
        deleted = await self.repository.delete_file(user_id, upload_id)

        # Decrement quota if file was in READY state
        if deleted and file_meta.status == FileStatus.READY:
            await self.repository.decrement_quota(user_id, file_meta.size_bytes)

        logger.info(f"Deleted file {upload_id} for user {user_id}")
        return True

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
