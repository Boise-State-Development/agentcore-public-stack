"""Messages service layer

Retrieves conversation history from AgentCore Memory or local file storage.
"""

import logging
import os
import json
import base64
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

from apis.app_api.messages.models import Message, MessageContent, MessageResponse, MessagesListResponse

logger = logging.getLogger(__name__)


# Check if AgentCore Memory is available
try:
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False
    logger.info("AgentCore Memory not available - will use local file storage")


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
            tool_result = content_item["toolResult"]
        elif "image" in content_item:
            content_type = "image"
            image = content_item["image"]
        elif "document" in content_item:
            content_type = "document"
            document = content_item["document"]
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
            reasoning_content=reasoning_content
        )
    else:
        # Handle non-dict content (shouldn't happen but be defensive)
        return MessageContent(type="text", text=str(content_item))


def _convert_message_to_response(
    msg: Message,
    session_id: str,
    sequence_number: int,
    message_id: Optional[str] = None
) -> MessageResponse:
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
    if msg.metadata:
        metadata_dict = msg.metadata.model_dump(exclude_none=True, by_alias=True)

    return MessageResponse(
        id=computed_id,
        role=msg.role,
        content=msg.content,
        created_at=msg.timestamp or "",
        metadata=metadata_dict
    )


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

    return Message(
        role=role,
        content=content_blocks,
        timestamp=str(timestamp) if timestamp else None,
        metadata=message_metadata
    )


def _apply_pagination(
    messages: List[Message],
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> Tuple[List[Message], Optional[str]]:
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
            decoded = base64.b64decode(next_token).decode('utf-8')
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
            next_token = base64.b64encode(str(next_seq).encode('utf-8')).decode('utf-8')
        else:
            next_token = None
    else:
        next_token = None
    
    return paginated_messages, next_token


async def get_messages_from_cloud(
    session_id: str,
    user_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
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
    memory_id = os.environ.get('AGENTCORE_MEMORY_ID')
    aws_region = os.environ.get('AWS_REGION', 'us-west-2')

    if not memory_id:
        raise ValueError("AGENTCORE_MEMORY_ID environment variable not set")

    # Create AgentCore Memory config
    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=user_id,
        enable_prompt_caching=False  # Not needed for reading
    )

    # Create session manager
    session_manager = AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=aws_region
    )

    logger.info(f"Retrieving messages from AgentCore Memory - Session: {session_id}, User: {user_id}")

    try:
        # Fetch messages and metadata in parallel for better performance
        import asyncio
        
        async def fetch_messages():
            """Fetch messages from AgentCore Memory (runs in thread pool since it's sync)"""
            return await asyncio.to_thread(
                session_manager.list_messages,
                session_id,
                "default"
            )
        
        async def fetch_metadata():
            """Fetch metadata from DynamoDB"""
            from apis.app_api.sessions.services.metadata import get_all_message_metadata
            return await get_all_message_metadata(session_id, user_id)
        
        # Run both fetches in parallel
        messages_raw, metadata_index = await asyncio.gather(
            fetch_messages(),
            fetch_metadata()
        )

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
                    if metadata:
                        logger.info(f"🔗 Joined metadata for message {idx}")
                    else:
                        logger.warning(f"⚠️ No metadata found for message {idx}")

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
                decoded = base64.b64decode(next_token).decode('utf-8')
                start_seq = int(decoded)
            except Exception:
                start_seq = 0
        
        message_responses = [
            _convert_message_to_response(msg, session_id, start_seq + idx)
            for idx, msg in enumerate(paginated_messages)
        ]

        return MessagesListResponse(
            messages=message_responses,
            next_token=next_page_token
        )

    except Exception as e:
        logger.error(f"Error retrieving messages from AgentCore Memory: {e}")
        raise


