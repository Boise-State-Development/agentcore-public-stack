"""
File Upload API Routes

Endpoints for file upload via pre-signed URLs.
"""

import logging
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from apis.shared.auth import User, get_current_user

from .models import (
    PresignRequest,
    PresignResponse,
    CompleteUploadResponse,
    FileListResponse,
    QuotaResponse,
    QuotaExceededError as QuotaExceededModel,
    ALLOWED_MIME_TYPES,
)
from .service import (
    get_file_upload_service,
    FileUploadService,
    QuotaExceededError,
    InvalidFileTypeError,
    FileTooLargeError,
    FileNotFoundError,
    FileUploadError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


# =============================================================================
# Pre-signed URL Endpoints
# =============================================================================


@router.post("/presign", response_model=PresignResponse)
async def request_presigned_url(
    request: PresignRequest,
    user: User = Depends(get_current_user),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Request a pre-signed URL for uploading a file.

    The client should:
    1. Call this endpoint with file metadata
    2. Use the returned presignedUrl to PUT the file directly to S3
    3. Call POST /files/{uploadId}/complete to finalize

    **Supported file types:** PDF, DOCX, TXT, HTML, CSV, XLS, XLSX, MD

    **Limits:**
    - Maximum file size: 4MB
    - Maximum files per message: 5
    - User storage quota: 1GB
    """
    logger.info(
        f"User {user.email} requesting presigned URL for {request.filename} "
        f"({request.size_bytes} bytes)"
    )

    try:
        response = await service.request_presigned_url(user.user_id, request)
        return response

    except InvalidFileTypeError as e:
        logger.warning(f"Invalid file type from user {user.email}: {e.mime_type}")
        allowed = ", ".join(ALLOWED_MIME_TYPES.values())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {e.mime_type}. Supported: {allowed}",
        )

    except FileTooLargeError as e:
        logger.warning(
            f"File too large from user {user.email}: "
            f"{e.size_bytes} > {e.max_size}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds {e.max_size // (1024*1024)}MB limit",
        )

    except QuotaExceededError as e:
        logger.warning(
            f"Quota exceeded for user {user.email}: "
            f"{e.current_usage}/{e.max_allowed}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=QuotaExceededModel(
                current_usage=e.current_usage,
                max_allowed=e.max_allowed,
                required_space=e.required_space,
            ).model_dump(by_alias=True),
        )


@router.post("/{upload_id}/complete", response_model=CompleteUploadResponse)
async def complete_upload(
    upload_id: str,
    user: User = Depends(get_current_user),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Mark an upload as complete after successful S3 upload.

    Call this after successfully uploading the file using the pre-signed URL.
    This verifies the S3 object exists and updates the file status to 'ready'.
    """
    logger.info(f"User {user.email} completing upload {upload_id}")

    try:
        response = await service.complete_upload(user.user_id, upload_id)
        return response

    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload {upload_id} not found or not owned by you",
        )

    except FileUploadError as e:
        logger.warning(f"Upload completion error for {upload_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


# =============================================================================
# File Management Endpoints
# =============================================================================


class SortBy(str, Enum):
    """Sort field options for file listing."""
    DATE = "date"
    SIZE = "size"
    TYPE = "type"


class SortOrder(str, Enum):
    """Sort order options for file listing."""
    ASC = "asc"
    DESC = "desc"


@router.get("/", response_model=FileListResponse)
async def list_files(
    session_id: Optional[str] = Query(
        None, alias="sessionId", description="Filter by session/conversation"
    ),
    limit: int = Query(20, ge=1, le=100, description="Maximum files to return"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
    sort_by: SortBy = Query(
        SortBy.DATE, alias="sortBy", description="Sort by: date, size, or type"
    ),
    sort_order: SortOrder = Query(
        SortOrder.DESC, alias="sortOrder", description="Sort order: asc or desc"
    ),
    user: User = Depends(get_current_user),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    List files for the authenticated user.

    Optionally filter by session/conversation. Returns only files with 'ready' status.
    Supports sorting by date (default), size, or type.
    """
    logger.info(
        f"User {user.email} listing files"
        + (f" for session {session_id}" if session_id else "")
        + f" (sort: {sort_by.value} {sort_order.value})"
    )

    response = await service.list_user_files(
        user_id=user.user_id,
        session_id=session_id,
        limit=limit,
        cursor=cursor,
        sort_by=sort_by.value,
        sort_order=sort_order.value,
    )
    return response


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    upload_id: str,
    user: User = Depends(get_current_user),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Delete a file.

    Removes both the S3 object and metadata. Also decrements user quota.
    Use this when a user removes an attached file before sending,
    or when manually deleting from the file browser.
    """
    logger.info(f"User {user.email} deleting file {upload_id}")

    deleted = await service.delete_file(user.user_id, upload_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {upload_id} not found or not owned by you",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# =============================================================================
# Quota Endpoint
# =============================================================================


@router.get("/quota", response_model=QuotaResponse)
async def get_quota(
    user: User = Depends(get_current_user),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Get current quota usage for the authenticated user.

    Returns used bytes, maximum allowed, and file count.
    """
    response = await service.get_user_quota(user.user_id)
    return response
