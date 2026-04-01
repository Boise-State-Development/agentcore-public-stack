"""
File Upload Models

Pydantic models for file upload metadata, requests, and responses.
Supports the pre-signed URL upload flow for S3.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


class FileStatus(str, Enum):
    """Upload status for a file."""
    PENDING = "pending"  # Pre-signed URL generated, awaiting upload
    READY = "ready"      # Upload complete, file is ready for use
    FAILED = "failed"    # Upload failed or timed out


# =============================================================================
# Allowed File Types (Bedrock-compliant)
# =============================================================================

ALLOWED_MIME_TYPES = {
    # Documents
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/html": "html",
    "text/csv": "csv",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/markdown": "md",
    # Images (Bedrock-supported)
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

ALLOWED_EXTENSIONS = {
    # Documents
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".html": "text/html",
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown",
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def get_file_format(mime_type: str) -> Optional[str]:
    """Get Bedrock document format from MIME type."""
    return ALLOWED_MIME_TYPES.get(mime_type)


def is_allowed_mime_type(mime_type: str) -> bool:
    """Check if MIME type is allowed for upload."""
    return mime_type in ALLOWED_MIME_TYPES


# =============================================================================
# Database Models (stored in DynamoDB)
# =============================================================================


class FileMetadata(BaseModel):
    """
    File metadata stored in DynamoDB.

    Key Schema:
      PK: USER#{userId}
      SK: FILE#{uploadId}
      GSI1PK: CONV#{sessionId}
      GSI1SK: FILE#{uploadId}
    """

    # Identity
    upload_id: str = Field(..., description="Unique identifier (timestamp-prefixed UUID)")
    user_id: str = Field(..., description="Owner user ID")
    session_id: str = Field(..., description="Associated conversation session")

    # File metadata
    filename: str = Field(..., description="Original filename")
    mime_type: str = Field(..., description="MIME type (e.g., application/pdf)")
    size_bytes: int = Field(..., description="File size in bytes")

    # S3 location
    s3_key: str = Field(..., description="Full S3 object key")
    s3_bucket: str = Field(..., description="S3 bucket name")

    # Status
    status: FileStatus = Field(default=FileStatus.PENDING)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # TTL for DynamoDB (365 days from creation)
    ttl: Optional[int] = Field(None, description="Unix epoch for TTL expiration")

    model_config = ConfigDict(use_enum_values=True)

    @property
    def s3_uri(self) -> str:
        """Get S3 URI for Bedrock document block."""
        return f"s3://{self.s3_bucket}/{self.s3_key}"

    @property
    def file_format(self) -> Optional[str]:
        """Get Bedrock document format from MIME type."""
        return get_file_format(self.mime_type)

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format."""
        # Calculate TTL: 365 days from creation
        ttl_value = self.ttl
        if ttl_value is None:
            ttl_value = int(self.created_at.timestamp()) + (365 * 24 * 60 * 60)

        return {
            "PK": f"USER#{self.user_id}",
            "SK": f"FILE#{self.upload_id}",
            "GSI1PK": f"CONV#{self.session_id}",
            "GSI1SK": f"FILE#{self.upload_id}",
            "uploadId": self.upload_id,
            "userId": self.user_id,
            "sessionId": self.session_id,
            "filename": self.filename,
            "mimeType": self.mime_type,
            "sizeBytes": self.size_bytes,
            "s3Key": self.s3_key,
            "s3Bucket": self.s3_bucket,
            "s3Uri": self.s3_uri,
            "status": self.status if isinstance(self.status, str) else self.status.value,
            "createdAt": self.created_at.isoformat() + "Z",
            "updatedAt": self.updated_at.isoformat() + "Z",
            "ttl": ttl_value,
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "FileMetadata":
        """Create from DynamoDB item."""
        created_at = item.get("createdAt", "")
        updated_at = item.get("updatedAt", "")

        return cls(
            upload_id=item.get("uploadId", ""),
            user_id=item.get("userId", ""),
            session_id=item.get("sessionId", ""),
            filename=item.get("filename", ""),
            mime_type=item.get("mimeType", ""),
            size_bytes=int(item.get("sizeBytes", 0)),
            s3_key=item.get("s3Key", ""),
            s3_bucket=item.get("s3Bucket", ""),
            status=item.get("status", FileStatus.PENDING),
            created_at=datetime.fromisoformat(created_at.rstrip("Z")) if created_at else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z")) if updated_at else datetime.now(timezone.utc),
            ttl=item.get("ttl"),
        )


class UserFileQuota(BaseModel):
    """
    User's file storage quota tracking.

    Key Schema:
      PK: USER#{userId}
      SK: QUOTA
    """

    user_id: str
    total_bytes: int = Field(default=0, description="Current usage in bytes")
    file_count: int = Field(default=0, description="Total number of files")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format."""
        return {
            "PK": f"USER#{self.user_id}",
            "SK": "QUOTA",
            "userId": self.user_id,
            "totalBytes": self.total_bytes,
            "fileCount": self.file_count,
            "updatedAt": self.updated_at.isoformat() + "Z",
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "UserFileQuota":
        """Create from DynamoDB item."""
        updated_at = item.get("updatedAt", "")
        return cls(
            user_id=item.get("userId", ""),
            total_bytes=int(item.get("totalBytes", 0)),
            file_count=int(item.get("fileCount", 0)),
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z")) if updated_at else datetime.now(timezone.utc),
        )


# =============================================================================
# API Request Models
# =============================================================================


class PresignRequest(BaseModel):
    """Request body for POST /api/files/presign."""

    session_id: str = Field(..., validation_alias="sessionId", description="Conversation session ID")
    filename: str = Field(..., min_length=1, max_length=255, description="Original filename")
    mime_type: str = Field(..., validation_alias="mimeType", description="File MIME type")
    size_bytes: int = Field(..., validation_alias="sizeBytes", gt=0, description="File size in bytes")

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# API Response Models
# =============================================================================


class PresignResponse(BaseModel):
    """Response for POST /api/files/presign."""

    upload_id: str = Field(..., alias="uploadId")
    presigned_url: str = Field(..., alias="presignedUrl")
    expires_at: str = Field(..., alias="expiresAt", description="ISO8601 expiration time")

    model_config = ConfigDict(populate_by_name=True)


class CompleteUploadResponse(BaseModel):
    """Response for POST /api/files/{uploadId}/complete."""

    upload_id: str = Field(..., alias="uploadId")
    status: str
    s3_uri: str = Field(..., alias="s3Uri")
    filename: str
    size_bytes: int = Field(..., alias="sizeBytes")

    model_config = ConfigDict(populate_by_name=True)


class FileResponse(BaseModel):
    """Single file in list response."""

    upload_id: str = Field(..., alias="uploadId")
    filename: str
    mime_type: str = Field(..., alias="mimeType")
    size_bytes: int = Field(..., alias="sizeBytes")
    session_id: str = Field(..., alias="sessionId")
    s3_uri: str = Field(..., alias="s3Uri")
    status: str
    created_at: str = Field(..., alias="createdAt")

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_metadata(cls, meta: FileMetadata) -> "FileResponse":
        """Create from FileMetadata."""
        return cls(
            upload_id=meta.upload_id,
            filename=meta.filename,
            mime_type=meta.mime_type,
            size_bytes=meta.size_bytes,
            session_id=meta.session_id,
            s3_uri=meta.s3_uri,
            status=meta.status if isinstance(meta.status, str) else meta.status.value,
            created_at=meta.created_at.isoformat() + "Z",
        )


class FileListResponse(BaseModel):
    """Response for GET /api/files."""

    files: List[FileResponse]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
    total_count: Optional[int] = Field(None, alias="totalCount")

    model_config = ConfigDict(populate_by_name=True)


class QuotaResponse(BaseModel):
    """Response for GET /api/files/quota."""

    used_bytes: int = Field(..., alias="usedBytes")
    max_bytes: int = Field(..., alias="maxBytes")
    file_count: int = Field(..., alias="fileCount")

    model_config = ConfigDict(populate_by_name=True)


class QuotaExceededError(BaseModel):
    """Error response when quota is exceeded."""

    error: str = "QUOTA_EXCEEDED"
    message: str = "Storage quota exceeded"
    current_usage: int = Field(..., alias="currentUsage")
    max_allowed: int = Field(..., alias="maxAllowed")
    required_space: int = Field(..., alias="requiredSpace")

    model_config = ConfigDict(populate_by_name=True)
