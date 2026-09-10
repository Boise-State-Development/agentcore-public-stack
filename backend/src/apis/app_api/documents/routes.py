"""Document management API routes"""

import asyncio
import logging
import mimetypes
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.assistants.service import resolve_assistant_permission
from apis.app_api.documents.models import (
    CreateDocumentRequest,
    DocumentProvenance,
    DocumentResponse,
    DocumentsListResponse,
    DownloadUrlResponse,
    ImportDocumentsRequest,
    ImportDocumentsResponse,
    ReportUploadFailureRequest,
    UploadUrlResponse,
)
from apis.app_api.documents.services.document_service import _generate_document_id, create_document, list_assistant_documents, update_document_status, release_reservation_if_managed
from apis.app_api.documents.services.document_service import get_document as get_document_service
from apis.app_api.documents.services.import_service import run_import
from apis.app_api.documents.services.storage_service import (
    _get_s3_key,
    _sanitize_filename,
    generate_download_url,
    generate_upload_url,
)
from apis.app_api.file_sources.service import require_file_source_token, resolve_file_source
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.kb_backend.byte_cap import ByteCapExceeded

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistants/{assistant_id}/documents", tags=["documents"])


async def _require_edit_permission(assistant_id: str, current_user: User) -> str:
    """Resolve the requesting user's permission and require owner|editor.

    Returns the assistant's real owner_id so existing document-service calls
    (which are owner-keyed) pass cleanly. Raises HTTPException on 404/403.
    """
    assistant, permission = await resolve_assistant_permission(
        assistant_id=assistant_id, user_id=current_user.user_id, user_email=current_user.email
    )
    if not assistant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Assistant not found: {assistant_id}")
    if permission not in ("owner", "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage documents for this assistant",
        )
    return assistant.owner_id


async def _resolve_managed_kb(assistant_id: str) -> tuple[bool, bool]:
    """Return ``(is_managed, elevated)`` for this assistant's knowledge base.

    Reads the KB_Record once. A legacy knowledge base — an absent record, or any
    record whose engine is not the exact managed literal — returns
    ``(False, False)`` so the caller skips all cap logic: legacy S3-Vectors
    knowledge bases stay uncapped (Requirement 12.11 scopes the cap to managed
    KBs). The elevated tier is READ from the existing ``elevatedByteCap`` flag,
    never written here — granting it is a separate feature.
    """
    from apis.shared.kb_backend.records import ENGINE_MANAGED, get_kb_record, resolve_engine

    # app_kb_id == assistant_id this phase. get_kb_record is a blocking boto3 call.
    record = await asyncio.to_thread(get_kb_record, assistant_id, assistant_id)
    if resolve_engine(record) != ENGINE_MANAGED:
        return False, False
    return True, bool((record or {}).get("elevatedByteCap"))


async def _reserve_managed_upload(assistant_id: str, size_bytes: int) -> int:
    """Provisionally reserve a declared upload size against the managed-KB cap.

    Returns the number of bytes reserved — ``0`` for a legacy knowledge base,
    which is uncapped and never touched. Raises
    :class:`~apis.shared.kb_backend.byte_cap.ByteCapExceeded` if the reservation
    would breach the binding cap; the endpoint turns that into HTTP 413 with the
    numbers (Requirement 12.12).

    The reservation is PROVISIONAL. ``size_bytes`` is the client's own declaration
    and a client that under-reports its size would defeat the cap, so this only
    buys fast, friendly feedback before a presigned URL is issued — the
    authoritative gate is the S3-HEAD reconcile at ingestion (Requirement 12.3).
    ``effective_cap`` folds the per-owner allowance and the per-KB ceiling into one
    atomic conditional write (Requirement 12.1/12.5).
    """
    from apis.shared.kb_backend import byte_cap

    is_managed, elevated = await _resolve_managed_kb(assistant_id)
    if not is_managed:
        return 0
    cap = byte_cap.effective_cap(elevated)
    await asyncio.to_thread(byte_cap.reserve, assistant_id, assistant_id, size_bytes, cap)
    return size_bytes


