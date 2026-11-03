"""Bedrock embedding generation and S3 vector store operations

Generates embeddings using Amazon Bedrock Titan and provides
S3 vector store operations (store, search, delete).

NOTE: This module intentionally has NO tiktoken dependency.
Token validation/chunk splitting lives in the ingestion pipeline
(apis.app_api.documents.ingestion) where tiktoken is available.
"""

import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List

import boto3

# Module-level constants (read once at import time, but not validated until use)
_VECTOR_STORE_BUCKET_NAME = os.environ.get("S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME")
_VECTOR_STORE_INDEX_NAME = os.environ.get("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

BEDROCK_EMBEDDING_CONFIG = {
    "model_id": "amazon.titan-embed-text-v2:0",
    # TITAN LIMITS
    "max_tokens": 8192,  # Hard limit of the model
    # RAG OPTIMIZATION
    "target_chunk_size": 1024,
    "overlap_tokens": 200,
    "strategy": "recursive",
}

logger = logging.getLogger(__name__)

# One Bedrock client for every embedding, built on first use. A client per call
# cost ~250ms the first time in a process (botocore loads the service model) and,
# every time, a fresh connection pool — a new TCP+TLS handshake on each knowledge
# base search, which sits in front of the model's first token. boto3 clients are
# thread-safe, so the executor workers below can share it. Held here rather than
# in `apis.shared.aws_clients` because the kb-sync and rag-ingestion Lambda
# images copy this package without that module.
_bedrock_runtime_client: Any = None
# Guards the first build: boto3's default session is not thread-safe, and
# parallel embeddings can reach it from separate worker threads at once.
_bedrock_runtime_client_lock = threading.Lock()


def _get_bedrock_runtime_client() -> Any:
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        with _bedrock_runtime_client_lock:
            if _bedrock_runtime_client is None:
                _bedrock_runtime_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_runtime_client


# The same, for the knowledge-base search's `QueryVectors`: one client per
# process instead of a fresh one (and its service-model parse) per search.
_s3vectors_client: Any = None
_s3vectors_client_lock = threading.Lock()


def _get_s3vectors_client() -> Any:
    global _s3vectors_client
    if _s3vectors_client is None:
        with _s3vectors_client_lock:
            if _s3vectors_client is None:
                _s3vectors_client = boto3.client("s3vectors", region_name=AWS_REGION)
    return _s3vectors_client


def _get_vector_store_bucket() -> str:
    """Get vector store bucket name, validating if not set"""
    if not _VECTOR_STORE_BUCKET_NAME:
        raise ValueError("S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME environment variable is required")
    return _VECTOR_STORE_BUCKET_NAME


def _get_vector_store_index() -> str:
    """Get vector store index name, validating if not set"""
    if not _VECTOR_STORE_INDEX_NAME:
        raise ValueError("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME environment variable is required")
    return _VECTOR_STORE_INDEX_NAME


async def generate_embeddings(chunks: List[str]) -> List[List[float]]:
    """
    Generate embeddings for text chunks using Bedrock (parallelized)

    Supported models:
    - amazon.titan-embed-text-v2:0 (1024 dimensions)

    IMPORTANT: This function does NOT validate token counts. Callers that
    process large documents should validate/split chunks before calling this.

    Search queries are length-capped by the caller, not here: the facade applies
    `apis.shared.kb_backend.query_guard.clamp_query` before dispatch. This used to
    say no validation was needed for queries, which was true only because Titan v2
    tolerates roughly 32,000 characters. Managed Knowledge Base caps `Retrieve`
    input at 10,000 and that quota is not adjustable, so the assumption no longer
    holds for every backend.

    Args:
        chunks: List of text chunks to embed

    Returns:
        List of embedding vectors (one per chunk)

    Raises:
        Exception: If Bedrock API call fails
    """
    logger.info(f"Generating embeddings for {len(chunks)} chunks in parallel...")

    async def get_single_embedding(chunk: str, index: int) -> List[float]:
        """Generate embedding for a single chunk"""
        loop = asyncio.get_event_loop()

        # Run synchronous boto3 call in thread pool to avoid blocking; the
        # client is fetched there too, so its first build stays off the loop.
        response = await loop.run_in_executor(
            None,
            lambda: _get_bedrock_runtime_client().invoke_model(
                modelId=BEDROCK_EMBEDDING_CONFIG["model_id"],
                contentType="application/json",
                accept="application/json",
                body=json.dumps({"inputText": chunk}),
            ),
        )

        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding")

        # Log progress for large batches
        if (index + 1) % 20 == 0:
            logger.info(f"Generated embeddings for {index + 1}/{len(chunks)} chunks...")

        return embedding

    # Generate all embeddings in parallel
    embeddings = await asyncio.gather(*[get_single_embedding(chunk, i) for i, chunk in enumerate(chunks)])

    logger.info(f"All {len(embeddings)} embeddings generated successfully")
    return embeddings


async def store_embeddings_in_s3(
    assistant_id: str, document_id: str, chunks: List[str], embeddings: List[List[float]], metadata: Dict[str, Any]
) -> str:
    """
    Store embeddings directly into the S3 Vector Index in batches.
    """
    s3vectors = boto3.client("s3vectors", region_name=AWS_REGION)

    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()
    logger.info(f"Storing {len(chunks)} chunks for {document_id} in {vector_bucket} (index: {vector_index})")

    BATCH_SIZE = 50  # Safe batch size to stay under S3 Vectors request body limit

    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, len(chunks))
        batch_payload = []

        for i in range(batch_start, batch_end):
            batch_payload.append({
                "key": f"{document_id}#{i}",
                "data": {"float32": embeddings[i]},
                "metadata": {
                    "text": chunks[i],
                    "document_id": document_id,
                    "assistant_id": assistant_id,
                    "source": metadata.get("filename", "unknown"),
                },
            })

        s3vectors.put_vectors(vectorBucketName=vector_bucket, indexName=vector_index, vectors=batch_payload)

        if batch_end % 500 == 0 or batch_end == len(chunks):
            logger.info(f"Stored {batch_end}/{len(chunks)} vectors")

    return f"Indexed {len(chunks)} chunks for {document_id}"


