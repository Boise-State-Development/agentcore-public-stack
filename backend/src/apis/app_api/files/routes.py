"""
File Upload API Routes

Endpoints for file upload via pre-signed URLs.
"""

import logging
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse, Response

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import attachment_kb_enabled
from apis.shared.files.attachment_index import search_status
from apis.shared.files.repository import InvalidCursorError
from apis.shared.files.models import (
    ImportFileRequest,
    PresignRequest,
    PresignResponse,
    CompleteUploadResponse,
    FileResponse,
    PreviewUrlResponse,
    SearchStatusResponse,
    SheetPreviewResponse,
    TextSnippetResponse,
    ThumbnailResponse,
    FileListResponse,
    QuotaResponse,
    QuotaExceededError as QuotaExceededModel,
    ALLOWED_MIME_TYPES,
)
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service

from apis.app_api.file_sources.models import FileSourceError, FileSourceTooLargeError
from apis.app_api.file_sources.service import (
    http_error_for_file_source_error,
    require_file_source_token,
    resolve_file_source,
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
from .sheet_preview import WorkbookTooLargeError, WorkbookUnreadableError
from .thumbnails import ThumbnailRenderError, ThumbnailUnsupportedError

from apis.shared.security.log_sanitize import scrub_log

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

    **Supported file types:** PDF, DOCX, TXT, HTML, CSV, XLS, XLSX, PPTX, MD

    **Limits:**
    - Maximum file size: 4MB (25MB for PPTX — decks never go inline to
      Bedrock, so the inline-document ceiling doesn't bound them)
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

    except FileTooLargeError as e:
        logger.warning(
            f"Stored upload too large from user {user.name}: "
            f"{e.size_bytes} > {e.max_size}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds {e.max_size // (1024*1024)}MB limit",
        )

    except FileUploadError as e:
        logger.warning("Upload completion error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.post("/import", response_model=FileResponse)
async def import_file_from_source(
    request: ImportFileRequest,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
):
    """
    Attach a file from a connected file source to a conversation.

    The composer's "Add from Google Drive": app-api resolves the connector
    to its file-source adapter (the same gating as `/connectors/{id}/browse`),
    downloads the file with the user's vaulted OAuth token, and stores it as
    a ready upload with the device-upload type, size and quota rules. The
    response is the file's metadata, the shape `GET /files` lists.

    One file per call, so the SPA can show each file's own progress and
    retry one that failed. The download is capped at the largest size any
    upload class allows, so an oversized file is refused before it is
    buffered.

    **Errors:** 409 when the connector needs consent, 400 for an unsupported
    type or an oversized file, 403 when the user's storage quota is full,
    502 when the file source cannot be reached.
    """
    provider, adapter = await resolve_file_source(
        request.connector_id, user, provider_repo, role_service
    )
    access_token = await require_file_source_token(provider, user.user_id)

    try:
        downloaded = await adapter.download(
            access_token, request.file_id, max_bytes=service.max_download_bytes()
        )
    except FileSourceTooLargeError:
        cap_mb = service.max_download_bytes() // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds {cap_mb}MB limit",
        )
    except FileSourceError as err:
        logger.warning(
            "File-source download failed for connector %s: %s",
            scrub_log(request.connector_id),
            scrub_log(err),
        )
        raise http_error_for_file_source_error(err)

    try:
        file_meta = await service.import_file(
            user_id=user.user_id,
            session_id=request.session_id,
            filename=downloaded.filename,
            mime_type=downloaded.content_type,
            content=downloaded.content,
            source=f"connector:{provider.provider_id}",
        )
    except InvalidFileTypeError as e:
        logger.warning(f"Unsupported file type imported by user {user.name}: {e.mime_type}")
        allowed = ", ".join(ALLOWED_MIME_TYPES.values())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {e.mime_type}. Supported: {allowed}",
        )
    except FileTooLargeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds {e.max_size // (1024*1024)}MB limit",
        )
    except QuotaExceededError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=QuotaExceededModel(
                current_usage=e.current_usage,
                max_allowed=e.max_allowed,
                required_space=e.required_space,
            ).model_dump(by_alias=True),
        )
    except FileUploadError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )

    return FileResponse.from_metadata(file_meta)


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


@router.get("/{upload_id}/download", response_class=RedirectResponse)
async def download_file(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Redirect to a freshly minted presigned GET URL that downloads the file.

    This is the *durable* download link. Presigned S3 URLs expire in minutes,
    so any URL that gets written into a conversation — the inline download
    card on a generated .docx, a link in an assistant message — is dead by the
    time the user reopens the thread. This route is a stable, owner-scoped
    path (`/api/files/{uploadId}/download`) that mints the presigned URL at
    click time, so a persisted link keeps working for as long as the file does.

    A 302 rather than a proxied body: the bytes still come straight from S3,
    and the response carries `Content-Disposition: attachment` from the
    presigned request.
    """
    try:
        url = await service.get_download_url(user.user_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {upload_id} not found or not owned by you",
        )

    # 302 (not 307): this is a GET-only redirect to a different origin, and
    # `no-store` keeps a browser or intermediary from reusing the Location
    # after the signature it carries has expired.
    return RedirectResponse(
        url=url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
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


@router.get("/{upload_id}/search-status", response_model=SearchStatusResponse)
async def get_search_status(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Whether a large document can be searched through the attachments KB.

    One row read, no Bedrock call: the status is what the indexing pipeline last
    wrote on the file (`kbIndex`, docs/specs/large-document-attachments.md §4G).
    The SPA polls it while the answer is `preparing` or `indexing`.
    """
    meta = await service.get_file(user.user_id, upload_id)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {upload_id} not found or not owned by you",
        )
    return SearchStatusResponse(
        upload_id=upload_id, status=search_status(meta, kb_enabled=attachment_kb_enabled())
    )


@router.get("/{upload_id}/sheet-preview", response_model=SheetPreviewResponse)
async def get_sheet_preview(
    upload_id: str,
    user: User = Depends(get_current_user_from_session),
    service: FileUploadService = Depends(get_file_upload_service),
):
    """
    Read an .xlsx workbook into rows for the UI's data grid.

    The other previews (.docx, .pptx, .csv) hand the browser a presigned
    URL and parse the bytes client-side. Spreadsheets cannot: the npm
    build of SheetJS is frozen on a release with unfixed advisories, and
    ExcelJS raises on any workbook holding a native chart — which is what
    `create_excel_spreadsheet` produces. So the workbook is read here and
    only values cross the wire.

    Values only. No fills, fonts, borders, merges or charts; download and
    open the file for those.

    Status codes:
    - 200: Sheets read (possibly truncated — see `truncated` on each).
    - 404: File not found, not owned by the caller, or not readable.
    - 413: Workbook is past the reader's size cap.
    - 415: MIME type is not a readable workbook (the UI should not have
           offered a preview; `.xls` lands here).
    - 422: File present but unreadable (corrupt, encrypted, not OOXML).
    """
    try:
        return await service.get_sheet_preview(user.user_id, upload_id)

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

    except WorkbookTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="This workbook is too large to preview. Download it to open in Excel.",
        )

    except WorkbookUnreadableError:
        # Deliberately not echoing openpyxl's message: it names internal
        # XML parts and tells the user nothing they can act on.
        logger.warning("Workbook could not be parsed")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This workbook could not be read. It may be corrupt or password-protected.",
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
        logger.warning(f"Thumbnail render failed for {scrub_log(upload_id)}: {scrub_log(e)}")
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