async def _release_managed_reservation(assistant_id: str, size_bytes: int) -> None:
    """Return a managed-KB reservation taken earlier in THIS request.

    Used only to unwind the request-time reservation when a later step of the same
    upload-URL request fails (document-row create, presigned-URL generation) before
    the client is ever handed a URL. No ``settle_once`` guard here: the reservation
    was taken microseconds ago by this same request, the document is not yet
    visible to any other settlement path, and the ``DOC#`` row may not even exist —
    stamping a marker on it would conjure a partial row. The abandoned-after-URL
    cases (client upload failure, stale sweep) settle through their own guarded
    paths (Requirement 12.6).
    """
    from apis.shared.kb_backend import byte_cap

    if size_bytes <= 0:
        return
    is_managed, _ = await _resolve_managed_kb(assistant_id)
    if not is_managed:
        return
    await asyncio.to_thread(byte_cap.release, assistant_id, assistant_id, size_bytes)


@router.post("/upload-url", response_model=UploadUrlResponse, status_code=status.HTTP_200_OK)
async def generate_upload_url_endpoint(
    assistant_id: str,
    request: CreateDocumentRequest,
    current_user: User = Depends(get_current_user_from_session),
) -> UploadUrlResponse:
    """
    Generate presigned S3 URL for document upload

    Flow:
    1. Verify user is owner or editor of the assistant
    2. Generate document_id
    3. Create document record in DynamoDB (status='uploading')
    4. Generate presigned S3 URL
    5. Return URL to client
    """
    try:
        # 1. Resolve permission — owner or editor may upload documents
        await _require_edit_permission(assistant_id, current_user)

        # 1b. Managed KBs are byte-capped on EVERY path that adds bytes
        #     (Requirement 12.11); the interactive upload path is enforced here.
        #     Reserve the client-declared size BEFORE creating the DOC# row or
        #     issuing a presigned URL, so an over-cap upload is refused with a 413
        #     the client can act on rather than after the bytes are already staged.
        #     Legacy KBs return 0 and are never checked.
        reserved_bytes = await _reserve_managed_upload(assistant_id, request.size_bytes)

        # 2. Generate document_id and S3 key
        from apis.app_api.documents.services.storage_service import _get_s3_key, _sanitize_filename

        document_id = _generate_document_id()
        # Sanitize filename so the s3_key stored in DynamoDB matches the actual S3 object
        sanitized_filename = _sanitize_filename(request.filename)
        s3_key = _get_s3_key(assistant_id, document_id, sanitized_filename)

        try:
            # 3. Create document record in DynamoDB (status='uploading'). The
            #    declared size is persisted as sizeBytes — the value the ingestion
            #    step reconciles the true S3 size against (Requirement 12.3).
            _ = await create_document(
                assistant_id=assistant_id,
                filename=request.filename,
                content_type=request.content_type,
                size_bytes=request.size_bytes,
                s3_key=s3_key,
                document_id=document_id,
            )

            # 4. Generate presigned S3 URL
            presigned_url, _ = await generate_upload_url(
                assistant_id=assistant_id, document_id=document_id, filename=request.filename, content_type=request.content_type, expires_in=3600
            )
        except Exception:
            # A step after the reservation failed and the client never received a
            # URL, so this upload can never settle the bytes. Return the
            # reservation now rather than leak it (Requirement 12.6).
            await _release_managed_reservation(assistant_id, reserved_bytes)
            raise

        # 5. Return response
        return UploadUrlResponse(documentId=document_id, uploadUrl=presigned_url, expiresIn=3600)

    except ByteCapExceeded as e:
        # Requirement 12.12: the numbers make the error actionable — the user can
        # see how far over they are and request an elevated tier.
        used = "" if e.already_used is None else f" (currently using {e.already_used} bytes)"
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"This file ({e.requested} bytes) would put the assistant over its "
                f"{e.cap}-byte knowledge-base limit{used}. Delete unused documents "
                f"or request an elevated storage tier."
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating upload URL: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to generate upload URL: {str(e)}")


