"""Document management API routes"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from apis.app_api.assistants.services.assistant_service import get_assistant
from apis.app_api.documents.models import CreateDocumentRequest, DocumentResponse, DocumentsListResponse, UploadUrlResponse
from apis.app_api.documents.services.document_service import _generate_document_id, create_document, list_assistant_documents
from apis.app_api.documents.services.document_service import delete_document as delete_document_service
from apis.app_api.documents.services.document_service import get_document as get_document_service
from apis.app_api.documents.services.storage_service import generate_upload_url
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
        from apis.app_api.documents.services.storage_service import _get_s3_key

        document_id = _generate_document_id()
        s3_key = _get_s3_key(assistant_id, document_id, request.filename)

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


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(assistant_id: str, document_id: str, user_id: str = Depends(get_current_user_id)) -> None:
    """
    Delete document from DynamoDB, S3, and vector store

    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        user_id: Authenticated user ID from JWT
    """
    try:
        # Get document first to get S3 key
        document = await get_document_service(assistant_id, document_id, user_id)

        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document not found: {document_id}")

        # Delete from DynamoDB
        success = await delete_document_service(assistant_id, document_id, user_id)

        if not success:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete document")

        # Delete S3 object
        try:
            import boto3

            from apis.app_api.documents.services.storage_service import _get_documents_bucket

            s3_client = boto3.client("s3")
            bucket = _get_documents_bucket()

            s3_client.delete_object(Bucket=bucket, Key=document.s3_key)

            logger.info(f"Deleted S3 object: {document.s3_key}")
        except Exception as s3_error:
            # Log but don't fail - DynamoDB deletion succeeded
            logger.warning(f"Failed to delete S3 object {document.s3_key}: {s3_error}")

        # Delete vector store objects
        try:
            from apis.app_api.documents.ingestion.embeddings.bedrock_embeddings import delete_vectors_for_document

            deleted_count = await delete_vectors_for_document(document_id)
            logger.info(f"Deleted {deleted_count} vectors for document {document_id}")
        except Exception as vector_error:
            # Log but don't fail - DynamoDB and S3 deletion succeeded
            logger.warning(f"Failed to delete vectors for document {document_id}: {vector_error}")

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting document: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete document: {str(e)}")
