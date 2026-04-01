"""Document management API routes"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.assistants.service import get_assistant
from apis.app_api.documents.models import CreateDocumentRequest, DocumentResponse, DocumentsListResponse, DownloadUrlResponse, UploadUrlResponse, ReportUploadFailureRequest
from apis.app_api.documents.services.document_service import _generate_document_id, create_document, list_assistant_documents, update_document_status
from apis.app_api.documents.services.document_service import get_document as get_document_service
from apis.app_api.documents.services.storage_service import generate_download_url, generate_upload_url
from apis.shared.auth.dependencies import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistants/{assistant_id}/documents", tags=["documents"])


@router.post("/upload-url", response_model=UploadUrlResponse, status_code=status.HTTP_200_OK)
async def generate_upload_url_endpoint(
    assistant_id: str, request: CreateDocumentRequest, user_id: str = Depends(get_current_user_id)
) -> UploadUrlResponse:
    """
    Generate presigned S3 URL for document upload

    Flow:
    1. Verify user owns the assistant
    2. Generate document_id
    3. Create document record in DynamoDB (status='uploading')
    4. Generate presigned S3 URL
    5. Return URL to client

    Args:
        assistant_id: Parent assistant identifier
        request: Document metadata (filename, content_type, size)
        user_id: Authenticated user ID from JWT

    Returns:
        UploadUrlResponse with presigned URL and document_id
    """
    try:
        # 1. Verify user owns the assistant
        assistant = await get_assistant(assistant_id, user_id)
        if not assistant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Assistant not found: {assistant_id}")

        # 2. Generate document_id and S3 key
        from apis.app_api.documents.services.storage_service import _get_s3_key, _sanitize_filename

        document_id = _generate_document_id()
        # Sanitize filename so the s3_key stored in DynamoDB matches the actual S3 object
        sanitized_filename = _sanitize_filename(request.filename)
        s3_key = _get_s3_key(assistant_id, document_id, sanitized_filename)

        # 3. Create document record in DynamoDB (status='uploading')
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

        # 5. Return response
        return UploadUrlResponse(documentId=document_id, uploadUrl=presigned_url, expiresIn=3600)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating upload URL: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to generate upload URL: {str(e)}")


@router.post("/{document_id}/upload-failed", response_model=DocumentResponse, status_code=status.HTTP_200_OK)
async def report_upload_failure(
    assistant_id: str, document_id: str, request: ReportUploadFailureRequest, user_id: str = Depends(get_current_user_id)
) -> DocumentResponse:
    """
    Report that a client-side S3 upload failed.

    Marks the document as 'failed' in DynamoDB so the frontend stops polling
    and displays the error. Called by the client when the presigned URL upload
    to S3 fails (network error, permission error, etc.).

    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        request: Error details from the client
        user_id: Authenticated user ID from JWT
    """
    try:
        # Verify document exists and user owns the assistant
        document = await get_document_service(assistant_id, document_id, user_id)
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

        return DocumentResponse.model_validate(updated.model_dump(by_alias=True))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reporting upload failure: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to report upload failure: {str(e)}")


@router.get("", response_model=DocumentsListResponse, status_code=status.HTTP_200_OK)
async def list_documents(
    assistant_id: str, limit: Optional[int] = None, next_token: Optional[str] = None, user_id: str = Depends(get_current_user_id)
) -> DocumentsListResponse:
    """
    List all documents for an assistant with pagination

    Query pattern:
    - PK = AST#{assistant_id}
    - SK begins_with DOC#

    Args:
        assistant_id: Parent assistant identifier
        limit: Maximum number of documents to return
        next_token: Pagination token
        user_id: Authenticated user ID from JWT

    Returns:
        DocumentsListResponse with documents and pagination token
    """
    try:
        # Verify assistant ownership
        assistant = await get_assistant(assistant_id, user_id)
        if not assistant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Assistant not found: {assistant_id}")

        # List documents
        documents, next_page_token = await list_assistant_documents(assistant_id=assistant_id, owner_id=user_id, limit=limit, next_token=next_token)

        # Convert to response models
        document_responses = [DocumentResponse.model_validate(doc.model_dump(by_alias=True)) for doc in documents]

        return DocumentsListResponse(documents=document_responses, nextToken=next_page_token)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing documents: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list documents: {str(e)}")


@router.get("/{document_id}", response_model=DocumentResponse, status_code=status.HTTP_200_OK)
async def get_document(assistant_id: str, document_id: str, user_id: str = Depends(get_current_user_id)) -> DocumentResponse:
    """
    Get document details and processing status

    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        user_id: Authenticated user ID from JWT

    Returns:
        DocumentResponse with current status and metadata
    """
    try:
        # Verify assistant ownership and get document
        document = await get_document_service(assistant_id, document_id, user_id)

        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        return DocumentResponse.model_validate(document.model_dump(by_alias=True))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving document: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve document: {str(e)}")


@router.get("/{document_id}/download", response_model=DownloadUrlResponse, status_code=status.HTTP_200_OK)
async def get_download_url(assistant_id: str, document_id: str, user_id: str = Depends(get_current_user_id)) -> DownloadUrlResponse:
    """
    Generate presigned S3 URL for document download

    This endpoint is called on-demand when a user clicks to view/download a source document
    from a citation. The presigned URL is generated fresh each time to ensure it's valid.

    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        user_id: Authenticated user ID from JWT

    Returns:
        DownloadUrlResponse with presigned URL and filename
    """
    try:
        # Verify assistant ownership and get document
        document = await get_document_service(assistant_id, document_id, user_id)

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
async def delete_document(assistant_id: str, document_id: str, user_id: str = Depends(get_current_user_id)) -> None:
    """Delete document using soft-delete + background cleanup pattern."""
    try:
        from apis.app_api.documents.services.document_service import soft_delete_document
        from apis.app_api.documents.services.cleanup_service import cleanup_document_resources

        document = await soft_delete_document(assistant_id, document_id, user_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        # Fire-and-forget cleanup (response already sent as 204)
        import asyncio
        asyncio.ensure_future(
            cleanup_document_resources(
                document_id=document.document_id,
                assistant_id=assistant_id,
                s3_key=document.s3_key,
                chunk_count=document.chunk_count,
            )
        )

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting document: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete document: {str(e)}")