@router.post("/import", response_model=ImportDocumentsResponse, status_code=status.HTTP_202_ACCEPTED)
async def import_documents(
    assistant_id: str,
    request: ImportDocumentsRequest,
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> ImportDocumentsResponse:
    """Import files from a connected file source into an assistant's index.

    Creates one document record per file (status 'uploading', provenance
    populated) and returns immediately. A fire-and-forget task then downloads
    each file through the file-source adapter and stages it to S3, where the
    existing S3-event ingestion Lambda drives chunking/embedding — exactly as
    a device upload would.

    Args:
        assistant_id: Parent assistant identifier
        request: Connector id and the files selected for import
        current_user: Authenticated user from the session cookie
    """
    try:
        # 1. Verify the caller is the owner or an editor of this assistant.
        await _require_edit_permission(assistant_id, current_user)

        # 2. Resolve the connector to a usable adapter + access token. Raises
        #    404/403 (not a visible file source), 409 (not connected), or 503
        #    (workload context unavailable).
        provider, adapter = await resolve_file_source(
            request.connector_id, current_user, provider_repo, role_service
        )
        access_token = await require_file_source_token(provider, current_user.user_id)

        # 3. Create a document record per file. Values are provisional — the
        #    async task backfills the real filename/type/size/key after the
        #    adapter download (Google-native docs change extension on export).
        created: list = []
        items: list = []
        for file_ref in request.files:
            document_id = _generate_document_id()
            guessed_type, _ = mimetypes.guess_type(file_ref.name)
            provisional_key = _get_s3_key(
                assistant_id, document_id, _sanitize_filename(file_ref.name)
            )
            document = await create_document(
                assistant_id=assistant_id,
                filename=file_ref.name,
                content_type=guessed_type or "application/octet-stream",
                size_bytes=0,
                s3_key=provisional_key,
                document_id=document_id,
                provenance=DocumentProvenance(
                    source_connector_id=provider.provider_id,
                    source_adapter_key=adapter.metadata.key,
                    source_file_id=file_ref.file_id,
                    imported_by_user_id=current_user.user_id,
                ),
            )
            created.append(document)
            items.append((document_id, file_ref.file_id))

        # 4. Fire-and-forget the download + S3 stage (response already formed).
        asyncio.ensure_future(
            run_import(
                assistant_id=assistant_id,
                adapter=adapter,
                access_token=access_token,
                items=items,
            )
        )

        return ImportDocumentsResponse(
            documents=[
                DocumentResponse.model_validate(doc.model_dump(by_alias=True))
                for doc in created
            ]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error importing documents: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to import documents: {str(e)}")


@router.post("/{document_id}/upload-failed", response_model=DocumentResponse, status_code=status.HTTP_200_OK)
async def report_upload_failure(
    assistant_id: str,
    document_id: str,
    request: ReportUploadFailureRequest,
    current_user: User = Depends(get_current_user_from_session),
) -> DocumentResponse:
    """
    Report that a client-side S3 upload failed.

    Marks the document as 'failed' in DynamoDB so the frontend stops polling
    and displays the error. Called by the client when the presigned URL upload
    to S3 fails (network error, permission error, etc.).
    """
    try:
        # Verify caller has edit permission on the assistant
        assistant_owner_id = await _require_edit_permission(assistant_id, current_user)

        # Verify document exists (using the assistant's real owner_id)
        document = await get_document_service(assistant_id, document_id, assistant_owner_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        # Only allow marking as failed if still in 'uploading' state
        if document.status != "uploading":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Document is in '{document.status}' state, not 'uploading'. Cannot mark as upload failed.",
            )

        error_message = request.error or "Upload to S3 failed"
        updated = await update_document_status(
            assistant_id=assistant_id,
            document_id=document_id,
            status="failed",
            error_message=error_message,
            error_details=request.details,
        )

        if not updated:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update document status")

        # The client's upload to S3 never landed, so the bytes reserved at
        # request time will never be settled by the ingestion consumer. Return
        # them now (Requirement 12.6). settle_once makes this idempotent against a
        # concurrent stale-document sweep marking the same document failed.
        await release_reservation_if_managed(document)

        return DocumentResponse.model_validate(updated.model_dump(by_alias=True))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reporting upload failure: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to report upload failure: {str(e)}")


@router.get("", response_model=DocumentsListResponse, status_code=status.HTTP_200_OK)
async def list_documents(
    assistant_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None,
    current_user: User = Depends(get_current_user_from_session),
) -> DocumentsListResponse:
    """
    List all documents for an assistant with pagination.

    Owners and editors can list documents. Query pattern:
    - PK = AST#{assistant_id}
    - SK begins_with DOC#
    """
    try:
        # Verify owner/editor permission and get the assistant's real owner_id
        assistant_owner_id = await _require_edit_permission(assistant_id, current_user)

        # List documents
        documents, next_page_token = await list_assistant_documents(
            assistant_id=assistant_id, owner_id=assistant_owner_id, limit=limit, next_token=next_token
        )

        # Convert to response models
        document_responses = [DocumentResponse.model_validate(doc.model_dump(by_alias=True)) for doc in documents]

        return DocumentsListResponse(documents=document_responses, nextToken=next_page_token)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing documents: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list documents: {str(e)}")


@router.get("/{document_id}", response_model=DocumentResponse, status_code=status.HTTP_200_OK)
async def get_document(
    assistant_id: str, document_id: str, current_user: User = Depends(get_current_user_from_session)
) -> DocumentResponse:
    """Get document details and processing status. Owners and editors may read."""
    try:
        # Verify owner/editor permission and use the assistant's real owner_id
        assistant_owner_id = await _require_edit_permission(assistant_id, current_user)
        document = await get_document_service(assistant_id, document_id, assistant_owner_id)

        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        return DocumentResponse.model_validate(document.model_dump(by_alias=True))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving document: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve document: {str(e)}")


@router.get("/{document_id}/download", response_model=DownloadUrlResponse, status_code=status.HTTP_200_OK)
async def get_download_url(
    assistant_id: str, document_id: str, current_user: User = Depends(get_current_user_from_session)
) -> DownloadUrlResponse:
    """
    Generate presigned S3 URL for document download.

    Owners and editors may download. This endpoint is called on-demand when a
    user clicks to view/download a source document from a citation. The presigned
    URL is generated fresh each time to ensure it's valid.
    """
    try:
        assistant_owner_id = await _require_edit_permission(assistant_id, current_user)
        document = await get_document_service(assistant_id, document_id, assistant_owner_id)

        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        # Generate presigned download URL (1 hour expiration)
        expires_in = 3600
        download_url = await generate_download_url(
            s3_key=document.s3_key,
            expires_in=expires_in,
        )

        return DownloadUrlResponse(downloadUrl=download_url, filename=document.filename, expiresIn=expires_in)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating download URL: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to generate download URL: {str(e)}")


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    assistant_id: str, document_id: str, current_user: User = Depends(get_current_user_from_session)
) -> None:
    """Delete document using soft-delete + background cleanup pattern. Owners and editors may delete."""
    try:
        from apis.app_api.documents.services.document_service import soft_delete_document
        from apis.app_api.documents.services.cleanup_service import cleanup_document_resources

        assistant_owner_id = await _require_edit_permission(assistant_id, current_user)
        document = await soft_delete_document(assistant_id, document_id, assistant_owner_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        # Delete any sync policy covering this document so the schedule
        # cannot outlive its source (drive_file policies use the document
        # id as source_ref; crawl policies are cascaded with the CrawlJob)
        from apis.shared.sync_policies.service import delete_sync_policies_for_source
        await delete_sync_policies_for_source(assistant_id, document_id)

        # Fire-and-forget cleanup (response already sent as 204)
        asyncio.ensure_future(
            cleanup_document_resources(
                document_id=document.document_id,
                assistant_id=assistant_id,
                s3_key=document.s3_key,
                chunk_count=document.chunk_count,
                source_connector_id=document.source_connector_id,
                source_file_id=document.source_file_id,
            )
        )

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting document: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete document: {str(e)}")
