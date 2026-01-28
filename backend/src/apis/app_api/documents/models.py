"""Document API request/response models"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Type alias for document processing status
DocumentStatus = Literal["uploading", "chunking", "embedding", "complete", "failed"]


class Document(BaseModel):
    """
    Complete document model (internal use)
    Stored in DynamoDB using adjacency list pattern:
    PK: AST#{assistant_id}
    SK: DOC#{document_id}
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    document_id: str = Field(..., alias="documentId", description="Document identifier")
    assistant_id: str = Field(..., alias="assistantId", description="Parent assistant identifier")
    filename: str = Field(..., description="Original filename")
    content_type: str = Field(..., alias="contentType", description="MIME type")
    size_bytes: int = Field(..., alias="sizeBytes", description="File size in bytes")
    s3_key: str = Field(..., alias="s3Key", description="S3 object key")
    vector_store_id: Optional[str] = Field(None, alias="vectorStoreId", description="S3 vector store identifier")
    status: DocumentStatus = Field(..., description="Processing status")
    error_message: Optional[str] = Field(None, alias="errorMessage", description="User-friendly error message for UI display")
    error_details: Optional[str] = Field(None, alias="errorDetails", description="Technical error details for debugging")
    chunk_count: Optional[int] = Field(None, alias="chunkCount", description="Number of chunks created")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 timestamp of last update")


class CreateDocumentRequest(BaseModel):
    """Request body for initiating document upload"""

    model_config = ConfigDict(populate_by_name=True)

    filename: str = Field(..., description="Original filename")
    content_type: str = Field(..., alias="contentType", description="MIME type")
    size_bytes: int = Field(..., alias="sizeBytes", description="File size in bytes")


class UploadUrlResponse(BaseModel):
    """Response containing presigned S3 upload URL"""

    model_config = ConfigDict(populate_by_name=True)

    document_id: str = Field(..., alias="documentId", description="Generated document identifier")
    upload_url: str = Field(..., alias="uploadUrl", description="Presigned S3 URL for upload")
    expires_in: int = Field(..., alias="expiresIn", description="URL expiration in seconds")


class DocumentResponse(BaseModel):
    """Response containing document data"""

    model_config = ConfigDict(populate_by_name=True)

    document_id: str = Field(..., alias="documentId", description="Document identifier")
    assistant_id: str = Field(..., alias="assistantId", description="Parent assistant identifier")
    filename: str = Field(..., description="Original filename")
    content_type: str = Field(..., alias="contentType", description="MIME type")
    size_bytes: int = Field(..., alias="sizeBytes", description="File size in bytes")
    status: DocumentStatus = Field(..., description="Processing status")
    error_message: Optional[str] = Field(None, alias="errorMessage", description="User-friendly error message for UI display")
    error_details: Optional[str] = Field(None, alias="errorDetails", description="Technical error details for debugging")
    chunk_count: Optional[int] = Field(None, alias="chunkCount", description="Number of chunks")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 creation timestamp")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 update timestamp")


class DocumentsListResponse(BaseModel):
    """Response for listing documents with pagination support"""

    model_config = ConfigDict(populate_by_name=True)

    documents: List[DocumentResponse] = Field(..., description="List of documents for the assistant")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for next page")


class DownloadUrlResponse(BaseModel):
    """Response containing presigned S3 download URL"""

    model_config = ConfigDict(populate_by_name=True)

    download_url: str = Field(..., alias="downloadUrl", description="Presigned S3 URL for download")
    filename: str = Field(..., description="Original filename")
    expires_in: int = Field(..., alias="expiresIn", description="URL expiration in seconds")
