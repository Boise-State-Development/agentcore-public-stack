"""Embedding generation for text chunks

Generates vector embeddings using AWS Bedrock models.
"""

from .bedrock_embeddings import (
    generate_embeddings,
    store_embeddings_in_s3,
    search_assistant_knowledgebase
)

__all__ = [
    'generate_embeddings',
    'store_embeddings_in_s3'
    'search_assistant_knowledgebase'
]
