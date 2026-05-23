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
    source_connector_id: Optional[str] = None,
    source_file_id: Optional[str] = None,
) -> bool:
    """
    Delete vectors and S3 source file with exponential backoff retries.

    Phase 1: Delete vectors (deterministic if chunk_count available, else probe-and-scan).
    Phase 2: Delete S3 source file.
    Phases are independent — failure of one does not prevent the other.

    Returns True only if both phases succeed. On True, hard-deletes the
    DynamoDB record. On failure, logs and leaves the record for TTL auto-expiry.

    For web-source documents (`source_connector_id == 'web'`) the success
    path also cascades into `_cascade_delete_orphaned_crawl_jobs`, which
    purges any terminal-status CrawlJob row no longer referenced by a
    surviving doc. A running crawl is left alone — the crawler is still
    creating rows that would re-orphan it on every tick.

    Never raises exceptions.

    Args:
        document_id: The document identifier
        assistant_id: Parent assistant identifier
        s3_key: S3 object key for the source file
        chunk_count: Number of vector chunks (None triggers probe-and-scan fallback)
        max_retries: Maximum retry attempts per phase
        base_delay: Base delay in seconds for exponential backoff
        source_connector_id: Provenance connector id of the doc being deleted.
            Only `'web'` triggers the CrawlJob cascade; everything else is a no-op.
        source_file_id: Provenance file id of the doc being deleted (the page
            URL for web docs). Unused today — kept symmetric with the rest of
            the provenance fields and reserved for tighter prefix matching.

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

        if source_connector_id == "web":
            try:
                await _cascade_delete_orphaned_crawl_jobs(assistant_id)
            except Exception as e:
                logger.error(
                    f"CrawlJob cascade after deleting {document_id} failed: {e}",
                    exc_info=True,
                )
    else:
        logger.warning(
            f"Cleanup incomplete for {document_id}: vectors={vectors_deleted}, "
            f"s3={s3_deleted}. TTL will auto-expire."
        )

    return all_succeeded


async def _cascade_delete_orphaned_crawl_jobs(assistant_id: str) -> None:
    """Hard-delete terminal CrawlJob rows whose root_url no surviving web doc references.

    Called right after a web-source document is hard-deleted. The cascade is
    intentionally scoped to terminal-status crawls (`complete` / `failed`) —
    a running crawl is still spawning child docs, and deleting its row would
    break the SPA's watcher loop and cause the in-flight `finalize_crawl`
    update to fail its `attribute_exists` precondition.

    "Orphaned" is decided by URL prefix match: a CrawlJob is kept iff some
    surviving doc has `source_connector_id == 'web'` and `source_file_id`
    starts with the crawl's `root_url`. Prefix is good enough — the crawler
    only enqueues URLs that already satisfy the same-domain / same-root
    filter, so false positives are rare in practice and the worst case is
    that a row sticks around until its TTL fires.

    Never raises. Failures of the underlying list/delete calls are logged.
    """
    from apis.app_api.web_sources.crawl_repository import (
        hard_delete_crawl_job,
        list_all_crawls,
    )

    crawls = await list_all_crawls(assistant_id)
    terminal_crawls = [c for c in crawls if c.status in ("complete", "failed")]
    if not terminal_crawls:
        return

    surviving_web_urls = await _list_surviving_web_source_file_ids(assistant_id)

    for crawl in terminal_crawls:
        if any(url.startswith(crawl.root_url) for url in surviving_web_urls):
            continue
        await hard_delete_crawl_job(assistant_id, crawl.crawl_id)


async def _list_surviving_web_source_file_ids(assistant_id: str) -> list[str]:
    """Return source_file_id values for every surviving `web` document of an assistant.

    Reads the assistants table directly (not via `list_assistant_documents`)
    so the cascade does not require an ownership check — by the time we get
    here the deleting user has already been verified by the soft-delete
    upstream. Paginates internally; the typical assistant has a few hundred
    docs at most.
    """
    import os

    import boto3
    from boto3.dynamodb.conditions import Attr, Key

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        logger.error("DYNAMODB_ASSISTANTS_TABLE_NAME not set; skipping crawl cascade")
        return []

    table = boto3.resource("dynamodb").Table(table_name)
    urls: list[str] = []
    exclusive_start_key: Optional[dict] = None
    while True:
        kwargs: dict = {
            "KeyConditionExpression": Key("PK").eq(f"AST#{assistant_id}")
            & Key("SK").begins_with("DOC#"),
            "FilterExpression": Attr("sourceConnectorId").eq("web"),
            "ProjectionExpression": "sourceFileId, #st",
            "ExpressionAttributeNames": {"#st": "status"},
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        try:
            response = table.query(**kwargs)
        except Exception as e:
            logger.error(f"Failed to list web docs for cascade ({assistant_id}): {e}")
            return urls

        for item in response.get("Items", []):
            # Soft-deleted rows still exist in the table until cleanup hard-deletes
            # them; treat them as gone for cascade purposes.
            if item.get("status") == "deleting":
                continue
            file_id = item.get("sourceFileId")
            if isinstance(file_id, str):
                urls.append(file_id)

        exclusive_start_key = response.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break

    return urls


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
