"""Ingestion embedding utilities

Token validation and chunk splitting for the document ingestion pipeline.
Core embedding/vector operations are in apis.shared.embeddings.
Re-exports are provided for Lambda handler compatibility.
"""

from .bedrock_embeddings import (
    generate_embeddings,
    store_embeddings_in_s3,
    search_assistant_knowledgebase,
    validate_and_split_chunks,
)

__all__ = [
    "generate_embeddings",
    "store_embeddings_in_s3",
    "search_assistant_knowledgebase",
    "validate_and_split_chunks",
]