async def search_assistant_knowledgebase(assistant_id: str, query: str):
    """Search the S3 vector store for chunks relevant to the query.

    Both round trips — the query embedding and ``QueryVectors`` — run on ONE
    worker thread, back to back. The turn path starts this search before the
    agent build and awaits it after (docs/specs/turn-path-ttft.md §5 P3b), and
    a cold build holds the event loop while it runs: had the embedding been
    awaited on the loop, ``QueryVectors`` could not be sent until the build
    let go, and only the first hop would overlap. The ``s3vectors`` client is
    cached per process, so its service-model parse is paid once.
    """
    return await asyncio.to_thread(_search_assistant_knowledgebase_sync, assistant_id, query)


def _search_assistant_knowledgebase_sync(assistant_id: str, query: str) -> Dict[str, Any]:
    # Generate vector for the query. Length is already capped upstream by the
    # facade's query clamp (10,000 chars, the Managed KB Retrieve limit).
    response = _get_bedrock_runtime_client().invoke_model(
        modelId=BEDROCK_EMBEDDING_CONFIG["model_id"],
        contentType="application/json",
        accept="application/json",
        body=json.dumps({"inputText": query}),
    )
    query_embedding = json.loads(response["body"].read()).get("embedding")

    # Query the Global Index with a STRICT Filter
    return _get_s3vectors_client().query_vectors(
        vectorBucketName=_get_vector_store_bucket(),
        indexName=_get_vector_store_index(),
        queryVector={"float32": query_embedding},
        filter={"assistant_id": assistant_id},
        topK=5,
        returnMetadata=True,
        returnDistance=True,
    )


#: The S3 Vectors ``GetVectors`` limit on ``keys`` per call.
GET_VECTORS_MAX_KEYS = 100


