"""
Prompt builder for multimodal content (text, images, documents)
"""
import logging
import base64
from typing import List, Optional, Union, Dict, Any, Set
from agents.main_agent.multimodal.image_handler import ImageHandler
from agents.main_agent.multimodal.document_handler import DocumentHandler
from agents.main_agent.multimodal.file_sanitizer import FileSanitizer

logger = logging.getLogger(__name__)


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
        files: Optional[List[Any]] = None,
        attachment_names: Optional[List[str]] = None,
    ) -> Union[str, List[Dict[str, Any]]]:
        """
        Build prompt for Strands Agent with multimodal support

        Args:
            message: User message text
            files: Optional list of FileContent objects with base64 bytes.
                These become content blocks, so this must be the *inline*
                set only — handing it a .pptx would produce a document block
                in a format Bedrock's enum doesn't accept.
            attachment_names: Optional authoritative filename list for the
                ``[Attached files: …]`` marker. Defaults to the names in
                ``files``. Pass this when some attachments are deliberately
                kept out of ``files`` (spreadsheets route to the analysis
                tools, decks to the PowerPoint tools) — the marker is what
                the SPA replays to rebuild attachment cards on reload, so a
                name missing here means that file's card silently disappears
                from history even though the file itself is still in the
                session.

        Returns:
            str or list[ContentBlock]: Simple string or multimodal content blocks
        """
        marker_names = (
            list(attachment_names)
            if attachment_names is not None
            else [f.filename for f in (files or []) if hasattr(f, 'filename')]
        )

        # The marker must stay at the very END of the text: the SPA's
        # ATTACHED_FILES_PATTERN is `$`-anchored.
        text = message
        if marker_names:
            text = f"{message}\n\n[Attached files: {', '.join(marker_names)}]"

        # Nothing to inline — return plain text. This still carries the
        # marker, which is the case where every attachment was diverted
        # (e.g. a lone .pptx): previously this returned early with a bare
        # message and the attachment vanished from restored history.
        if not files or len(files) == 0:
            return text

        # Build ContentBlock list for multimodal input
        content_blocks: List[Dict[str, Any]] = [{"text": text}]

        # Track sanitized document names used in this turn to prevent
        # Bedrock ValidationException: "Messages can't contain duplicate document names"
        used_document_names: Set[str] = set()

        # Add each file as appropriate ContentBlock
        for file in files:
            content_block = self._process_file(file, used_document_names)
            if content_block:
                content_blocks.append(content_block)

        return content_blocks

    def _process_file(self, file: Any, used_document_names: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
        """
        Process a single file and create appropriate ContentBlock

        Args:
            file: FileContent object with content_type, filename, and base64 bytes
            used_document_names: Set of already-used document names in this turn.
                When provided, duplicate document names are made unique by appending
                a counter suffix to prevent Bedrock ValidationException.

        Returns:
            dict: ContentBlock or None if unsupported
        """
        content_type = file.content_type.lower()
        filename = file.filename.lower()

        # Decode base64 to bytes
        file_bytes = base64.b64decode(file.bytes)

        # Check if image
        if self.image_handler.is_image(content_type, filename):
            return self.image_handler.create_content_block(
                file_bytes=file_bytes,
                content_type=content_type,
                filename=filename
            )

        # Check if document
        elif self.document_handler.is_document(filename):
            # Sanitize filename for Bedrock
            sanitized_name = self.file_sanitizer.sanitize_filename(file.filename)

            # Deduplicate document names within this turn. Bedrock rejects
            # requests where two document blocks share the same name, even
            # across different messages in the conversation history. When the
            # same (or similarly-named) file appears more than once, append
            # a numeric suffix to make the name unique.
            if used_document_names is not None:
                sanitized_name = self._unique_document_name(sanitized_name, used_document_names)
                used_document_names.add(sanitized_name)

            return self.document_handler.create_content_block(
                file_bytes=file_bytes,
                filename=filename,
                sanitized_name=sanitized_name
            )

        else:
            logger.warning(f"Unsupported file type: {filename} ({content_type})")
            return None

    @staticmethod
    def _unique_document_name(name: str, used_names: Set[str]) -> str:
        """Return a name that is not already in used_names.

        If ``name`` is already taken, appends ``_2``, ``_3``, … until a free
        slot is found. This keeps names deterministic and human-readable while
        satisfying Bedrock's uniqueness constraint.
        """
        if name not in used_names:
            return name
        counter = 2
        while True:
            candidate = f"{name}_{counter}"
            if candidate not in used_names:
                return candidate
            counter += 1

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
