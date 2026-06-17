"""
File Upload API Routes

Endpoints for file upload via pre-signed URLs.
"""

import logging
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.files.repository import InvalidCursorError
from apis.shared.files.models import (
    PresignRequest,
    PresignResponse,
    CompleteUploadResponse,
    PreviewUrlResponse,
    TextSnippetResponse,
    ThumbnailResponse,
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
from .thumbnails import ThumbnailRenderError, ThumbnailUnsupportedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


# =============================================================================
# Pre-signed URL Endpoints
# =============================================================================


@router.post("/presign", response_model=PresignResponse)
async def request_presigned_url(
    request: PresignRequest,
    user: User = Depends(get_current_user_from_session),
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
        f"User {user.name} requesting presigned URL for {request.filename} "
        f"({request.size_bytes} bytes)"
    )

    try:
        response = await service.request_presigned_url(user.user_id, request)
        return response

    except InvalidFileTypeError as e:
        logger.warning(f"Invalid file type from user {user.name}: {e.mime_type}")
        allowed = ", ".join(ALLOWED_MIME_TYPES.values())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {e.mime_type}. Supported: {allowed}",
        )

    except FileTooLargeError as e:
        logger.warning(
            f"File too large from user {user.name}: "
            f"{e.size_bytes} > {e.max_size}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds {e.max_size // (1024*1024)}MB limit",
        )

    except QuotaExceededError as e:
        logger.warning(
            f"Quota exceeded for user {user.name}: "
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
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Mark an upload as complete after successful S3 upload.

    Call this after successfully uploading the file using the pre-signed URL.
    This verifies the S3 object exists and updates the file status to 'ready'.
    """
    logger.info("User completing upload")

    try:
        response = await service.complete_upload(user.user_id, upload_id)
        return response

    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload {upload_id} not found or not owned by you",
        )

    except FileUploadError as e:
        logger.warning("Upload completion error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.get("/{upload_id}/preview-url", response_model=PreviewUrlResponse)
async def get_preview_url(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Generate a short-lived presigned GET URL for an uploaded file.

    Used by the UI to render image previews inline and to open files in a
    lightbox. The URL is scoped to the file owner and expires after a few
    minutes.
    """
    try:
        return await service.get_preview_url(user.user_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {upload_id} not found or not owned by you",
        )


@router.get("/{upload_id}/text-snippet", response_model=TextSnippetResponse)
async def get_text_snippet(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Return a short UTF-8 text excerpt from the start of a file.

    Used by the UI to render a content peek inside the document-style
    attachment card for text-based files (txt, md, csv, html). Returns an
    empty snippet for non-text MIME types so the UI can fall back to a
    skeleton mockup.
    """
    try:
        return await service.get_text_snippet(user.user_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {upload_id} not found or not owned by you",
        )


@router.get("/{upload_id}/thumbnail", response_model=ThumbnailResponse)
async def get_thumbnail(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Return a presigned URL for a PNG thumbnail of the file's first page.

    Lazy-renders on first request and caches the resulting `_thumb.png`
    sibling object next to the original. Subsequent calls hit the cache and
    return immediately.

    Status codes:
    - 200: Thumbnail available (response body indicates `cached`).
    - 404: File not found or not owned by the caller.
    - 415: MIME type has no thumbnail renderer (UI should fall back to its
           skeleton card).
    - 422: File present but unrenderable (corrupt, encrypted, empty PDF, ...).
    """
    try:
        return await service.get_or_create_thumbnail(user.user_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {upload_id} not found or not owned by you",
        )
    except ThumbnailUnsupportedError as e:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(e),
        )
    except ThumbnailRenderError as e:
        logger.warning(f"Thumbnail render failed for {upload_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not render a thumbnail for this file",
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


@router.get("", response_model=FileListResponse)
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
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    List files for the authenticated user.

    Optionally filter by session/conversation. Returns only files with 'ready' status.
    Supports sorting by date (default), size, or type.
    """
    logger.info("User listing files")

    try:
        response = await service.list_user_files(
            user_id=user.user_id,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            sort_by=sort_by.value,
            sort_order=sort_order.value,
        )
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.")
    return response


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Delete a file.

    Removes both the S3 object and metadata. Also decrements user quota.
    Use this when a user removes an attached file before sending,
    or when manually deleting from the file browser.
    """
    logger.info("User deleting file")

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
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Get current quota usage for the authenticated user.

    Returns used bytes, maximum allowed, and file count.
    """
    response = await service.get_user_quota(user.user_id)
    return response