# ---------------------------------------------------------------------------
# Deletion
#
# Every function below is ``async`` for its callers' sake but does its work on
# ``asyncio.to_thread``: boto3 is synchronous, and the full-index ``list_vectors``
# fallback takes seconds per document. Run on the loop, as these once were, an
# agent deletion with 41 documents blocked app-api's event loop for about
# three and a half minutes, no ``/health`` was answered, and ECS replaced the
# task with every in-flight request on it (production, 2026-10-19, -20, -30).
# ---------------------------------------------------------------------------

_DELETE_BATCH_SIZE = 500


def _delete_keys(client: Any, vector_bucket: str, vector_index: str, keys: List[str]) -> int:
    for i in range(0, len(keys), _DELETE_BATCH_SIZE):
        batch = keys[i : i + _DELETE_BATCH_SIZE]
        client.delete_vectors(vectorBucketName=vector_bucket, indexName=vector_index, keys=batch)
    return len(keys)


def _delete_vectors_for_document_sync(document_id: str, scan_index: bool) -> int:
    client = boto3.client("s3vectors", region_name=AWS_REGION)
    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()

    existing_keys: List[str] = []
    # GetVectors accepts at most 100 keys. At 500 every probe was rejected
    # (ValidationException) and each delete fell through to listing the whole index.
    probe_batch_size = GET_VECTORS_MAX_KEYS
    probe_offset = 0
    max_probe = 10000  # Safety limit

    # Strategy 1: Probe for keys using the known pattern {document_id}#{index}
    while probe_offset < max_probe:
        candidate_keys = [f"{document_id}#{i}" for i in range(probe_offset, probe_offset + probe_batch_size)]
        try:
            response = client.get_vectors(
                vectorBucketName=vector_bucket,
                indexName=vector_index,
                keys=candidate_keys,
            )
            found = [v["key"] for v in response.get("vectors", [])]
            if not found:
                # No more vectors in this range — stop probing
                break
            existing_keys.extend(found)
        except Exception as e:
            logger.warning(f"GetVectors probe failed at offset {probe_offset}: {e}")
            break

        probe_offset += probe_batch_size

    # Strategy 2: Fallback to list scan if probe found nothing. This walks the
    # WHOLE index (every document of every assistant, 1,000 keys a page), so
    # it costs seconds per call and is only worth it where the vectors could
    # exist under a key the probe does not know. A managed-KB document never
    # had its vectors here, so its caller passes ``scan_index=False``.
    if not existing_keys and scan_index:
        logger.info(f"Key probe found no vectors for {document_id}, falling back to list scan")
        next_token = None
        document_prefix = f"{document_id}#"

        while True:
            list_params = {
                "vectorBucketName": vector_bucket,
                "indexName": vector_index,
                "maxResults": 1000,
            }
            if next_token:
                list_params["nextToken"] = next_token

            response = client.list_vectors(**list_params)
            for vector in response.get("vectors", []):
                if vector.get("key", "").startswith(document_prefix):
                    existing_keys.append(vector["key"])

            next_token = response.get("nextToken")
            if not next_token:
                break

    if not existing_keys:
        logger.info(f"No vectors found for document {document_id}")
        return 0

    deleted_count = _delete_keys(client, vector_bucket, vector_index, existing_keys)
    logger.info(f"Deleted {deleted_count} vectors for document {document_id}")
    return deleted_count


async def delete_vectors_for_document(document_id: str, *, scan_index: bool = True) -> int:
    """
    Delete all vectors for a specific document from the S3 vector store.

    Vectors are stored with keys formatted as {document_id}#{chunk_index}
    where chunk_index is a sequential integer starting at 0.

    Uses a probe-and-delete strategy: generates candidate keys in batches,
    checks which exist via GetVectors, then deletes them. Stops probing
    when a batch returns no results. With ``scan_index`` (the default) a
    probe that finds nothing falls back to a full list scan of the index,
    which handles unexpected key formats at the cost of seconds per call;
    pass ``scan_index=False`` for a document whose vectors were never
    written to this index (a managed knowledge base), where the scan can
    only ever come back empty.

    Runs off the event loop.

    Args:
        document_id: The document identifier
        scan_index: Whether an empty probe may fall back to the full index scan

    Returns:
        Number of vectors deleted
    """
    return await asyncio.to_thread(_delete_vectors_for_document_sync, document_id, scan_index)


