"""Messages service layer

Retrieves conversation history from AgentCore Memory or local file storage.
"""

import logging
import os
import json
from typing import List, Dict, Any
from pathlib import Path

from .models import Message, MessageContent, GetMessagesResponse

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
    """Convert a content block to MessageContent model"""
    # Handle different content types
    if isinstance(content_item, dict):
        content_type = None
        text = None
        tool_use = None
        tool_result = None
        image = None
        document = None

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
        else:
            # Unknown type, default to text
            content_type = "text"
            text = str(content_item)

        return MessageContent(
            type=content_type,
            text=text,
            tool_use=tool_use,
            tool_result=tool_result,
            image=image,
            document=document
        )
    else:
        # Handle non-dict content (shouldn't happen but be defensive)
        return MessageContent(type="text", text=str(content_item))


def _convert_message(msg: Any) -> Message:
    """Convert a session message to Message model"""
    # Extract role and content
    if isinstance(msg, dict):
        role = msg.get("role", "assistant")
        content = msg.get("content", [])
        timestamp = msg.get("timestamp")
    else:
        # Handle SessionMessage object
        role = getattr(msg, "role", "assistant")
        content = getattr(msg, "content", [])
        timestamp = getattr(msg, "timestamp", None)

    # Convert content blocks
    content_blocks = []
    if isinstance(content, list):
        content_blocks = [_convert_content_block(item) for item in content]
    elif isinstance(content, str):
        # Handle simple string content
        content_blocks = [MessageContent(type="text", text=content)]

    return Message(
        role=role,
        content=content_blocks,
        timestamp=str(timestamp) if timestamp else None
    )


async def get_messages_from_cloud(
    session_id: str,
    user_id: str
) -> GetMessagesResponse:
    """
    Retrieve messages from AgentCore Memory

    Args:
        session_id: Session identifier
        user_id: User identifier

    Returns:
        GetMessagesResponse with conversation history
    """
    memory_id = os.environ.get('MEMORY_ID')
    aws_region = os.environ.get('AWS_REGION', 'us-west-2')

    if not memory_id:
        raise ValueError("MEMORY_ID environment variable not set")

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
        # Get messages from session
        # The session manager uses the base manager's list_messages method
        messages_raw = session_manager.list_messages(session_id, agent_id="default")

        # Convert to our Message model
        messages = []
        if messages_raw:
            for msg in messages_raw:
                try:
                    messages.append(_convert_message(msg))
                except Exception as e:
                    logger.error(f"Error converting message: {e}")
                    continue

        logger.info(f"Retrieved {len(messages)} messages from AgentCore Memory")

        return GetMessagesResponse(
            session_id=session_id,
            user_id=user_id,
            messages=messages,
            total_count=len(messages)
        )

    except Exception as e:
        logger.error(f"Error retrieving messages from AgentCore Memory: {e}")
        raise


async def get_messages_from_local(
    session_id: str,
    user_id: str
) -> GetMessagesResponse:
    """
    Retrieve messages from local file storage

    FileSessionManager uses directory structure:
    sessions/session_{session_id}/agents/agent_default/messages/message_N.json

    Args:
        session_id: Session identifier
        user_id: User identifier (for consistency, not used in file lookup)

    Returns:
        GetMessagesResponse with conversation history
    """
    # Determine sessions directory (same as session_factory.py)
    sessions_dir = Path(__file__).parent.parent.parent.parent / "sessions"
    session_dir = sessions_dir / f"session_{session_id}"
    messages_dir = session_dir / "agents" / "agent_default" / "messages"

    logger.info(f"Retrieving messages from local file - Session: {session_id}, Dir: {messages_dir}")

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

                    # Convert to our Message model
                    messages.append(_convert_message(msg))

                except Exception as e:
                    logger.error(f"Error reading message file {message_file}: {e}")
                    continue

            logger.info(f"Retrieved {len(messages)} messages from local file storage")

        except Exception as e:
            logger.error(f"Error reading session directory: {e}")
            raise

    else:
        logger.info(f"Session messages directory does not exist yet: {messages_dir}")

    return GetMessagesResponse(
        session_id=session_id,
        user_id=user_id,
        messages=messages,
        total_count=len(messages)
    )


async def get_messages(
    session_id: str,
    user_id: str
) -> GetMessagesResponse:
    """
    Retrieve messages for a session and user

    Automatically selects cloud or local storage based on environment configuration.

    Args:
        session_id: Session identifier
        user_id: User identifier

    Returns:
        GetMessagesResponse with conversation history
    """
    memory_id = os.environ.get('MEMORY_ID')

    # Use cloud if MEMORY_ID is set and library is available
    if memory_id and AGENTCORE_MEMORY_AVAILABLE:
        logger.info(f"Using AgentCore Memory for session {session_id}")
        return await get_messages_from_cloud(session_id, user_id)
    else:
        logger.info(f"Using local file storage for session {session_id}")
        return await get_messages_from_local(session_id, user_id)
