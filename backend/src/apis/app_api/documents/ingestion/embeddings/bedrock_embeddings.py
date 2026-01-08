"""Bedrock embedding generation and S3 vector store

Generates embeddings using Amazon Bedrock and stores them in S3
as a vector store for retrieval.
"""

import logging
import json
import os
import asyncio
import boto3
from typing import List, Dict, Any

# Module-level constants (read once at import time, but not validated until use)
_VECTOR_STORE_BUCKET_NAME = os.environ.get('ASSISTANTS_VECTOR_STORE_BUCKET_NAME')
_VECTOR_STORE_INDEX_NAME = os.environ.get('ASSISTANTS_VECTOR_STORE_INDEX_NAME')
AWS_REGION = os.environ.get('AWS_REGION', 'us-west-2')


def _get_vector_store_bucket() -> str:
    """Get vector store bucket name, validating if not set"""
    if not _VECTOR_STORE_BUCKET_NAME:
        raise ValueError("ASSISTANTS_VECTOR_STORE_BUCKET_NAME environment variable is required")
    return _VECTOR_STORE_BUCKET_NAME


def _get_vector_store_index() -> str:
    """Get vector store index name, validating if not set"""
    if not _VECTOR_STORE_INDEX_NAME:
        raise ValueError("ASSISTANTS_VECTOR_STORE_INDEX_NAME environment variable is required")
    return _VECTOR_STORE_INDEX_NAME

BEDROCK_EMBEDDING_CONFIG = {
    'model_id': 'amazon.titan-embed-text-v2:0',
    
    # TITAN LIMITS
    'max_tokens': 8192,           # Hard limit of the model
    
    # RAG OPTIMIZATION
    # We use 1024 tokens (approx 4,000 chars) for the chunk size.
    # This is large enough to hold ~3 paragraphs of context, but small enough
    # to make specific facts ("passwords", "dates") easy to find.
    'target_chunk_size': 1024,
    
    # 20% Overlap (approx 200 tokens)
    # Ensures we don't cut a sentence in half at the chunk border.
    'overlap_tokens': 200,
    
    'strategy': 'recursive'
}

logger = logging.getLogger(__name__)


def _get_aws_region() -> str:
    """Get AWS region from environment, defaulting to us-west-2"""
    return AWS_REGION


async def generate_embeddings(
    chunks: List[str],
) -> List[List[float]]:
    """
    Generate embeddings for text chunks using Bedrock (parallelized)
    
    Supported models:
    - amazon.titan-embed-text-v2:0 (1024 dimensions)
        
    Args:
        chunks: List of text chunks to embed
    
    Returns:
        List of embedding vectors (one per chunk)
    
    Raises:
        Exception: If Bedrock API call fails
    """
    bedrock_runtime = boto3.client('bedrock-runtime', region_name=AWS_REGION)
    
    logger.info(f"Generating embeddings for {len(chunks)} chunks in parallel...")
    
    async def get_single_embedding(chunk: str, index: int) -> List[float]:
        """Generate embedding for a single chunk"""
        loop = asyncio.get_event_loop()
        
        # Run synchronous boto3 call in thread pool to avoid blocking
        response = await loop.run_in_executor(
            None,
            lambda: bedrock_runtime.invoke_model(
                modelId=BEDROCK_EMBEDDING_CONFIG['model_id'],
                contentType='application/json',
                accept='application/json',
                body=json.dumps({'inputText': chunk})
            )
        )
        
        response_body = json.loads(response['body'].read())
        embedding = response_body.get('embedding')
        
        # Log progress for large batches
        if (index + 1) % 20 == 0:
            logger.info(f"Generated embeddings for {index + 1}/{len(chunks)} chunks...")
        
        return embedding
    
    # Generate all embeddings in parallel
    # Note: Bedrock has rate limits, but for typical document sizes (<100 chunks)
    # this parallelization is safe and significantly faster
    embeddings = await asyncio.gather(*[
        get_single_embedding(chunk, i) for i, chunk in enumerate(chunks)
    ])
    
    logger.info(f"All {len(embeddings)} embeddings generated successfully")
    return embeddings