def _delete_vectors_for_document_deterministic_sync(document_id: str, chunk_count: int) -> int:
    client = boto3.client("s3vectors", region_name=AWS_REGION)
    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()

    keys = [f"{document_id}#{i}" for i in range(chunk_count)]
    _delete_keys(client, vector_bucket, vector_index, keys)
    logger.info(f"Deterministic delete: sent {chunk_count} keys for document {document_id}")
    return chunk_count


async def delete_vectors_for_document_deterministic(
    document_id: str,
    chunk_count: int,
) -> int:
    """
    Delete vectors using deterministic keys: {document_id}#{i} for i in range(chunk_count).
    No probing, no list-scan. O(chunk_count) with a single batch delete call.

    Deletion of non-existent keys is a no-op in the S3 Vectors API. Runs off
    the event loop.

    Args:
        document_id: The document identifier
        chunk_count: Number of chunks to delete

    Returns:
        Number of keys sent for deletion (= chunk_count)

    Raises:
        Exception: If S3 Vectors API call fails (caller handles retries)
    """
    if chunk_count == 0:
        return 0
    return await asyncio.to_thread(_delete_vectors_for_document_deterministic_sync, document_id, chunk_count)


def _delete_vector_tail_sync(document_id: str, from_index: int, to_index: int) -> int:
    client = boto3.client("s3vectors", region_name=AWS_REGION)
    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()

    keys = [f"{document_id}#{i}" for i in range(from_index, to_index)]
    _delete_keys(client, vector_bucket, vector_index, keys)
    logger.info(
        f"Tail delete: sent {len(keys)} keys ({from_index}..{to_index - 1}) for document {document_id}"
    )
    return len(keys)


async def delete_vector_tail(document_id: str, from_index: int, to_index: int) -> int:
    """
    Delete vectors {document_id}#{i} for i in [from_index, to_index).

    Shrinkage cleanup for KB sync re-ingestion: when a re-indexed document
    produces fewer chunks than before, the in-place vector overwrite leaves
    stale tail chunks behind — this removes exactly that tail. Deletion of
    non-existent keys is a no-op in the S3 Vectors API. Runs off the event
    loop.

    Returns:
        Number of keys sent for deletion (0 if the range is empty)
    """
    if to_index <= from_index:
        return 0
    return await asyncio.to_thread(_delete_vector_tail_sync, document_id, from_index, to_index)


def _delete_vectors_for_assistant_sync(assistant_id: str) -> int:
    client = boto3.client("s3vectors", region_name=AWS_REGION)
    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()

    keys_to_delete: List[str] = []
    next_token = None

    while True:
        list_params = {
            "vectorBucketName": vector_bucket,
            "indexName": vector_index,
            "maxResults": 1000,
            "returnMetadata": True,
        }
        if next_token:
            list_params["nextToken"] = next_token

        response = client.list_vectors(**list_params)
        for vector in response.get("vectors", []):
            metadata = vector.get("metadata", {})
            if metadata.get("assistant_id") == assistant_id:
                keys_to_delete.append(vector["key"])

        next_token = response.get("nextToken")
        if not next_token:
            break

    if not keys_to_delete:
        logger.info(f"No vectors found for assistant {assistant_id}")
        return 0

    deleted_count = _delete_keys(client, vector_bucket, vector_index, keys_to_delete)
    logger.info(f"Deleted {deleted_count} vectors for assistant {assistant_id}")
    return deleted_count


async def delete_vectors_for_assistant(assistant_id: str) -> int:
    """
    Delete ALL vectors belonging to an assistant from the S3 vector store.

    Used when deleting an entire assistant to prevent orphaned vectors.
    Scans the index filtering by assistant_id metadata via list + client-side
    filter. Runs off the event loop.

    Args:
        assistant_id: The assistant identifier

    Returns:
        Number of vectors deleted
    """
    return await asyncio.to_thread(_delete_vectors_for_assistant_sync, assistant_id)
