# Requirements Document

## Introduction

This document specifies the requirements for reliable document deletion in the RAG assistant system. The current deletion pipeline deletes from DynamoDB, S3, and S3 Vectors sequentially, silently swallowing failures. When vector deletion fails, orphaned vectors cause stale RAG search results. The solution introduces a soft-delete + inline cleanup with retries + DynamoDB TTL pattern that treats DynamoDB as the single source of truth for document existence, ensuring deleted documents are immediately invisible to search and eventually cleaned up from all stores.

## Glossary

- **Document_Service**: The backend service layer responsible for DynamoDB operations on document records (create, read, update, delete).
- **Cleanup_Service**: The new service module that orchestrates deletion of vectors and S3 objects with retry logic and exponential backoff.
- **RAG_Search_Service**: The service that searches the S3 vector store for relevant chunks and returns formatted results to the chat endpoint.
- **Vector_Store**: The S3 Vectors index that stores document chunk embeddings for similarity search.
- **Documents_Bucket**: The S3 bucket that stores uploaded source document files.
- **Assistants_Table**: The DynamoDB table storing assistant and document records using an adjacency list pattern (PK=AST#{assistant_id}, SK=DOC#{document_id}).
- **Soft_Delete**: An atomic DynamoDB update that transitions a document's status to "deleting" and sets a TTL, without removing the record.
- **Hard_Delete**: The unconditional removal of a DynamoDB document record, performed only after successful cleanup of all associated resources.
- **TTL**: DynamoDB Time-To-Live, an epoch-second attribute that causes DynamoDB to automatically delete expired items.
- **Deterministic_Key_Deletion**: Generating vector keys from the stored chunk_count rather than probing or scanning the vector index.
- **Chunk_Count**: The number of embedding chunks created for a document, stored in the DynamoDB document record.
- **Delete_Endpoint**: The HTTP DELETE route at /assistants/{assistant_id}/documents/{document_id}.
- **Assistant_Delete_Endpoint**: The HTTP DELETE route at /assistants/{assistant_id} that removes an assistant and all its documents.

## Requirements

### Requirement 1: Soft-Delete Document Status Transition

**User Story:** As a user, I want document deletion to immediately mark the document as being deleted, so that the document becomes invisible to search results without waiting for full resource cleanup.

#### Acceptance Criteria

1. WHEN a user issues a delete request for a document, THE Document_Service SHALL atomically update the document status to "deleting" in the Assistants_Table.
2. WHEN the Document_Service performs a soft-delete, THE Document_Service SHALL set the TTL attribute to the current epoch time plus 7 days (604800 seconds).
3. WHEN the Document_Service performs a soft-delete, THE Document_Service SHALL update the updatedAt timestamp to the current time.
4. WHEN the Document_Service performs a soft-delete, THE Document_Service SHALL return the full document record including chunk_count and s3_key for use by the Cleanup_Service.
5. IF the document does not exist or the user does not own the parent assistant, THEN THE Document_Service SHALL return None and the Delete_Endpoint SHALL respond with HTTP 404.
6. WHEN a soft-delete is performed on a document already in "deleting" status, THE Document_Service SHALL treat the operation as idempotent and succeed without error.

### Requirement 2: Immediate API Response After Soft-Delete

**User Story:** As a user, I want the delete endpoint to respond immediately after marking the document, so that I do not experience delays from resource cleanup.

#### Acceptance Criteria

1. WHEN the soft-delete succeeds, THE Delete_Endpoint SHALL return HTTP 204 No Content to the client before initiating resource cleanup.
2. WHEN the soft-delete succeeds, THE Delete_Endpoint SHALL initiate inline resource cleanup as a background task after sending the response.

### Requirement 3: Search Path Document Status Filtering

**User Story:** As a user, I want RAG search results to exclude documents that have been deleted, so that I only see citations from valid, complete documents.

#### Acceptance Criteria

1. WHEN the RAG_Search_Service receives vector search results, THE RAG_Search_Service SHALL extract unique document_id values from the result metadata and look up their status in the Assistants_Table.
2. THE RAG_Search_Service SHALL return only chunks from documents where the status equals "complete" in the Assistants_Table.
3. WHEN a document record does not exist in the Assistants_Table for a given document_id, THE RAG_Search_Service SHALL exclude chunks from that document.
4. IF the Assistants_Table lookup fails due to a DynamoDB error, THEN THE RAG_Search_Service SHALL fall back to returning unfiltered vector results.

### Requirement 4: Inline Cleanup with Retries

**User Story:** As a system operator, I want resource cleanup to retry on transient failures, so that vectors and S3 objects are reliably removed without manual intervention.

#### Acceptance Criteria

1. WHEN the Cleanup_Service deletes vectors for a document, THE Cleanup_Service SHALL retry up to 3 times on failure using exponential backoff with jitter.
2. WHEN the Cleanup_Service deletes the S3 source file for a document, THE Cleanup_Service SHALL retry up to 3 times on failure using exponential backoff with jitter.
3. WHEN both vector deletion and S3 deletion succeed, THE Cleanup_Service SHALL invoke hard-delete to remove the DynamoDB document record.
4. IF vector deletion or S3 deletion fails after all retries, THEN THE Cleanup_Service SHALL log the failure and leave the DynamoDB record with status "deleting" for TTL auto-expiry.
5. THE Cleanup_Service SHALL process vector deletion and S3 deletion as independent phases, so that failure of one does not prevent attempting the other.

### Requirement 5: Deterministic Vector Key Deletion

**User Story:** As a system operator, I want vector deletion to use deterministic keys derived from chunk_count, so that deletion is efficient and does not require probing or scanning the vector index.

#### Acceptance Criteria

1. WHEN chunk_count is available on the document record, THE Cleanup_Service SHALL generate vector keys using the pattern "{document_id}#{i}" for i in range(chunk_count) and delete them in batches of 500.
2. WHEN chunk_count is not available on the document record, THE Cleanup_Service SHALL fall back to the existing probe-and-scan deletion method.
3. THE Cleanup_Service SHALL treat deletion of non-existent vector keys as a successful no-op.

### Requirement 6: DynamoDB TTL Backstop

**User Story:** As a system operator, I want documents stuck in "deleting" status to be automatically expired by DynamoDB TTL, so that failed cleanups do not leave permanent orphaned records.

#### Acceptance Criteria

1. THE Assistants_Table SHALL have DynamoDB TTL enabled on the "ttl" attribute.
2. WHEN a document is soft-deleted, THE Document_Service SHALL set the ttl attribute to an epoch-second value 7 days in the future.
3. WHILE a document record has a ttl attribute with an epoch value in the past, THE Assistants_Table SHALL automatically delete that record.

### Requirement 7: Document Status Model Extension

**User Story:** As a developer, I want the document model to support the "deleting" status and a TTL field, so that the soft-delete pattern can be implemented consistently.

#### Acceptance Criteria

1. THE Document model SHALL include "deleting" as a valid value in the DocumentStatus type.
2. THE Document model SHALL include an optional integer ttl field representing a DynamoDB TTL epoch timestamp.

### Requirement 8: Assistant Deletion with Bulk Document Cleanup

**User Story:** As a user, I want deleting an assistant to reliably clean up all associated documents, vectors, and S3 objects, so that no orphaned resources remain.

#### Acceptance Criteria

1. WHEN a user deletes an assistant, THE Assistant_Delete_Endpoint SHALL list all documents for the assistant and batch soft-delete them with TTL before deleting the assistant record.
2. WHEN the assistant record is hard-deleted, THE Assistant_Delete_Endpoint SHALL initiate background cleanup for all soft-deleted documents.
3. WHEN performing bulk document cleanup, THE Cleanup_Service SHALL process documents concurrently and return counts of successes and failures.
4. WHEN cleanup succeeds for an individual document during bulk cleanup, THE Cleanup_Service SHALL hard-delete that document's DynamoDB record.

### Requirement 9: Hard-Delete Document Record

**User Story:** As a system operator, I want a hard-delete operation that unconditionally removes a document record from DynamoDB, so that successfully cleaned-up documents do not linger in the table.

#### Acceptance Criteria

1. WHEN the Cleanup_Service has successfully deleted all vectors and the S3 source file for a document, THE Document_Service SHALL unconditionally remove the DynamoDB record for that document.
2. THE hard-delete operation SHALL NOT perform ownership verification, as ownership was already verified during the preceding soft-delete.

### Requirement 10: CDK Infrastructure TTL Configuration

**User Story:** As a DevOps engineer, I want the CDK stack to enable DynamoDB TTL on the assistants table, so that the TTL backstop mechanism functions correctly in all environments.

#### Acceptance Criteria

1. THE RagIngestionStack SHALL configure the Assistants_Table with timeToLiveAttribute set to "ttl".

### Requirement 11: List Documents Excludes Deleting Documents

**User Story:** As a user, I want the document list endpoint to exclude documents in "deleting" status, so that I only see documents that are active and available.

#### Acceptance Criteria

1. WHEN listing documents for an assistant, THE Document_Service SHALL exclude documents with status "deleting" from the returned list.