async def store_embeddings_in_s3(
    assistant_id: str,
    document_id: str,
    chunks: List[str],
    embeddings: List[List[float]],
    metadata: Dict[str, Any]
) -> str:
    """
    Store embeddings directly into the S3 Vector Index (NOT just a file in S3)
    """
    
    # 1. Use the specific Vector client
    s3vectors = boto3.client('s3vectors', region_name=AWS_REGION)
    
    vector_bucket = _get_vector_store_bucket()
    vector_index = _get_vector_store_index()
    print(f"Storing {len(chunks)} chunks for {document_id} in {vector_bucket} with index {vector_index}")
    
    vectors_payload = []

    # 2. Build the entries manually (The "Correct" way)
    for i, chunk in enumerate(chunks):
        
        # Unique ID for this specific paragraph
        vector_key = f"{document_id}#{i}"
        vector_entry = {
            'key': vector_key,
            'data': {
                'float32': embeddings[i]  # Required wrapper
            },
            'metadata': {
                'text': chunk, 
                'document_id': document_id,
                'assistant_id': assistant_id,
                'source': metadata.get('filename', 'unknown')
            }
        }
        vectors_payload.append(vector_entry)

    # 3. Send to the Vector Index
    # Note: In a production app, you might chunk this into batches of 500
    s3vectors.put_vectors(
        vectorBucketName=vector_bucket,
        indexName=vector_index,
        vectors=vectors_payload
    )
    
    return f"Indexed {len(chunks)} chunks for {document_id}"


async def test_s3vector_dump():
    """
    Test the s3vector dump
    """
    client = boto3.client('s3vectors', region_name=AWS_REGION)

    response = client.list_vectors(
        vectorBucketName=_get_vector_store_bucket(),
        indexName=_get_vector_store_index(),
        maxResults=5,
        returnMetadata=True
    )

    print(f"Found {len(response.get('vectors', []))} vectors.")

    for v in response.get('vectors', []):
        print("---")
        print(f"ID: {v.get('key')}")
        # This proves your non-filterable text field is working:
        print(f"Content: {v.get('metadata', {}).get('text')[:100]}...")
        
async def delete_s3vector_data():
    client = boto3.client('s3vectors', region_name=AWS_REGION)

    vector_index = _get_vector_store_index()
    print(f"🧹 Starting cleanup for index: {vector_index}...")
    
    # 1. List all vectors
    # Note: If you have > 1000 vectors, you would need a loop with 'NextToken'
    # but for a demo, a single call usually grabs them all.
    response = client.list_vectors(
        vectorBucketName=_get_vector_store_bucket(),
        indexName=vector_index
    )
    
    vectors = response.get('vectors', [])
    
    if not vectors:
        print("✅ Index is already empty!")
        return

    # 2. Extract just the IDs (Keys)
    keys_to_delete = [v['key'] for v in vectors]
    print(f"found {len(keys_to_delete)} vectors to delete...")

    # 3. Delete them in a batch
    client.delete_vectors(
        vectorBucketName=_get_vector_store_bucket(),
        indexName=vector_index,
        keys=keys_to_delete
    )
    
    print(f"🗑️  Deleted {len(keys_to_delete)} vectors.")
    print("✅ Index is now clean.")
    
async def search_assistant_knowledgebase(assistant_id: str, query: str):
    client = boto3.client('s3vectors', region_name=AWS_REGION)
    
    # 1. Generate vector for the query
    query_embedding = await generate_embeddings([query])

    # 2. Query the Global Index with a STRICT Filter
    response = client.query_vectors(
        vectorBucketName=_get_vector_store_bucket(),
        indexName=_get_vector_store_index(),
        queryVector={
            'float32': query_embedding[0]
        },
        # THIS IS THE KEY PART:
        filter={
            "assistant_id": assistant_id 
        },
        topK=5,
        returnMetadata=True,
        returnDistance=True  # Get similarity distances
    )

    return response