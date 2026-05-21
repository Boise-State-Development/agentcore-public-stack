"""Document API request/response models"""

from dataclasses import dataclass
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Type alias for document processing status
DocumentStatus = Literal["uploading", "chunking", "embedding", "complete", "failed", "deleting"]


@dataclass(frozen=True)
class DocumentProvenance:
    """Origin metadata for a document imported from an external file source.

    Populated only on the import path; device uploads leave every provenance
    field on `Document` unset. Captured at import time so a document can
    later be re-indexed from its source — the information is unrecoverable
    if skipped.
    """

    source_connector_id: str
    source_adapter_key: str
    source_file_id: str
    imported_by_user_id: str
    source_etag: Optional[str] = None


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
    ttl: Optional[int] = Field(None, alias="ttl", description="DynamoDB TTL epoch timestamp for auto-expiry")
    # Source provenance — populated only when a document was imported from an
    # external file source (Google Drive, etc.); null for device uploads.
    # Required to support re-indexing a document from its origin later: they
    # record which connector/adapter/file the bytes came from and whose
    # credentials fetched them. Cheap to capture at import time, unrecoverable
    # if skipped.
    source_connector_id: Optional[str] = Field(None, alias="sourceConnectorId", description="OAuth connector the file was imported from")
    source_adapter_key: Optional[str] = Field(None, alias="sourceAdapterKey", description="File-source adapter that fetched the file")
    source_file_id: Optional[str] = Field(None, alias="sourceFileId", description="Provider-side opaque file identifier")
    source_etag: Optional[str] = Field(None, alias="sourceEtag", description="Provider-side version stamp at import time")
    imported_by_user_id: Optional[str] = Field(None, alias="importedByUserId", description="User whose credentials imported the file")


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


class ReportUploadFailureRequest(BaseModel):
    """Request body for reporting a client-side upload failure"""

    model_config = ConfigDict(populate_by_name=True)

    error: str = Field(..., description="User-friendly error message")
    details: Optional[str] = Field(None, description="Technical error details")


class ImportFileRef(BaseModel):
    """One file selected for import from a connected file source.

    `name` is the display name the file browser already showed the user; it
    seeds the document record so the row reads correctly during the brief
    'uploading' window. The async import task overwrites it with the real
    filename once the adapter download completes (Google-native docs change
    extension on export).
    """

    model_config = ConfigDict(populate_by_name=True)

    file_id: str = Field(..., alias="fileId", min_length=1, description="Provider-side opaque file identifier")
    name: str = Field(..., min_length=1, description="Display name from the file browser")


class ImportDocumentsRequest(BaseModel):
    """Request body for importing files from a connected file source."""

    model_config = ConfigDict(populate_by_name=True)

    connector_id: str = Field(..., alias="connectorId", min_length=1, description="OAuth connector to import from")
    files: List[ImportFileRef] = Field(..., min_length=1, max_length=50, description="Files selected for import")


class ImportDocumentsResponse(BaseModel):
    """Response listing the document records created for an import request.

    Each document starts in 'uploading' state; the SPA polls them the same
    way it polls a device upload.
    """

    model_config = ConfigDict(populate_by_name=True)

    documents: List[DocumentResponse] = Field(..., description="Created document records, each in 'uploading' state")
