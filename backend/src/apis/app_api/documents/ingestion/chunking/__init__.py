"""Text chunking strategies for semantic segmentation

Splits long documents into smaller chunks for embedding generation.
"""

from .strategies import (
    chunk_recursive
)

__all__ = [
    'chunk_recursive'
]
