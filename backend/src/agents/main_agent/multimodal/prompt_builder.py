"""
Prompt builder for multimodal content (text, images, documents)
"""
import logging
import base64
from typing import List, Optional, Sequence, Union, Dict, Any, Set
from agents.main_agent.multimodal.image_handler import ImageHandler
from agents.main_agent.multimodal.document_handler import DocumentHandler
from agents.main_agent.multimodal.file_sanitizer import FileSanitizer

logger = logging.getLogger(__name__)


def document_names_in(messages: Optional[Sequence[Any]]) -> Set[str]:
    """Every document block name already in ``messages``, tool results included.

    Bedrock rejects a request in which two document blocks share a name
    anywhere in the conversation, not only within one message. A cached agent
    keeps the inline blocks of its earlier turns in its live ``agent.messages``
    (only a restore from Memory swaps them for digests). That includes a turn
    that failed: its user message stays in the list with the synthetic error
    reply after it. So an attachment sent again under the same filename, by the
    user or by the pending-attachment recovery after a failed turn, collides
    with the copy already there. One collision leaves the pair in the list, and
    every later turn on that agent fails the same way.

    Read-only, and only names are collected, so history is never rewritten and
    the cached prefix is untouched. One pass over the content blocks: a few
    microseconds for a long conversation.
    """
    names: Set[str] = set()
    if not messages:
        return names
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            document = block.get("document")
            if isinstance(document, dict) and isinstance(document.get("name"), str):
                names.add(document["name"])
                continue
            tool_result = block.get("toolResult")
            inner = tool_result.get("content") if isinstance(tool_result, dict) else None
            if not isinstance(inner, list):
                continue
            for item in inner:
                nested = item.get("document") if isinstance(item, dict) else None
                if isinstance(nested, dict) and isinstance(nested.get("name"), str):
                    names.add(nested["name"])
    return names


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
        conversation: Optional[Sequence[Any]] = None,
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
            conversation: The messages this prompt will be appended to (the
                agent's live ``agent.messages``). A new document block is named
                so it collides with no document already in them; see
                ``document_names_in``. Read only, never modified.

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

        # Track the document names already taken, in this turn AND in the
        # conversation it joins, to prevent Bedrock's ValidationException
        # "Messages can't contain duplicate document names". Seeded from the
        # conversation because the uniqueness rule spans the whole request.
        used_document_names: Set[str] = document_names_in(conversation)

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
            used_document_names: Set of document names already taken, in this
                turn and in the conversation it joins.
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

            # Deduplicate document names against this turn and the
            # conversation. Bedrock rejects requests where two document blocks
            # share the same name, even across different messages in the
            # conversation history. When the same (or similarly-named) file
            # appears more than once, append a numeric suffix to make the name
            # unique. Only the new block is named; history is never renamed.
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
