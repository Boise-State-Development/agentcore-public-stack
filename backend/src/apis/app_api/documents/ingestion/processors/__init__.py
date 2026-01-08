"""Document processors for different file types

All document formats are processed through Docling for unified handling.
Docling supports PDF, DOCX, PPTX, TXT, MD, RTF and other formats.
"""

from .docling_processor import (
    process_with_docling,
    is_docling_supported
)

__all__ = [
    'process_with_docling',
    'is_docling_supported'
]
