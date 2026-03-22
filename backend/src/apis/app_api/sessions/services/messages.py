"""Messages service layer

Retrieves conversation history from AgentCore Memory.
"""

import base64
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.sessions.models import Message, MessageContent, MessageResponse, MessagesListResponse

logger = logging.getLogger(__name__)


# Check if AgentCore Memory is available
try:
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False
    logger.warning("AgentCore Memory not available - install bedrock_agentcore package")


def _ensure_image_base64(image_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure image data has base64 encoding instead of raw bytes

    Handles image content from tool results where bytes may be raw.
    Converts to base64 string for JSON serialization.
    """
    if not image_data:
        return image_data

    # Check for source.bytes pattern (from Code Interpreter tool)
    source = image_data.get("source", {})
    if isinstance(source, dict) and "bytes" in source:
        raw_bytes = source["bytes"]
        if isinstance(raw_bytes, bytes):
            # Convert raw bytes to base64 string
            encoded = base64.b64encode(raw_bytes).decode("utf-8")
            return {"format": image_data.get("format", "png"), "data": encoded}
        elif isinstance(raw_bytes, str):
            # Already a string (possibly base64), use as-is
            return {"format": image_data.get("format", "png"), "data": raw_bytes}

    # Check if already in frontend format (format + data)
    if "data" in image_data and "format" in image_data:
        return image_data

    return image_data


def _ensure_document_base64(document_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure document data has base64 encoding instead of raw bytes

    Handles document content where bytes may be raw (from multimodal input).
    Converts to base64 string for JSON serialization.

    Document structure from PromptBuilder:
    {
        "format": "pdf",
        "name": "filename.pdf",
        "source": {
            "bytes": <raw bytes>
        }
    }

    Converted to frontend format:
    {
        "format": "pdf",
        "name": "filename.pdf",
        "data": "<base64 string>"
    }
    """
    if not document_data:
        return document_data

    # Check for source.bytes pattern (from multimodal document input)
    source = document_data.get("source", {})
    if isinstance(source, dict) and "bytes" in source:
        raw_bytes = source["bytes"]
        if isinstance(raw_bytes, bytes):
            # Convert raw bytes to base64 string
            encoded = base64.b64encode(raw_bytes).decode("utf-8")
            return {"format": document_data.get("format", "txt"), "name": document_data.get("name", "document"), "data": encoded}
        elif isinstance(raw_bytes, str):
            # Already a string (possibly base64), use as-is
            return {"format": document_data.get("format", "txt"), "name": document_data.get("name", "document"), "data": raw_bytes}

    # Check if already in frontend format (format + name + data)
    if "data" in document_data and "format" in document_data:
        return document_data

    return document_data


def _process_tool_result_content(tool_result: Dict[str, Any]) -> Dict[str, Any]:
    """Process tool result content to ensure binary data is base64 encoded

    Tool results can contain nested image or document content that needs conversion.
    """
    if not tool_result:
        return tool_result

    content = tool_result.get("content", [])
    if not isinstance(content, list):
        return tool_result

    processed_content = []
    for item in content:
        if isinstance(item, dict):
            processed_item = dict(item)
            # Process nested image in tool result
            if "image" in item:
                processed_item["image"] = _ensure_image_base64(item["image"])
            # Process nested document in tool result
            if "document" in item:
                processed_item["document"] = _ensure_document_base64(item["document"])
            processed_content.append(processed_item)
        else:
            processed_content.append(item)

    result = dict(tool_result)
    result["content"] = processed_content
    return result


def _convert_content_block(content_item: Any) -> MessageContent:
    """Convert a content block to MessageContent model

    Handles all Bedrock Converse API content types:
    - text: Plain text content
    - toolUse: Tool/function call
    - toolResult: Result from tool execution
    - image: Image content
    - document: Document content
    - reasoningContent: Chain-of-thought reasoning (Claude extended thinking, GPT reasoning, etc.)
    """
    # Handle different content types
    if isinstance(content_item, dict):
        content_type = None
        text = None
        tool_use = None
        tool_result = None
        image = None
        document = None
        reasoning_content = None

        # Determine content type
        if "text" in content_item:
            content_type = "text"
            text = content_item["text"]
        elif "toolUse" in content_item:
            content_type = "toolUse"
            tool_use = content_item["toolUse"]
        elif "toolResult" in content_item:
            content_type = "toolResult"
            # Process tool result to ensure images are base64 encoded
            tool_result = _process_tool_result_content(content_item["toolResult"])
        elif "image" in content_item:
            content_type = "image"
            # Ensure image is base64 encoded
            image = _ensure_image_base64(content_item["image"])
        elif "document" in content_item:
            content_type = "document"
            # Ensure document is base64 encoded (raw bytes from multimodal input)
            document = _ensure_document_base64(content_item["document"])
        elif "reasoningContent" in content_item:
            # Handle reasoning content (extended thinking from Claude 3.7+, GPT, etc.)
            # Preserve the full structure including reasoningText and signature
            content_type = "reasoningContent"
            reasoning_content = content_item["reasoningContent"]
        else:
            # Unknown type - log warning and preserve as-is
            # Don't stringify, just store as text with a note
            logger.warning(f"Unknown content block type with keys: {list(content_item.keys())}")
            content_type = "text"
            text = str(content_item)

        return MessageContent(
            type=content_type,
            text=text,
            tool_use=tool_use,
            tool_result=tool_result,
            image=image,
            document=document,
            reasoning_content=reasoning_content,
        )
    else:
        # Handle non-dict content (shouldn't happen but be defensive)
        return MessageContent(type="text", text=str(content_item))


def _convert_message_to_response(msg: Message, session_id: str, sequence_number: int, message_id: Optional[str] = None) -> MessageResponse:
    """
    Convert a Message model to MessageResponse model for API response

    Args:
        msg: Message model
        session_id: Session identifier
        sequence_number: 0-based sequence number of the message
        message_id: Optional message ID (deprecated, computed from session_id and sequence)

    Returns:
        MessageResponse model with predictable ID format: msg-{sessionId}-{index}
    """
    # Always compute message_id from session_id and sequence_number (0-based)
    # Format: msg-{sessionId}-{index}
    computed_id = f"msg-{session_id}-{sequence_number}"

    # Convert metadata to dict if it's a MessageMetadata object
    metadata_dict = None
    citations = None
    if msg.metadata:
        metadata_dict = msg.metadata.model_dump(exclude_none=True, by_alias=True)

        # Extract citations from metadata (they're stored there but should be top-level in response)
        # Citations are removed from metadata_dict to avoid duplication
        if isinstance(metadata_dict, dict) and "citations" in metadata_dict:
            citations_data = metadata_dict.pop("citations")
            # Convert citation dicts to Citation objects for type validation
            if citations_data:
                from apis.app_api.messages.models import Citation

                try:
                    citations = [Citation(**c) for c in citations_data]
                except Exception as e:
                    logger.warning(f"Failed to parse citations for message {computed_id}: {e}")
                    citations = None

    return MessageResponse(
        id=computed_id, role=msg.role, content=msg.content, created_at=msg.timestamp or "", metadata=metadata_dict, citations=citations
    )


def _get_message_role(msg: Any) -> str:
    """
    Extract the role from a message for logging purposes.

    Args:
        msg: Message data (dict or SessionMessage object)

    Returns:
        Role string ("user" or "assistant")
    """
    if isinstance(msg, dict):
        return msg.get("role", "assistant")

    # Handle SessionMessage object (from AgentCore Memory)
    inner_message = getattr(msg, "message", None)
    if inner_message:
        if isinstance(inner_message, dict):
            return inner_message.get("role", "assistant")
        return getattr(inner_message, "role", "assistant")

    return getattr(msg, "role", "assistant")


def _convert_message(msg: Any, metadata: Any = None) -> Message:
    """
    Convert a session message to Message model

    Args:
        msg: Message data (dict or SessionMessage object)
        metadata: Optional metadata (MessageMetadata dict or object)

    Returns:
        Message with embedded metadata
    """
    # Extract role and content
    if isinstance(msg, dict):
        role = msg.get("role", "assistant")
        content = msg.get("content", [])
        timestamp = msg.get("timestamp")
    else:
        # Handle SessionMessage object (from AgentCore Memory)
        # SessionMessage has a nested 'message' field that contains the actual Message
        inner_message = getattr(msg, "message", None)
        if inner_message:
            # The inner message can be either a dict or an object
            if isinstance(inner_message, dict):
                role = inner_message.get("role", "assistant")
                content = inner_message.get("content", [])
            else:
                role = getattr(inner_message, "role", "assistant")
                content = getattr(inner_message, "content", [])
        else:
            # Fallback: try to get role/content directly (shouldn't happen)
            role = getattr(msg, "role", "assistant")
            content = getattr(msg, "content", [])

        # SessionMessage uses created_at field
        timestamp = getattr(msg, "created_at", None)

    # Convert content blocks
    content_blocks = []
    if isinstance(content, list):
        content_blocks = [_convert_content_block(item) for item in content]
    elif isinstance(content, str):
        # Handle simple string content
        content_blocks = [MessageContent(type="text", text=content)]

    # Convert metadata if present
    from apis.app_api.messages.models import MessageMetadata

    message_metadata = None
    if metadata:
        if isinstance(metadata, dict):
            try:
                message_metadata = MessageMetadata(**metadata)
            except Exception as e:
                logger.error(f"Failed to parse message metadata: {e}")
        elif isinstance(metadata, MessageMetadata):
            message_metadata = metadata

    return Message(role=role, content=content_blocks, timestamp=str(timestamp) if timestamp else None, metadata=message_metadata)


def _apply_pagination(messages: List[Message], limit: Optional[int] = None, next_token: Optional[str] = None) -> Tuple[List[Message], Optional[str]]:
    """
    Apply pagination to a list of messages

    Args:
        messages: List of messages (should be sorted by sequence)
        limit: Maximum number of messages to return
        next_token: Pagination token (sequence number to start from)

    Returns:
        Tuple of (paginated messages, next_token if more messages exist)
    """
    start_index = 0

    # Decode next_token if provided (it's a base64-encoded sequence number)
    if next_token:
        try:
            decoded = base64.b64decode(next_token).decode("utf-8")
            start_index = int(decoded)
        except Exception as e:
            logger.warning(f"Invalid next_token: {e}, starting from beginning")
            start_index = 0

    # Apply start index
    paginated_messages = messages[start_index:]

    # Apply limit
    if limit and limit > 0:
        paginated_messages = paginated_messages[:limit]
        # Check if there are more messages
        if start_index + limit < len(messages):
            next_seq = start_index + limit
            next_token = base64.b64encode(str(next_seq).encode("utf-8")).decode("utf-8")
        else:
            next_token = None
    else:
        next_token = None

    return paginated_messages, next_token


async def get_messages_from_cloud(
    session_id: str, user_id: str, limit: Optional[int] = None, next_token: Optional[str] = None
) -> MessagesListResponse:
    """
    Retrieve messages from AgentCore Memory

    Args:
        session_id: Session identifier
        user_id: User identifier
        limit: Maximum number of messages to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        MessagesListResponse with paginated conversation history
    """
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
    aws_region = os.environ.get("AWS_REGION", "us-west-2")

    if not memory_id:
        raise ValueError("AGENTCORE_MEMORY_ID environment variable not set")

    # Create AgentCore Memory config
    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=user_id,
        enable_prompt_caching=False,  # Not needed for reading
    )

    # Create session manager
    session_manager = AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=aws_region)

    logger.info(f"Retrieving messages from AgentCore Memory - Session: {session_id}, User: {user_id}")

    try:
        # Fetch messages and metadata in parallel for better performance
        import asyncio

        async def fetch_messages():
            """Fetch messages from AgentCore Memory (runs in thread pool since it's sync)"""
            return await asyncio.to_thread(session_manager.list_messages, session_id, "default")

        async def fetch_metadata():
            """Fetch metadata from DynamoDB"""
            from apis.shared.sessions.metadata import get_all_message_metadata

            return await get_all_message_metadata(session_id, user_id)

        # Run both fetches in parallel
        messages_raw, metadata_index = await asyncio.gather(fetch_messages(), fetch_metadata())

        logger.info(f"AgentCore Memory returned {len(messages_raw) if messages_raw else 0} raw messages")
        logger.info(f"Metadata index contains {len(metadata_index)} entries")
        logger.info(f"🔑 Metadata index keys: {sorted(metadata_index.keys())}")

        # Convert to our Message model
        messages = []
        if messages_raw:
            # DO NOT SORT - AgentCore Memory returns messages in chronological order
            # The enumerated index matches the stored sequence number (message_id)
            for idx, msg in enumerate(messages_raw):
                try:
                    # Metadata join: use index as message_id (0-based sequence)
                    metadata = metadata_index.get(str(idx))

                    # Determine message role for logging
                    # Cost metadata only exists for assistant messages (LLM API calls)
                    msg_role = _get_message_role(msg)

                    if metadata:
                        logger.debug(f"🔗 Joined metadata for message {idx} ({msg_role})")
                    elif msg_role == "user":
                        # User messages don't have cost metadata - this is expected
                        logger.debug(f"📝 User message {idx} - no cost metadata (expected)")
                    else:
                        # Assistant message without metadata is unexpected
                        logger.warning(f"⚠️ No metadata found for assistant message {idx}")

                    messages.append(_convert_message(msg, metadata=metadata))
                except Exception as e:
                    logger.error(f"Error converting message {idx}: {e}", exc_info=True)
                    continue

        logger.info(f"Retrieved {len(messages)} messages from AgentCore Memory with metadata")

        # Apply pagination
        paginated_messages, next_page_token = _apply_pagination(messages, limit, next_token)

        # Convert to MessageResponse format
        start_seq = 0
        if next_token:
            try:
                decoded = base64.b64decode(next_token).decode("utf-8")
                start_seq = int(decoded)
            except Exception:
                start_seq = 0

        message_responses = [_convert_message_to_response(msg, session_id, start_seq + idx) for idx, msg in enumerate(paginated_messages)]

        return MessagesListResponse(messages=message_responses, next_token=next_page_token)

    except Exception as e:
        logger.error(f"Error retrieving messages from AgentCore Memory: {e}")
        raise


async def get_messages(session_id: str, user_id: str, limit: Optional[int] = None, next_token: Optional[str] = None) -> MessagesListResponse:
    """
    Retrieve messages for a session and user with pagination support.

    Args:
        session_id: Session identifier
        user_id: User identifier
        limit: Maximum number of messages to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        MessagesListResponse with paginated conversation history

    Raises:
        RuntimeError: If AgentCore Memory package is not available
    """
    if not AGENTCORE_MEMORY_AVAILABLE:
        raise RuntimeError(
            "bedrock_agentcore package is required. "
            "Install with: uv sync --extra agentcore"
        )
    return await get_messages_from_cloud(session_id, user_id, limit, next_token)
