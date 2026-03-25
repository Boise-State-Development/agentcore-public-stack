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
    For search queries (short strings), no validation is needed.

    Args:
        chunks: List of text chunks to embed

    Returns:
        List of embedding vectors (one per chunk)

    Raises:
        Exception: If Bedrock API call fails
    """
    bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    logger.info(f"Generating embeddings for {len(chunks)} chunks in parallel...")

    async def get_single_embedding(chunk: str, index: int) -> List[float]:
        """Generate embedding for a single chunk"""
        loop = asyncio.get_event_loop()

        # Run synchronous boto3 call in thread pool to avoid blocking
        response = await loop.run_in_executor(
            None,
            lambda: bedrock_runtime.invoke_model(
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
    Store embeddings directly into the S3 Vector Index (NOT just a file in S3)
    """
    s3vectors = boto3.client("s3vectors", region_name=AWS_REGION)

    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()
    print(f"Storing {len(chunks)} chunks for {document_id} in {vector_bucket} with index {vector_index}")

    vectors_payload = []

    for i, chunk in enumerate(chunks):
        vector_key = f"{document_id}#{i}"
        vector_entry = {
            "key": vector_key,
            "data": {
                "float32": embeddings[i]
            },
            "metadata": {
                "text": chunk,
                "document_id": document_id,
                "assistant_id": assistant_id,
                "source": metadata.get("filename", "unknown"),
            },
        }
        vectors_payload.append(vector_entry)

    s3vectors.put_vectors(vectorBucketName=vector_bucket, indexName=vector_index, vectors=vectors_payload)

    return f"Indexed {len(chunks)} chunks for {document_id}"


async def search_assistant_knowledgebase(assistant_id: str, query: str):
    """Search the S3 vector store for chunks relevant to the query."""
    client = boto3.client("s3vectors", region_name=AWS_REGION)

    # Generate vector for the query (short string, no token validation needed)
    query_embedding = await generate_embeddings([query])

    # Query the Global Index with a STRICT Filter
    response = client.query_vectors(
        vectorBucketName=_get_vector_store_bucket(),
        indexName=_get_vector_store_index(),
        queryVector={"float32": query_embedding[0]},
        filter={"assistant_id": assistant_id},
        topK=5,
        returnMetadata=True,
        returnDistance=True,
    )

    return response


async def delete_vectors_for_document(document_id: str) -> int:
    """
    Delete all vectors for a specific document from the S3 vector store.

    Vectors are stored with keys formatted as {document_id}#{chunk_index}.

    Args:
        document_id: The document identifier

    Returns:
        Number of vectors deleted
    """
    client = boto3.client("s3vectors", region_name=AWS_REGION)
    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()

    keys_to_delete = []
    next_token = None

    # List all vectors with pagination, filtering for this document
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
        vectors = response.get("vectors", [])

        document_prefix = f"{document_id}#"
        for vector in vectors:
            vector_key = vector.get("key", "")
            if vector_key.startswith(document_prefix):
                keys_to_delete.append(vector_key)

        next_token = response.get("nextToken")
        if not next_token:
            break

    if keys_to_delete:
        batch_size = 500
        deleted_count = 0

        for i in range(0, len(keys_to_delete), batch_size):
            batch = keys_to_delete[i : i + batch_size]
            client.delete_vectors(vectorBucketName=vector_bucket, indexName=vector_index, keys=batch)
            deleted_count += len(batch)

        logger.info(f"Deleted {deleted_count} vectors for document {document_id}")
        return deleted_count
    else:
        logger.info(f"No vectors found for document {document_id}")
        return 0
