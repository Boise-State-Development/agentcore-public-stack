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
import weakref
from typing import Optional

import boto3

from apis.shared.kb_backend.records import ENGINE_MANAGED

logger = logging.getLogger(__name__)

#: Background document cleanups one app-api task runs at once. Each one makes a
#: managed-KB delete, an S3 delete and several DynamoDB writes; a bulk delete of
#: 658 documents on 2026-11-16 started 658 of them unbounded, which drew
#: ``ThrottlingException`` from ``DeleteKnowledgeBaseDocuments`` and kept the
#: event loop busy enough that the ALB health check timed out. Cleanup runs
#: after the response (the rows are already soft-deleted and invisible), so
#: queueing behind this cap costs the user nothing.
MAX_CONCURRENT_CLEANUPS = 8

# One semaphore per event loop: an asyncio primitive binds to the loop that
# first waits on it, and tests run many loops in one process.
_cleanup_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)

# Assistants whose orphaned-crawl cascade is running, and those that asked for
# another pass while it ran. See `_cascade_coalesced`.
_cascade_running: set[str] = set()
_cascade_rerun: set[str] = set()


def _cleanup_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _cleanup_slots.get(loop)
    if slots is None:
        slots = asyncio.Semaphore(MAX_CONCURRENT_CLEANUPS)
        _cleanup_slots[loop] = slots
    return slots


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
    async with _cleanup_semaphore():
        return await _cleanup_document_resources(
            document_id=document_id,
            assistant_id=assistant_id,
            s3_key=s3_key,
            chunk_count=chunk_count,
            max_retries=max_retries,
            base_delay=base_delay,
            source_connector_id=source_connector_id,
        )


async def _cleanup_document_resources(
    *,
    document_id: str,
    assistant_id: str,
    s3_key: str,
    chunk_count: Optional[int],
    max_retries: int,
    base_delay: float,
    source_connector_id: Optional[str],
) -> bool:
    """The body of :func:`cleanup_document_resources`, run inside a cleanup slot."""
    # One engine lookup for both phases. A managed-KB document never had
    # vectors in the S3 Vectors index, so the legacy phase below must not fall
    # back to scanning the whole index for it — that scan is what blocked the
    # event loop for minutes on a 41-document agent deletion. An unreadable
    # record (``None``) keeps the conservative scan and lets the managed phase
    # resolve (and fail) on its own terms.
    engine = await _resolve_engine(assistant_id, document_id)

    try:
        vectors_deleted = await _delete_vectors_with_retries(
            document_id, chunk_count, max_retries, base_delay, scan_index=engine != ENGINE_MANAGED
        )
    except Exception as e:
        logger.error(f"Unexpected error in vector deletion for {document_id}: {e}", exc_info=True)
        vectors_deleted = False

    try:
        managed_deleted = await _delete_managed_documents_with_retries(
            assistant_id, document_id, max_retries, base_delay, engine=engine
        )
    except Exception as e:
        logger.error(
            f"Unexpected error in managed-KB deletion for {document_id}: {e}", exc_info=True
        )
        managed_deleted = False

    try:
        s3_deleted = await _delete_s3_with_retries(
            s3_key, max_retries, base_delay
        )
    except Exception as e:
        logger.error(f"Unexpected error in S3 deletion for {document_id}: {e}", exc_info=True)
        s3_deleted = False

    all_succeeded = vectors_deleted and managed_deleted and s3_deleted

    if all_succeeded:
        try:
            from apis.app_api.documents.services.document_service import hard_delete_document

            await hard_delete_document(assistant_id, document_id)
        except Exception as e:
            logger.error(f"Failed to hard-delete document {document_id}: {e}", exc_info=True)

        if source_connector_id == "web":
            try:
                await _cascade_coalesced(assistant_id)
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


async def _cascade_coalesced(assistant_id: str) -> None:
    """Run the orphaned-crawl cascade for an assistant, at most one at a time.

    The cascade reads every document row of the assistant, so running it once
    per deleted page made a bulk delete quadratic: deleting a 600-page web
    corpus ran 600 full-partition queries (2026-11-16). One pass after the last
    deletion decides the same thing, because it only asks which crawls no
    surviving page still references.

    So while a pass is running, a later caller only marks the assistant for one
    more pass and returns. The running caller repeats until no mark is left, so
    every hard-delete that finished before a pass began is seen by that pass,
    and a burst of N deletions costs a handful of passes instead of N. All of
    this runs on one event loop, so the check-and-mark needs no lock.
    """
    if assistant_id in _cascade_running:
        _cascade_rerun.add(assistant_id)
        return
    _cascade_running.add(assistant_id)
    try:
        while True:
            _cascade_rerun.discard(assistant_id)
            await _cascade_delete_orphaned_crawl_jobs(assistant_id)
            if assistant_id not in _cascade_rerun:
                return
    finally:
        _cascade_running.discard(assistant_id)


