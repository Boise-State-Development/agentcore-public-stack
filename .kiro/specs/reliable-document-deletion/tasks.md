# Implementation Plan: Reliable Document Deletion

## Overview

Implement a soft-delete + inline cleanup with retries + DynamoDB TTL pattern for document deletion. Documents are atomically marked as "deleting" (immediately invisible to search), cleaned up with retries, and auto-expired by TTL if cleanup fails. Applies to both single-document and assistant-level deletion.

## Tasks

- [x] 1. Extend Document model and enable DynamoDB TTL
  - [x] 1.1 Add "deleting" status and TTL field to Document model
    - In `backend/src/apis/app_api/documents/models.py`, add `"deleting"` to the `DocumentStatus` Literal type
    - Add `ttl: Optional[int] = Field(None, alias="ttl", description="DynamoDB TTL epoch timestamp for auto-expiry")` to the `Document` model
    - _Requirements: 7.1, 7.2_

  - [x] 1.2 Enable TTL on the DynamoDB assistants table in CDK
    - In `infrastructure/lib/rag-ingestion-stack.ts`, add `timeToLiveAttribute: 'ttl'` to the `RagAssistantsTable` definition
    - _Requirements: 10.1, 6.1_

- [x] 2. Implement soft-delete and hard-delete in Document Service
  - [x] 2.1 Implement `soft_delete_document` function
    - In `backend/src/apis/app_api/documents/services/document_service.py`, add `soft_delete_document(assistant_id, document_id, owner_id, ttl_days=7)` that atomically sets `status="deleting"`, `ttl=now+604800`, `updatedAt=now` via DynamoDB `update_item` with conditional expression
    - Verify assistant ownership via `get_assistant`
    - Return full `Document` record (including `chunk_count`, `s3_key`) on success, `None` on not found / access denied
    - Treat re-deleting a document already in "deleting" status as idempotent (no error)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 2.2 Write property test for soft-delete postconditions
    - **Property 1: Soft-delete postconditions**
    - Using `hypothesis`, for any valid document status and ttl_days > 0, verify the returned document has `status="deleting"`, TTL = `now_epoch + ttl_days * 86400`, updated `updatedAt`, and preserved `chunk_count` / `s3_key`
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4**

  - [x] 2.3 Write property test for idempotent soft-delete
    - **Property 2: Idempotent soft-delete**
    - Using `hypothesis`, verify that calling `soft_delete_document` on a document already in "deleting" status succeeds without error
    - **Validates: Requirement 1.6**

  - [x] 2.4 Implement `hard_delete_document` function
    - In `backend/src/apis/app_api/documents/services/document_service.py`, add `hard_delete_document(assistant_id, document_id)` that unconditionally removes the DynamoDB record (no ownership check)
    - _Requirements: 9.1, 9.2_

  - [x] 2.5 Implement `batch_soft_delete_documents` function
    - In `backend/src/apis/app_api/documents/services/document_service.py`, add `batch_soft_delete_documents(assistant_id, document_ids, ttl_days=7)` that soft-deletes multiple documents for an assistant, returns count of documents marked
    - _Requirements: 8.1_

  - [x] 2.6 Write property test for bulk soft-delete coverage
    - **Property 8: Bulk soft-delete covers all documents**
    - Using `hypothesis`, for any list of N document IDs, verify `batch_soft_delete_documents` marks all N as "deleting" with TTL
    - **Validates: Requirement 8.1**

  - [x] 2.7 Write unit tests for soft-delete, hard-delete, and batch soft-delete
    - Test `soft_delete_document` with mocked DynamoDB: verify status transition, TTL calculation, conditional expression, not-found case
    - Test `hard_delete_document`: verify unconditional delete, no ownership check
    - Test `batch_soft_delete_documents`: verify all documents are marked
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 8.1, 9.1, 9.2_

