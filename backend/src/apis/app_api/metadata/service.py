"""Metadata storage service for messages and conversations

This service handles storing message metadata (token usage, latency) after
streaming completes. It supports both local file storage and cloud DynamoDB storage.

Architecture:
- Local: Embeds metadata in message JSON files
- Cloud: Stores metadata in DynamoDB table specified by CONVERSATIONS_TABLE_NAME
"""

import logging
import json
import os
from typing import Optional
from pathlib import Path

from ..messages.models import MessageMetadata
from ..storage.paths import get_message_path

logger = logging.getLogger(__name__)


async def store_message_metadata(
    session_id: str,
    user_id: str,
    message_id: int,
    message_metadata: MessageMetadata
) -> None:
    """
    Store message metadata after streaming completes

    This function embeds metadata into existing message files (local)
    or updates DynamoDB records (cloud).

    Args:
        session_id: Session identifier
        user_id: User identifier
        message_id: Message number (1, 2, 3, ...)
        message_metadata: MessageMetadata object to store

    Note:
        This should be called AFTER the session manager flushes messages,
        ensuring the message file exists before we try to update it.
    """
    conversations_table = os.environ.get('CONVERSATIONS_TABLE_NAME')

    if conversations_table:
        await _store_message_metadata_cloud(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            message_metadata=message_metadata,
            table_name=conversations_table
        )
    else:
        await _store_message_metadata_local(
            session_id=session_id,
            message_id=message_id,
            message_metadata=message_metadata
        )


async def _store_message_metadata_local(
    session_id: str,
    message_id: int,
    message_metadata: MessageMetadata
) -> None:
    """
    Store message metadata in local file storage

    Strategy: Embed metadata in the existing message JSON file
    This avoids the need for separate metadata files.

    File structure before:
    {
      "message": { "role": "assistant", "content": [...] },
      "created_at": "2025-01-15T10:30:00Z"
    }

    File structure after:
    {
      "message": { "role": "assistant", "content": [...] },
      "created_at": "2025-01-15T10:30:00Z",
      "metadata": { "latency": {...}, "tokenUsage": {...} }
    }

    Args:
        session_id: Session identifier
        message_id: Message number
        message_metadata: MessageMetadata to store
    """
    message_file = get_message_path(session_id, message_id)

    # Check if message file exists
    if not message_file.exists():
        logger.warning(f"Message file does not exist yet: {message_file}")
        logger.warning(f"Metadata will be lost. This may indicate flush timing issue.")
        return

    try:
        # Read existing message file
        with open(message_file, 'r') as f:
            message_data = json.load(f)

        # Add metadata to the file
        message_data["metadata"] = message_metadata.model_dump(by_alias=True, exclude_none=True)

        # Write back to file
        with open(message_file, 'w') as f:
            json.dump(message_data, f, indent=2)

        logger.info(f"💾 Stored message metadata in {message_file}")

    except Exception as e:
        logger.error(f"Failed to store message metadata in local file: {e}")
        # Don't raise - metadata storage failures shouldn't break the app


async def _store_message_metadata_cloud(
    session_id: str,
    user_id: str,
    message_id: int,
    message_metadata: MessageMetadata,
    table_name: str
) -> None:
    """
    Store message metadata in DynamoDB

    This updates the message record in DynamoDB with metadata.

    Args:
        session_id: Session identifier
        user_id: User identifier
        message_id: Message number
        message_metadata: MessageMetadata to store
        table_name: DynamoDB table name from CONVERSATIONS_TABLE_NAME env var

    Note:
        Implementation depends on your DynamoDB schema.
        This is a placeholder showing the general approach.

    TODO: Implement based on your DynamoDB schema
    Example schema:
        PK: CONVERSATION#{session_id}
        SK: MESSAGE#{message_id}
        metadata: { latency: {...}, tokenUsage: {...} }
    """
    try:
        # TODO: Implement DynamoDB update
        # Example pseudocode:
        # dynamodb = boto3.resource('dynamodb')
        # table = dynamodb.Table(table_name)
        # table.update_item(
        #     Key={
        #         'PK': f'CONVERSATION#{session_id}',
        #         'SK': f'MESSAGE#{message_id}'
        #     },
        #     UpdateExpression='SET metadata = :metadata',
        #     ExpressionAttributeValues={
        #         ':metadata': message_metadata.model_dump(by_alias=True, exclude_none=True)
        #     }
        # )

        logger.info(f"💾 Would store message metadata in DynamoDB table {table_name}")
        logger.info(f"   Session: {session_id}, Message: {message_id}")

    except Exception as e:
        logger.error(f"Failed to store message metadata in DynamoDB: {e}")
        # Don't raise - metadata storage failures shouldn't break the app


# Future: Conversation metadata storage
# async def update_conversation_metadata(
#     session_id: str,
#     user_id: str,
#     last_message_at: Optional[str] = None,
#     increment_message_count: bool = True
# ) -> None:
#     """
#     Update conversation-level metadata
#
#     This will be implemented in the future to track:
#     - lastMessageAt: Timestamp of last message
#     - messageCount: Total number of messages
#     - Other conversation-level stats
#
#     For now, this is a placeholder for future implementation.
#     """
#     pass