async def _cascade_delete_orphaned_crawl_jobs(assistant_id: str) -> None:
    """Hard-delete terminal CrawlJob rows whose root_url no surviving web doc references.

    Called right after a web-source document is hard-deleted. The cascade is
    intentionally scoped to terminal-status crawls (`complete` / `failed`) —
    a running crawl is still spawning child docs, and deleting its row would
    break the SPA's watcher loop and cause the in-flight `finalize_crawl`
    update to fail its `attribute_exists` precondition.

    "Orphaned" is decided by crawl scope: a CrawlJob is kept iff some
    surviving doc has `source_connector_id == 'web'` and a `source_file_id`
    inside the crawl (`url_utils.in_crawl_scope`, the rule the crawler
    follows links by). Two crawls whose sections overlap can keep each
    other's row alive; the worst case is that a row sticks around until its
    TTL fires.

    Never raises. Failures of the underlying list/delete calls are logged.
    """
    from apis.app_api.web_sources.crawl_repository import (
        hard_delete_crawl_job,
        list_all_crawls,
    )
    from apis.app_api.web_sources.url_utils import in_crawl_scope

    crawls = await list_all_crawls(assistant_id)
    terminal_crawls = [c for c in crawls if c.status in ("complete", "failed")]
    if not terminal_crawls:
        return

    surviving_web_urls = await _list_surviving_web_source_file_ids(assistant_id)

    for crawl in terminal_crawls:
        if any(in_crawl_scope(url, crawl.root_url) for url in surviving_web_urls):
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
            # A full-partition read, page by page: on the event loop it stalled
            # every request on the task (2026-11-16).
            response = await asyncio.to_thread(table.query, **kwargs)
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


async def _resolve_engine(assistant_id: str, document_id: str) -> Optional[str]:
    """The knowledge base's retrieval engine, or ``None`` when its record
    cannot be read. Off the loop: it is a DynamoDB read."""
    from apis.shared.kb_backend.records import get_kb_record, resolve_engine

    try:
        # App_KB_Id == assistant_id in this phase, so one value serves both.
        record = await asyncio.to_thread(get_kb_record, assistant_id, assistant_id)
        return resolve_engine(record)
    except Exception as e:  # noqa: BLE001 - each phase decides what an unknown engine means
        logger.error(
            f"could not resolve the retrieval engine for assistant {assistant_id} "
            f"while deleting {document_id}: {e}"
        )
        return None


async def _delete_vectors_with_retries(
    document_id: str,
    chunk_count: Optional[int],
    max_retries: int,
    base_delay: float,
    scan_index: bool = True,
) -> bool:
    """Delete vectors with exponential backoff + jitter retries.

    Uses deterministic deletion when chunk_count is available, falls back to
    probe-and-scan otherwise. ``scan_index=False`` (a managed-KB document)
    keeps the probe, which still removes any copy left from before the
    knowledge base was promoted, but never the full-index scan behind it.
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
                await delete_vectors_for_document(document_id, scan_index=scan_index)
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


async def _delete_managed_documents_with_retries(
    assistant_id: str,
    document_id: str,
    max_retries: int,
    base_delay: float,
    engine: Optional[str] = None,
) -> bool:
    """Remove the document from the managed knowledge base, if there is one.

    ``engine`` is the already-resolved retrieval engine when the caller has
    it (``cleanup_document_resources`` reads the record once for both
    phases); left ``None``, the record is read here.

    The mirror of the legacy vector deletion above, and the reason it exists:
    before this, deletion removed the S3 Vectors copy and the ``DOC#`` row but
    never touched the managed knowledge base. On a promoted knowledge base the
    content therefore stayed indexed forever. The corpus could only grow, at
    $5.00/GB-month, and each orphan kept consuming a slot in every ``top_k``
    before the status filter dropped it — so answers quietly got thinner while
    nothing errored.

    Returns ``True`` when there is nothing to do
    -------------------------------------------
    A knowledge base that is not promoted has no managed copy, so "no managed
    copy to delete" is success, not failure. Returning ``False`` there would
    block ``hard_delete_document`` for every legacy document in the system.

    Why a failure here must block the hard delete
    ---------------------------------------------
    The caller only hard-deletes the ``DOC#`` row when every phase succeeds, and
    that row is what the fail-closed status filter joins against. Leaving it in
    place is what keeps a still-indexed managed chunk from being served while
    this deletion is retried. Reporting success on a failed managed delete would
    remove the row *and* leave the content — the one combination that turns a
    storage leak into a disclosure.

    Deletion is idempotent
    ----------------------
    ``DeleteKnowledgeBaseDocuments`` on an absent document is not an error, so a
    retry after a partial failure is safe and needs no bookkeeping.
    """
    if engine is None:
        engine = await _resolve_engine(assistant_id, document_id)
    if engine is None:
        # Unlike the ingestion gate — where an unreadable record resolves to
        # legacy so the upload still gets indexed — an unreadable record HERE
        # must fail. If this knowledge base is in fact promoted, reporting
        # success would hard-delete the row that keeps its chunks unserved.
        logger.error(
            f"retrieval engine for assistant {assistant_id} is unknown while deleting "
            f"{document_id}; treating the managed deletion as failed so the document "
            f"record survives for a retry"
        )
        return False

    if engine != ENGINE_MANAGED:
        return True

    from apis.shared.kb_backend.managed_backend import ManagedKbBackend, ManagedKbNotProvisioned

    backend = ManagedKbBackend()
    for attempt in range(max_retries):
        try:
            await backend.delete_document(assistant_id, document_id)
            logger.info(
                f"deleted document {document_id} from the managed knowledge base "
                f"for assistant {assistant_id}"
            )
            return True
        except ManagedKbNotProvisioned:
            # Promoted but carrying no AWS identifiers is not a state a real
            # migration produces — `promote` runs after provisioning. Nothing was
            # ever indexed, so there is nothing to remove and no reason to retry.
            logger.warning(
                f"assistant {assistant_id} names the managed engine but has no AWS "
                f"identifiers; nothing to delete for {document_id}"
            )
            return True
        except Exception as e:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            logger.warning(
                f"Managed-KB deletion attempt {attempt + 1}/{max_retries} failed for "
                f"{document_id}: {e}, retrying in {delay:.2f}s"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)

    logger.error(
        f"Managed-KB deletion failed after {max_retries} attempts for {document_id}; "
        f"the document record is being kept so the status filter continues to hide it"
    )
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
