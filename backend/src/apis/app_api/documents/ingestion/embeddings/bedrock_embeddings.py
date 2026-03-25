"""Ingestion-specific embedding utilities (token validation & chunk splitting)

This module provides tiktoken-based token validation for the document
ingestion pipeline. The core embedding generation and vector store
operations live in apis.shared.embeddings.

Re-exports shared functions for backward compatibility with existing
ingestion code that imports from this module.
"""

import logging
import re
from typing import List

# Re-export shared functions so existing ingestion imports still work
from apis.shared.embeddings.bedrock_embeddings import (  # noqa: F401
    BEDROCK_EMBEDDING_CONFIG,
    delete_vectors_for_document,
    generate_embeddings,
    search_assistant_knowledgebase,
    store_embeddings_in_s3,
)

logger = logging.getLogger(__name__)

# --- Token validation safety net (tiktoken-based, ingestion only) ---

_tiktoken_encoder = None


def _get_tiktoken_encoder():
    global _tiktoken_encoder
    if _tiktoken_encoder is None:
        import tiktoken

        _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_encoder


def _count_tokens(text: str) -> int:
    return len(_get_tiktoken_encoder().encode(text))


def _split_oversized_chunk(chunk: str, max_tokens: int) -> List[str]:
    """
    Split a single oversized chunk into pieces that fit within max_tokens.
    Tries paragraph boundaries first, then sentence boundaries, then hard-cuts.
    """
    paragraphs = chunk.split("\n\n")
    if len(paragraphs) > 1:
        pieces = []
        current = ""
        for para in paragraphs:
            candidate = (current + "\n\n" + para).strip() if current else para
            if _count_tokens(candidate) <= max_tokens:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                if _count_tokens(para) > max_tokens:
                    pieces.extend(_split_by_sentences(para, max_tokens, _get_tiktoken_encoder()))
                else:
                    current = para
        if current:
            pieces.append(current)
        return pieces

    return _split_by_sentences(chunk, max_tokens, _get_tiktoken_encoder())


def _split_by_sentences(text: str, max_tokens: int, enc) -> List[str]:
    """Split text by sentence boundaries, falling back to hard token cuts."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 1:
        return _hard_split(text, max_tokens, enc)

    pieces = []
    current = ""
    for sent in sentences:
        candidate = (current + " " + sent).strip() if current else sent
        if _count_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if _count_tokens(sent) > max_tokens:
                pieces.extend(_hard_split(sent, max_tokens, enc))
            else:
                current = sent
    if current:
        pieces.append(current)
    return pieces


def _hard_split(text: str, max_tokens: int, enc) -> List[str]:
    """Last resort: split by token count."""
    tokens = enc.encode(text)
    pieces = []
    for i in range(0, len(tokens), max_tokens):
        pieces.append(enc.decode(tokens[i : i + max_tokens]))
    return pieces


def validate_and_split_chunks(chunks: List[str], max_tokens: int = 8000) -> List[str]:
    """
    Validate all chunks are within the token limit.
    Any oversized chunks are automatically split.
    Uses 8000 as default (safe margin under Titan's 8192 hard limit).

    This requires tiktoken and should only be called from the ingestion pipeline.
    """
    validated = []
    split_count = 0
    for chunk in chunks:
        token_count = _count_tokens(chunk)
        if token_count <= max_tokens:
            validated.append(chunk)
        else:
            logger.warning(f"Chunk exceeds token limit ({token_count} > {max_tokens}), splitting")
            sub_chunks = _split_oversized_chunk(chunk, max_tokens)
            validated.extend(sub_chunks)
            split_count += 1

    if split_count > 0:
        logger.info(f"Token validation: split {split_count} oversized chunk(s), {len(chunks)} -> {len(validated)} chunks")
    return validated


# Keep the old name as an alias for backward compatibility with tests
_validate_and_split_chunks = validate_and_split_chunks
