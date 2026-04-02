"""Cleanup service for document resource deletion with retries.

Orchestrates deletion of vectors and S3 objects with exponential backoff
and jitter. Phases (vector deletion, S3 deletion) are independent — failure
of one does not prevent attempting the other.

Never raises exceptions — all failures are logged and swallowed.
"""

import asyncio
import logging
import os
import random
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


def _get_documents_bucket() -> str:
    """Get documents S3 bucket name from environment."""
    bucket = os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        raise ValueError("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME environment variable not set")
    return bucket


async def cleanup_document_resources(
    document_id: str,
    assistant_id: str,
    s3_key: str,
    chunk_count: Optional[int],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> bool:
    """
    Delete vectors and S3 source file with exponential backoff retries.

    Phase 1: Delete vectors (deterministic if chunk_count available, else probe-and-scan).
    Phase 2: Delete S3 source file.
    Phases are independent — failure of one does not prevent the other.

    Returns True only if both phases succeed. On True, hard-deletes the
    DynamoDB record. On failure, logs and leaves the record for TTL auto-expiry.

    Never raises exceptions.

    Args:
        document_id: The document identifier
        assistant_id: Parent assistant identifier
        s3_key: S3 object key for the source file
        chunk_count: Number of vector chunks (None triggers probe-and-scan fallback)
        max_retries: Maximum retry attempts per phase
        base_delay: Base delay in seconds for exponential backoff

    Returns:
        True if all resources were cleaned up successfully, False otherwise
    """
    try:
        vectors_deleted = await _delete_vectors_with_retries(
            document_id, chunk_count, max_retries, base_delay
        )
    except Exception as e:
        logger.error(f"Unexpected error in vector deletion for {document_id}: {e}", exc_info=True)
        vectors_deleted = False

    try:
        s3_deleted = await _delete_s3_with_retries(
            s3_key, max_retries, base_delay
        )
    except Exception as e:
        logger.error(f"Unexpected error in S3 deletion for {document_id}: {e}", exc_info=True)
        s3_deleted = False

    all_succeeded = vectors_deleted and s3_deleted

    if all_succeeded:
        try:
            from apis.app_api.documents.services.document_service import hard_delete_document

            await hard_delete_document(assistant_id, document_id)
        except Exception as e:
            logger.error(f"Failed to hard-delete document {document_id}: {e}", exc_info=True)
    else:
        logger.warning(
            f"Cleanup incomplete for {document_id}: vectors={vectors_deleted}, "
            f"s3={s3_deleted}. TTL will auto-expire."
        )

    return all_succeeded


async def _delete_vectors_with_retries(
    document_id: str,
    chunk_count: Optional[int],
    max_retries: int,
    base_delay: float,
) -> bool:
    """Delete vectors with exponential backoff + jitter retries.

    Uses deterministic deletion when chunk_count is available,
    falls back to probe-and-scan otherwise.
    """
    from apis.shared.embeddings.bedrock_embeddings import (
        delete_vectors_for_document,
        delete_vectors_for_document_deterministic,
    )

    for attempt in range(max_retries):
        try:
            if chunk_count is not None:
                await delete_vectors_for_document_deterministic(document_id, chunk_count)
            else:
                await delete_vectors_for_document(document_id)
            return True
        except Exception as e:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            logger.warning(
                f"Vector deletion attempt {attempt + 1}/{max_retries} failed for "
                f"{document_id}: {e}, retrying in {delay:.2f}s"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)

    logger.error(f"Vector deletion failed after {max_retries} attempts for {document_id}")
    return False


async def _delete_s3_with_retries(
    s3_key: str,
    max_retries: int,
    base_delay: float,
) -> bool:
    """Delete S3 source file with exponential backoff + jitter retries."""
    bucket = _get_documents_bucket()

    for attempt in range(max_retries):
        try:
            loop = asyncio.get_event_loop()
            s3_client = boto3.client("s3")
            await loop.run_in_executor(
                None,
                lambda: s3_client.delete_object(Bucket=bucket, Key=s3_key),
            )
            return True
        except Exception as e:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            logger.warning(
                f"S3 deletion attempt {attempt + 1}/{max_retries} failed for "
                f"{s3_key}: {e}, retrying in {delay:.2f}s"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)

    logger.error(f"S3 deletion failed after {max_retries} attempts for {s3_key}")
    return False


async def cleanup_assistant_documents(
    assistant_id: str,
    documents: list,
    max_retries: int = 3,
) -> tuple[int, int]:
    """
    Bulk cleanup for assistant deletion. Processes documents concurrently.
    Returns (success_count, failure_count).

    Each document is cleaned up via cleanup_document_resources, which
    hard-deletes the DynamoDB record on success. Never raises exceptions.

    Args:
        assistant_id: The assistant whose documents are being cleaned up
        documents: List of Document objects to clean up
        max_retries: Maximum retry attempts per document per phase

    Returns:
        Tuple of (success_count, failure_count)
    """
    if not documents:
        return (0, 0)

    try:
        results = await asyncio.gather(
            *(
                cleanup_document_resources(
                    document_id=doc.document_id,
                    assistant_id=assistant_id,
                    s3_key=doc.s3_key,
                    chunk_count=doc.chunk_count,
                    max_retries=max_retries,
                )
                for doc in documents
            ),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(
            f"Unexpected error in bulk cleanup for assistant {assistant_id}: {e}",
            exc_info=True,
        )
        return (0, len(documents))

    success_count = 0
    failure_count = 0
    for result in results:
        if result is True:
            success_count += 1
        else:
            failure_count += 1

    logger.info(
        f"Bulk cleanup for assistant {assistant_id}: "
        f"{success_count} succeeded, {failure_count} failed "
        f"out of {len(documents)} documents"
    )

    return (success_count, failure_count)
