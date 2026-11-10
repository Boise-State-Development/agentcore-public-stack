"""
Document format detection and content block creation
"""
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class DocumentHandler:
    """Handles document format detection and ContentBlock creation"""

    # Supported document formats and extensions. Every document extension the
    # upload allowlist admits (``ALLOWED_EXTENSIONS`` in
    # ``apis/shared/files/models.py``) must be listed here, or the file is
    # admitted and then silently dropped from the turn. ``.pptx`` is the one
    # exception: it never goes inline (PowerPoint tools), and is excluded on
    # purpose. ``test_document_handler_covers_admitted_extensions`` pins this.
    SUPPORTED_EXTENSIONS = {
        ".pdf", ".csv", ".doc", ".docx",
        ".xls", ".xlsx", ".html", ".txt", ".md", ".sql"
    }

    # Extension to format mapping
    FORMAT_MAP = {
        ".pdf": "pdf",
        ".csv": "csv",
        ".doc": "doc",
        ".docx": "docx",
        ".xls": "xls",
        ".xlsx": "xlsx",
        ".html": "html",
        ".txt": "txt",
        ".md": "md",
        # Admitted as text/plain; Bedrock has no sql format, so it reads as txt.
        ".sql": "txt",
    }

    @staticmethod
    def get_document_format(filename: str) -> str:
        """
        Determine document format from filename

        Args:
            filename: Filename with extension

        Returns:
            str: Document format (pdf, csv, doc, etc.)
        """
        filename_lower = filename.lower()

        for ext, fmt in DocumentHandler.FORMAT_MAP.items():
            if filename_lower.endswith(ext):
                return fmt

        return "txt"  # default

    @staticmethod
    def is_document(filename: str) -> bool:
        """
        Check if file is a supported document

        Args:
            filename: Filename with extension

        Returns:
            bool: True if file is a supported document
        """
        filename_lower = filename.lower()
        return any(filename_lower.endswith(ext) for ext in DocumentHandler.SUPPORTED_EXTENSIONS)

    @staticmethod
    def create_content_block(
        file_bytes: bytes,
        filename: str,
        sanitized_name: str
    ) -> Dict[str, Any]:
        """
        Create document ContentBlock for Strands Agent

        Args:
            file_bytes: Raw file bytes
            filename: Original filename
            sanitized_name: Sanitized filename (Bedrock-safe)

        Returns:
            dict: ContentBlock with document data
        """
        doc_format = DocumentHandler.get_document_format(filename)

        content_block = {
            "document": {
                "format": doc_format,
                "name": sanitized_name,
                "source": {
                    "bytes": file_bytes
                }
            }
        }

        logger.info(f"Created document content block: {filename} -> {sanitized_name} (format: {doc_format})")
        return content_block