- [x] 3. Implement deterministic vector deletion
  - [x] 3.1 Add `delete_vectors_for_document_deterministic` function
    - In `backend/src/apis/shared/embeddings/bedrock_embeddings.py`, add `delete_vectors_for_document_deterministic(document_id, chunk_count)` that generates keys `{document_id}#{i}` for `i in range(chunk_count)` and deletes in batches of 500
    - Treat deletion of non-existent keys as a no-op
    - _Requirements: 5.1, 5.3_

  - [x] 3.2 Write property test for deterministic vector key generation
    - **Property 7: Deterministic vector key generation**
    - Using `hypothesis`, for any `document_id` (text) and `chunk_count >= 0` (integers), verify exactly `chunk_count` keys are generated matching `{document_id}#{i}`, batched into groups of at most 500
    - **Validates: Requirement 5.1**

  - [x] 3.3 Write unit tests for deterministic vector deletion
    - Test key generation correctness, batch splitting at 500, zero chunk_count edge case
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement Cleanup Service with retries
  - [x] 5.1 Create `cleanup_service.py` with `cleanup_document_resources` function
    - Create new file `backend/src/apis/app_api/documents/services/cleanup_service.py`
    - Implement `cleanup_document_resources(document_id, assistant_id, s3_key, chunk_count, max_retries=3, base_delay=0.5)` with exponential backoff + jitter
    - Phase 1: Delete vectors (use deterministic if `chunk_count` available, fallback to probe-and-scan)
    - Phase 2: Delete S3 source file
    - Phases are independent — failure of one does not prevent the other
    - Return `True` only if both succeed; on `True`, call `hard_delete_document`
    - On failure, log and leave DynamoDB record for TTL auto-expiry
    - Never raise exceptions
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2_

  - [x] 5.2 Write property test for cleanup retry bounds
    - **Property 4: Cleanup retry bounded by max_retries**
    - Using `hypothesis`, for any `max_retries >= 1` and `base_delay > 0`, verify at most `max_retries` attempts are made, with delay following `base_delay * 2^attempt + jitter`
    - **Validates: Requirements 4.1, 4.2**

  - [x] 5.3 Write property test for failed cleanup preserving DynamoDB record
    - **Property 5: Failed cleanup preserves DynamoDB record**
    - Verify that when cleanup fails after all retries, `hard_delete_document` is NOT called and the record remains with `status="deleting"` and valid TTL
    - **Validates: Requirement 4.4**

  - [x] 5.4 Write property test for successful cleanup triggering hard-delete
    - **Property 6: Successful cleanup triggers hard-delete**
    - Verify that when both vector and S3 deletion succeed, `hard_delete_document` IS called
    - **Validates: Requirements 4.3, 9.1**

  - [x] 5.5 Implement `cleanup_assistant_documents` function
    - In `cleanup_service.py`, add `cleanup_assistant_documents(assistant_id, documents, max_retries=3)` that processes documents concurrently, returns `(success_count, failure_count)`, and hard-deletes each successfully cleaned document
    - _Requirements: 8.2, 8.3, 8.4_

  - [x] 5.6 Write property test for bulk cleanup count consistency
    - **Property 9: Bulk cleanup counts are consistent**
    - Using `hypothesis`, verify `success_count + failure_count == len(documents)` for any set of documents
    - **Validates: Requirement 8.3**

  - [x] 5.7 Write unit tests for cleanup_service
    - Test retry logic with mocked S3/S3Vectors failures, backoff timing, success/failure paths, independent phases
    - Test `cleanup_assistant_documents` concurrent processing and count consistency
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 8.2, 8.3, 8.4_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Add search path document status filtering
  - [x] 7.1 Modify `search_assistant_knowledgebase_with_formatting` in rag_service.py
    - After vector search, extract unique `document_id` values from result metadata
    - Batch-get document records from DynamoDB to check status
    - Filter out chunks from documents where `status != "complete"` or record doesn't exist
    - On DynamoDB lookup failure, fall back to returning unfiltered results (graceful degradation)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x] 7.2 Write property test for search filtering
    - **Property 3: Search results only contain complete documents**
    - Using `hypothesis`, for any mix of document statuses (complete, deleting, failed, uploading, missing), verify only chunks from `status="complete"` documents are returned
    - **Validates: Requirements 3.1, 3.2, 3.3**

  - [x] 7.3 Write unit tests for search filtering
    - Test filtering with mixed statuses, all-deleting, all-complete, missing records, DynamoDB error fallback
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 8. Update list documents to exclude deleting status
  - [x] 8.1 Modify `list_assistant_documents` in document_service.py
    - After querying DynamoDB, filter out documents with `status="deleting"` from the returned list
    - _Requirements: 11.1_

  - [x] 8.2 Write property test for list documents filtering
    - **Property 10: List documents excludes deleting status**
    - Using `hypothesis`, for any set of documents with mixed statuses, verify listing never returns documents with `status="deleting"`
    - **Validates: Requirement 11.1**

  - [x] 8.3 Write unit tests for list documents filtering
    - Test with mix of statuses, verify "deleting" documents are excluded
    - _Requirements: 11.1_

- [x] 9. Refactor delete endpoints to use soft-delete + background cleanup
  - [x] 9.1 Refactor single document DELETE endpoint
    - In `backend/src/apis/app_api/documents/routes.py`, replace the current `delete_document` handler:
    - Call `soft_delete_document` instead of `delete_document_service`
    - Return 204 immediately after soft-delete
    - Fire-and-forget `cleanup_document_resources` + `hard_delete_document` via `asyncio.ensure_future`
    - Remove inline S3 and vector deletion code from the route handler
    - _Requirements: 2.1, 2.2_

  - [x] 9.2 Refactor assistant DELETE endpoint
    - In `backend/src/apis/app_api/assistants/routes.py`, update `delete_assistant_endpoint`:
    - List all documents, batch soft-delete them with TTL
    - Hard-delete assistant record
    - Fire-and-forget `cleanup_assistant_documents` via `asyncio.ensure_future`
    - Remove the existing `_cleanup_assistant_resources` inline function
    - _Requirements: 8.1, 8.2_

  - [x] 9.3 Write unit tests for refactored delete endpoints
    - Test single document delete returns 204 after soft-delete, cleanup runs in background
    - Test assistant delete soft-deletes all docs before deleting assistant record
    - _Requirements: 2.1, 2.2, 8.1, 8.2_

- [x] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
    
## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The design uses Python throughout, so all implementation tasks use Python (backend) and TypeScript (CDK infrastructure)
