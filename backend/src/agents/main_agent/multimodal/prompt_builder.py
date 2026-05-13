"""
Prompt builder for multimodal content (text, images, documents)
"""
import logging
import base64
from typing import List, Optional, Union, Dict, Any
from agents.main_agent.multimodal.image_handler import ImageHandler
from agents.main_agent.multimodal.document_handler import DocumentHandler
from agents.main_agent.multimodal.file_sanitizer import FileSanitizer
from apis.shared.files.models import is_tabular_file

logger = logging.getLogger(__name__)

# Bedrock ConverseStream rejects document content blocks whose *internal*
# (decompressed) size exceeds 4.5 MB.  XLSX files expand dramatically when
# parsed, so a 1.4 MB raw file easily blows past the limit (issue #206).
# We use a conservative 4 MB guard on *raw* bytes for non-tabular docs.
_BEDROCK_DOC_MAX_RAW_BYTES = 4 * 1024 * 1024  # 4 MB


class PromptBuilder:
    """Builds prompts with multimodal content support"""

    def __init__(self):
        """Initialize prompt builder with handlers"""
        self.image_handler = ImageHandler()
        self.document_handler = DocumentHandler()
        self.file_sanitizer = FileSanitizer()

    def build_prompt(
        self,
        message: str,
        files: Optional[List[Any]] = None
    ) -> Union[str, List[Dict[str, Any]]]:
        """
        Build prompt for Strands Agent with multimodal support

        Args:
            message: User message text
            files: Optional list of FileContent objects with base64 bytes

        Returns:
            str or list[ContentBlock]: Simple string or multimodal content blocks
        """
        # If no files, return simple text
        if not files or len(files) == 0:
            return message

        # Build ContentBlock list for multimodal input
        content_blocks = []

        # Add text first (with file reference marker for session history reconstruction)
        file_names = [f.filename for f in files if hasattr(f, 'filename')]
        if file_names:
            # Add file reference marker after user message for session history
            text_with_marker = f"{message}\n\n[Attached files: {', '.join(file_names)}]"
            content_blocks.append({"text": text_with_marker})
        else:
            content_blocks.append({"text": message})

        # Add each file as appropriate ContentBlock
        for file in files:
            content_block = self._process_file(file)
            if content_block:
                content_blocks.append(content_block)

        return content_blocks

    def _process_file(self, file: Any) -> Optional[Dict[str, Any]]:
        """
        Process a single file and create appropriate ContentBlock

        Args:
            file: FileContent object with content_type, filename, and base64 bytes

        Returns:
            dict: ContentBlock or None if unsupported
        """
        content_type = file.content_type.lower()
        filename = file.filename.lower()

        # Decode base64 to bytes
        file_bytes = base64.b64decode(file.bytes)

        # --- Tabular files (CSV / XLSX / XLS) -----------------------------------
        # Bedrock's document content blocks cannot reliably handle tabular data:
        # * XLSX files expand internally when parsed and easily exceed Bedrock's
        #   4.5 MB inline-document limit even when the raw file is <2 MB (#206).
        # * CSV files sent as document blocks give the model raw text with no
        #   structured analysis capability.
        # Fix: skip the document block entirely and emit a text note that
        # instructs the model to use the Spreadsheet Analysis tool instead.
        if is_tabular_file(filename, content_type):
            return {
                "text": (
                    f"[Spreadsheet attached: {file.filename} — "
                    "use the Spreadsheet Analysis tool "
                    "(list_spreadsheets / analyze_spreadsheet) "
                    "to work with this file]"
                )
            }

        # --- Images --------------------------------------------------------------
        if self.image_handler.is_image(content_type, filename):
            return self.image_handler.create_content_block(
                file_bytes=file_bytes,
                content_type=content_type,
                filename=filename
            )

        # --- Other documents -----------------------------------------------------
        elif self.document_handler.is_document(filename):
            # Guard against Bedrock's 4.5 MB internal content limit.
            # We use a conservative 4 MB threshold on *raw* bytes because
            # Bedrock measures the document's decompressed size, which can
            # be larger than the compressed bytes we hold in memory.
            if len(file_bytes) > _BEDROCK_DOC_MAX_RAW_BYTES:
                size_mb = len(file_bytes) / (1024 * 1024)
                return {
                    "text": (
                        f"[File attached: {file.filename} ({size_mb:.1f} MB) — "
                        "file is too large to include inline "
                        f"(limit: {_BEDROCK_DOC_MAX_RAW_BYTES // (1024 * 1024)} MB). "
                        "Split into smaller sections or convert to plain text "
                        "before attaching.]"
                    )
                }

            # Sanitize filename for Bedrock
            sanitized_name = self.file_sanitizer.sanitize_filename(file.filename)

            return self.document_handler.create_content_block(
                file_bytes=file_bytes,
                filename=filename,
                sanitized_name=sanitized_name
            )

        else:
            logger.warning(f"Unsupported file type: {filename} ({content_type})")
            return None

    def get_content_type_summary(self, prompt: Union[str, List[Dict[str, Any]]]) -> str:
        """
        Get a summary of content types in the prompt

        Args:
            prompt: Prompt (string or content blocks)

        Returns:
            str: Summary description (e.g., "text only", "text + 2 images + 1 document")
        """
        if isinstance(prompt, str):
            return "text only"

        if isinstance(prompt, list):
            text_count = sum(1 for block in prompt if "text" in block)
            image_count = sum(1 for block in prompt if "image" in block)
            document_count = sum(1 for block in prompt if "document" in block)

            parts = []
            if text_count > 0:
                parts.append("text")
            if image_count > 0:
                parts.append(f"{image_count} image{'s' if image_count > 1 else ''}")
            if document_count > 0:
                parts.append(f"{document_count} document{'s' if document_count > 1 else ''}")

            return " + ".join(parts) if parts else "empty"

        return "unknown"
