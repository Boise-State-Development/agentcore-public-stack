"""Shared embedding generation and vector store operations

Provides Bedrock Titan embedding generation and S3 vector store
operations used by both the app API and inference API.
"""

from .bedrock_embeddings import (
    BEDROCK_EMBEDDING_CONFIG,
    delete_vectors_for_document,
    generate_embeddings,
    search_assistant_knowledgebase,
    store_embeddings_in_s3,
)

__all__ = [
    "BEDROCK_EMBEDDING_CONFIG",
    "delete_vectors_for_document",
    "generate_embeddings",
    "search_assistant_knowledgebase",
    "store_embeddings_in_s3",
]