async def get_messages_from_local(
    session_id: str,
    user_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> MessagesListResponse:
    """
    Retrieve messages from local file storage

    FileSessionManager uses directory structure:
    sessions/session_{session_id}/agents/agent_default/messages/message_N.json

    Message metadata is stored separately in:
    sessions/session_{session_id}/message-metadata.json

    This simulates the cloud architecture where messages and metadata
    are stored in separate tables/locations.

    Args:
        session_id: Session identifier
        user_id: User identifier (for consistency, not used in file lookup)
        limit: Maximum number of messages to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        MessagesListResponse with paginated conversation history
    """
    # Use centralized path utilities
    from apis.app_api.storage.paths import get_messages_dir, get_message_metadata_path
    messages_dir = get_messages_dir(session_id)
    metadata_file = get_message_metadata_path(session_id)

    logger.info(f"Retrieving messages from local file - Session: {session_id}, Dir: {messages_dir}")

    # Load metadata index once (simulates single query to metadata table)
    metadata_index = {}
    if metadata_file.exists():
        try:
            with open(metadata_file, 'r') as f:
                metadata_index = json.load(f)
            logger.info(f"Loaded metadata for {len(metadata_index)} messages")
        except Exception as e:
            logger.warning(f"Failed to load message metadata index: {e}")

    messages = []

    if messages_dir.exists() and messages_dir.is_dir():
        try:
            # Get all message files sorted by message_id
            message_files = sorted(
                messages_dir.glob("message_*.json"),
                key=lambda p: int(p.stem.split("_")[1])  # Extract number from message_N.json
            )

            logger.info(f"Found {len(message_files)} message files")

            # Read each message file
            for message_file in message_files:
                try:
                    with open(message_file, 'r') as f:
                        data = json.load(f)

                    # Extract the message object
                    msg = data.get("message", {})

                    # Add timestamp if available
                    if "created_at" in data:
                        msg["timestamp"] = data["created_at"]

                    # Get message_id from filename
                    message_id = int(message_file.stem.split("_")[1])

                    # Lookup metadata from the index (simulates join with metadata table)
                    metadata = metadata_index.get(str(message_id))

                    # Convert to our Message model with metadata
                    message_obj = _convert_message(msg, metadata=metadata)
                    messages.append(message_obj)

                except Exception as e:
                    logger.error(f"Error reading message file {message_file}: {e}")
                    continue

            logger.info(f"Retrieved {len(messages)} messages from local file storage")

        except Exception as e:
            logger.error(f"Error reading session directory: {e}")
            raise

    else:
        logger.info(f"Session messages directory does not exist yet: {messages_dir}")

    # Apply pagination
    paginated_messages, next_page_token = _apply_pagination(messages, limit, next_token)

    # Convert to MessageResponse format
    # Calculate starting index/sequence from next_token
    start_index = 0
    if next_token:
        try:
            decoded = base64.b64decode(next_token).decode('utf-8')
            start_index = int(decoded)
        except Exception:
            start_index = 0

    message_responses = []
    for idx, msg_obj in enumerate(paginated_messages):
        seq_num = start_index + idx
        # Message ID is computed from session_id and sequence number (0-based)
        message_responses.append(_convert_message_to_response(msg_obj, session_id, seq_num))

    return MessagesListResponse(
        messages=message_responses,
        next_token=next_page_token
    )


async def get_messages(
    session_id: str,
    user_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> MessagesListResponse:
    """
    Retrieve messages for a session and user with pagination support

    Automatically selects cloud or local storage based on environment configuration.

    Args:
        session_id: Session identifier
        user_id: User identifier
        limit: Maximum number of messages to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        MessagesListResponse with paginated conversation history
    """
    # Check AGENTCORE_MEMORY_TYPE to decide between cloud/local
    memory_type = os.environ.get('AGENTCORE_MEMORY_TYPE', 'file').lower()
    memory_id = os.environ.get('AGENTCORE_MEMORY_ID')

    # Use cloud if memory_type is dynamodb and library is available
    if memory_type == 'dynamodb' and memory_id and AGENTCORE_MEMORY_AVAILABLE:
        logger.info(f"Using AgentCore Memory for session {session_id}")
        return await get_messages_from_cloud(session_id, user_id, limit, next_token)
    else:
        logger.info(f"Using local file storage for session {session_id}")
        return await get_messages_from_local(session_id, user_id, limit, next_token)

