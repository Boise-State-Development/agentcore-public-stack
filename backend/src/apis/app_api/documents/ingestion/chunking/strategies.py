"""Text chunking strategies

Different approaches for splitting text into semantic chunks.
"""

import logging
from typing import List
import tiktoken

from embeddings.bedrock_embeddings import BEDROCK_EMBEDDING_CONFIG

logger = logging.getLogger(__name__)


def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken"""
    encoding = tiktoken.get_encoding(encoding_name)
    return len(encoding.encode(text))


def _split_text_recursive(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
    separators: List[str],
    encoding_name: str = "cl100k_base"
) -> List[str]:
    """
    Recursively split text into chunks respecting token limits.
    
    This implements the same logic as RecursiveCharacterTextSplitter but
    without the langchain dependency.
    """
    # Base case: if text fits in one chunk, return it
    if _count_tokens(text, encoding_name) <= chunk_size:
        return [text]
    
    # Try splitting on each separator in order
    for separator in separators:
        if separator:
            splits = text.split(separator)
        else:
            # Empty separator means split character-by-character (last resort)
            splits = list(text)
        
        # If we got multiple splits, try to combine them into chunks
        if len(splits) > 1:
            chunks = []
            current_chunk = ""
            
            for split in splits:
                # Add separator back (except for the first split)
                if current_chunk and separator:
                    test_chunk = current_chunk + separator + split
                else:
                    test_chunk = current_chunk + split
                
                # Check if adding this split would exceed chunk size
                if _count_tokens(test_chunk, encoding_name) <= chunk_size:
                    current_chunk = test_chunk
                else:
                    # Save current chunk and start new one
                    if current_chunk:
                        chunks.append(current_chunk)
                    
                    # If the split itself is too large, recursively split it
                    if _count_tokens(split, encoding_name) > chunk_size:
                        sub_chunks = _split_text_recursive(
                            split, chunk_size, chunk_overlap, separators, encoding_name
                        )
                        chunks.extend(sub_chunks)
                        current_chunk = ""
                    else:
                        current_chunk = split
            
            # Add the last chunk
            if current_chunk:
                chunks.append(current_chunk)
            
            # Apply overlap between chunks
            if chunk_overlap > 0 and len(chunks) > 1:
                encoding = tiktoken.get_encoding(encoding_name)
                overlapped_chunks = []
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        overlapped_chunks.append(chunk)
                    else:
                        # Get overlap from previous chunk
                        prev_chunk = chunks[i - 1]
                        prev_tokens = encoding.encode(prev_chunk)
                        if len(prev_tokens) >= chunk_overlap:
                            overlap_tokens = prev_tokens[-chunk_overlap:]
                            overlap_text = encoding.decode(overlap_tokens)
                        else:
                            overlap_text = prev_chunk
                        overlapped_chunks.append(overlap_text + chunk)
                return overlapped_chunks
            
            return chunks if chunks else [text]
    
    # If no separator worked, return text as single chunk (will be handled upstream)
    return [text]


async def chunk_recursive(text: str) -> List[str]:
    """
    Chunk text into semantic chunks using token-aware recursive splitting.
    
    Uses tiktoken directly instead of langchain's RecursiveCharacterTextSplitter.
    """
    chunks = _split_text_recursive(
        text=text,
        chunk_size=BEDROCK_EMBEDDING_CONFIG['target_chunk_size'],
        chunk_overlap=BEDROCK_EMBEDDING_CONFIG['overlap_tokens'],
        separators=['\n\n', '\n', '. ', ' ', '']
    )
    return chunks