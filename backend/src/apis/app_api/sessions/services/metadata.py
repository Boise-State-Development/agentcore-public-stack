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
import base64
from typing import Optional, Tuple
from pathlib import Path

from apis.app_api.messages.models import MessageMetadata
from apis.app_api.sessions.models import SessionMetadata
from apis.app_api.storage.paths import get_message_path, get_session_metadata_path, get_sessions_root

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


async def store_session_metadata(
    session_id: str,
    user_id: str,
    session_metadata: SessionMetadata
) -> None:
    """
    Store or update session metadata

    This function creates or updates session metadata in local storage
    or DynamoDB (cloud).

    Args:
        session_id: Session identifier
        user_id: User identifier
        session_metadata: SessionMetadata object to store

    Note:
        This performs a deep merge - existing fields are preserved unless
        explicitly overwritten by new values.
    """
    conversations_table = os.environ.get('CONVERSATIONS_TABLE_NAME')

    if conversations_table:
        await _store_session_metadata_cloud(
            session_id=session_id,
            user_id=user_id,
            session_metadata=session_metadata,
            table_name=conversations_table
        )
    else:
        await _store_session_metadata_local(
            session_id=session_id,
            session_metadata=session_metadata
        )


async def _store_session_metadata_local(
    session_id: str,
    session_metadata: SessionMetadata
) -> None:
    """
    Store session metadata in local file storage

    Strategy: Store in session.json at the session root directory.
    Performs a deep merge to preserve existing fields.

    File structure:
    {
      "sessionId": "session_abc123",
      "userId": "user123",
      "title": "Conversation Title",
      "status": "active",
      "createdAt": "2025-01-15T10:30:00Z",
      "lastMessageAt": "2025-01-15T10:35:00Z",
      "messageCount": 5,
      "starred": false,
      "tags": ["weather"],
      "preferences": {
        "lastModel": "claude-3-sonnet",
        "lastTemperature": 0.7,
        "enabledTools": ["weather", "search"]
      }
    }

    Args:
        session_id: Session identifier
        session_metadata: SessionMetadata to store
    """
    session_file = get_session_metadata_path(session_id)

    # Ensure parent directory exists
    session_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Read existing metadata if it exists
        existing_data = {}
        if session_file.exists():
            with open(session_file, 'r') as f:
                existing_data = json.load(f)

        # Convert new metadata to dict
        new_data = session_metadata.model_dump(by_alias=True, exclude_none=True)

        # Deep merge: new data overwrites existing, but preserves other fields
        merged_data = _deep_merge(existing_data, new_data)

        # Write merged data to file
        with open(session_file, 'w') as f:
            json.dump(merged_data, f, indent=2)

        logger.info(f"💾 Stored session metadata in {session_file}")

    except Exception as e:
        logger.error(f"Failed to store session metadata in local file: {e}")
        # Don't raise - metadata storage failures shouldn't break the app


async def _store_session_metadata_cloud(
    session_id: str,
    user_id: str,
    session_metadata: SessionMetadata,
    table_name: str
) -> None:
    """
    Store session metadata in DynamoDB

    This creates or updates the session record in DynamoDB.

    Args:
        session_id: Session identifier
        user_id: User identifier
        session_metadata: SessionMetadata to store
        table_name: DynamoDB table name from CONVERSATIONS_TABLE_NAME env var

    Note:
        Implementation depends on your DynamoDB schema.
        This is a placeholder showing the general approach.

    TODO: Implement based on your DynamoDB schema

    Recommended schema for chronological listing:
        PK: USER#{user_id}
        SK: SESSION#{created_at_iso}#{session_id}

        Example SK: SESSION#2025-01-15T10:30:00.000Z#anon0000_abc123_xyz789

        Benefits:
        - Sessions naturally sorted by creation time
        - Query with ScanIndexForward=False for newest first
        - No additional GSI needed for creation order

    Alternative: Add GSI for activity-based sorting
        GSI_PK: USER#{user_id}
        GSI_SK: ACTIVITY#{last_message_at}#{session_id}

        Benefits:
        - Shows most recently active sessions
        - Main table still sorted by creation
        - Best of both worlds
    """
    try:
        # TODO: Implement DynamoDB update
        # Example pseudocode:
        # import boto3
        # dynamodb = boto3.resource('dynamodb')
        # table = dynamodb.Table(table_name)
        #
        # # Prepare item for DynamoDB
        # item = session_metadata.model_dump(by_alias=True, exclude_none=True)
        # item['PK'] = f'USER#{user_id}'
        #
        # # Use timestamp-based SK for chronological ordering
        # created_at = session_metadata.created_at  # ISO 8601 format
        # item['SK'] = f'SESSION#{created_at}#{session_id}'
        #
        # # Optional: Add GSI keys for activity-based sorting
        # item['GSI_PK'] = f'USER#{user_id}'
        # item['GSI_SK'] = f'ACTIVITY#{session_metadata.last_message_at}#{session_id}'
        #
        # table.put_item(Item=item)

        logger.info(f"💾 Would store session metadata in DynamoDB table {table_name}")
        logger.info(f"   Session: {session_id}, User: {user_id}")

    except Exception as e:
        logger.error(f"Failed to store session metadata in DynamoDB: {e}")
        # Don't raise - metadata storage failures shouldn't break the app


async def get_session_metadata(session_id: str, user_id: str) -> Optional[SessionMetadata]:
    """
    Retrieve session metadata

    Args:
        session_id: Session identifier
        user_id: User identifier

    Returns:
        SessionMetadata object if found, None otherwise
    """
    conversations_table = os.environ.get('CONVERSATIONS_TABLE_NAME')

    if conversations_table:
        return await _get_session_metadata_cloud(
            session_id=session_id,
            user_id=user_id,
            table_name=conversations_table
        )
    else:
        return await _get_session_metadata_local(session_id=session_id)


async def _get_session_metadata_local(session_id: str) -> Optional[SessionMetadata]:
    """
    Retrieve session metadata from local file storage

    Args:
        session_id: Session identifier

    Returns:
        SessionMetadata object if found, None otherwise
    """
    session_file = get_session_metadata_path(session_id)

    if not session_file.exists():
        return None

    try:
        with open(session_file, 'r') as f:
            data = json.load(f)

        return SessionMetadata.model_validate(data)

    except Exception as e:
        logger.error(f"Failed to read session metadata from local file: {e}")
        return None


async def _get_session_metadata_cloud(
    session_id: str,
    user_id: str,
    table_name: str
) -> Optional[SessionMetadata]:
    """
    Retrieve session metadata from DynamoDB

    Args:
        session_id: Session identifier
        user_id: User identifier
        table_name: DynamoDB table name

    Returns:
        SessionMetadata object if found, None otherwise

    TODO: Implement based on your DynamoDB schema

    Note: With timestamp-based SK, retrieval requires a query instead of get_item:
        - Query with PK = USER#{user_id}
        - Filter with begins_with(SK, 'SESSION#') AND contains(SK, session_id)
        - Or store a GSI with session_id for direct lookup

    Recommended: Add GSI for session lookup by ID
        GSI_PK: SESSION_ID#{session_id}
        GSI_SK: USER#{user_id}
    """
    try:
        # TODO: Implement DynamoDB retrieval
        # Example pseudocode (Option 1: Query with filter):
        # import boto3
        # from boto3.dynamodb.conditions import Key, Attr
        # dynamodb = boto3.resource('dynamodb')
        # table = dynamodb.Table(table_name)
        #
        # response = table.query(
        #     KeyConditionExpression=Key('PK').eq(f'USER#{user_id}'),
        #     FilterExpression=Attr('sessionId').eq(session_id)
        # )
        #
        # if response['Items']:
        #     return SessionMetadata.model_validate(response['Items'][0])
        #
        # Example pseudocode (Option 2: Use GSI for direct lookup):
        # response = table.query(
        #     IndexName='SessionIdIndex',
        #     KeyConditionExpression=Key('GSI_PK').eq(f'SESSION_ID#{session_id}')
        # )
        #
        # if response['Items']:
        #     return SessionMetadata.model_validate(response['Items'][0])

        logger.info(f"Would retrieve session metadata from DynamoDB table {table_name}")
        return None

    except Exception as e:
        logger.error(f"Failed to retrieve session metadata from DynamoDB: {e}")
        return None


def _apply_pagination(
    sessions: list[SessionMetadata],
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> Tuple[list[SessionMetadata], Optional[str]]:
    """
    Apply pagination to a list of sessions
    
    Args:
        sessions: List of sessions (should be sorted by last_message_at descending)
        limit: Maximum number of sessions to return
        next_token: Pagination token (base64-encoded last_message_at timestamp to start from)
    
    Returns:
        Tuple of (paginated sessions, next_token if more sessions exist)
    """
    start_index = 0
    
    # Decode next_token if provided (it's a base64-encoded last_message_at timestamp)
    if next_token:
        try:
            decoded = base64.b64decode(next_token).decode('utf-8')
            # Find the index of the first session with last_message_at < decoded timestamp
            # This skips all sessions with the same timestamp as the token (to avoid duplicates)
            for idx, session in enumerate(sessions):
                if session.last_message_at < decoded:
                    start_index = idx
                    break
            else:
                # If no session found with timestamp < decoded, we've reached the end
                start_index = len(sessions)
        except Exception as e:
            logger.warning(f"Invalid next_token: {e}, starting from beginning")
            start_index = 0
    
    # Apply start index
    paginated_sessions = sessions[start_index:]
    
    # Apply limit
    if limit and limit > 0:
        paginated_sessions = paginated_sessions[:limit]
        # Check if there are more sessions
        if start_index + limit < len(sessions):
            # Use the last_message_at of the last session in this page as the next token
            last_session = paginated_sessions[-1]
            next_token = base64.b64encode(last_session.last_message_at.encode('utf-8')).decode('utf-8')
        else:
            next_token = None
    else:
        next_token = None
    
    return paginated_sessions, next_token


async def list_user_sessions(
    user_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> Tuple[list[SessionMetadata], Optional[str]]:
    """
    List sessions for a user with pagination support

    Args:
        user_id: User identifier
        limit: Maximum number of sessions to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        Tuple of (list of SessionMetadata objects, next_token if more sessions exist)
        Sessions are sorted by last_message_at descending (most recent first)
    """
    conversations_table = os.environ.get('CONVERSATIONS_TABLE_NAME')

    if conversations_table:
        return await _list_user_sessions_cloud(
            user_id=user_id,
            table_name=conversations_table,
            limit=limit,
            next_token=next_token
        )
    else:
        return await _list_user_sessions_local(
            user_id=user_id,
            limit=limit,
            next_token=next_token
        )


async def _list_user_sessions_local(
    user_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> Tuple[list[SessionMetadata], Optional[str]]:
    """
    List sessions for a user from local file storage with pagination

    Args:
        user_id: User identifier
        limit: Maximum number of sessions to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        Tuple of (list of SessionMetadata objects, next_token if more sessions exist)
        Sessions are sorted by last_message_at descending (most recent first)
    """
    sessions_root = get_sessions_root()

    if not sessions_root.exists():
        logger.info(f"Sessions directory does not exist: {sessions_root}")
        return [], None

    sessions = []

    try:
        # Iterate through all session directories
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir() or not session_dir.name.startswith('session_'):
                continue

            # Extract session_id from directory name (session_<id>)
            session_id = session_dir.name.replace('session_', '', 1)

            # Read session metadata file
            session_file = get_session_metadata_path(session_id)
            if not session_file.exists():
                continue

            try:
                with open(session_file, 'r') as f:
                    data = json.load(f)

                # Filter by user_id
                if data.get('userId') != user_id:
                    continue

                # Parse and add to list
                metadata = SessionMetadata.model_validate(data)
                sessions.append(metadata)

            except Exception as e:
                logger.warning(f"Failed to read session metadata from {session_file}: {e}")
                continue

        # Sort by last_message_at descending (most recent first)
        sessions.sort(key=lambda x: x.last_message_at, reverse=True)

        logger.info(f"Found {len(sessions)} sessions for user {user_id}")

        # Apply pagination
        paginated_sessions, next_page_token = _apply_pagination(sessions, limit, next_token)

        return paginated_sessions, next_page_token

    except Exception as e:
        logger.error(f"Failed to list user sessions from local storage: {e}")
        return [], None


async def _list_user_sessions_cloud(
    user_id: str,
    table_name: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> Tuple[list[SessionMetadata], Optional[str]]:
    """
    List sessions for a user from DynamoDB with pagination

    Args:
        user_id: User identifier
        table_name: DynamoDB table name
        limit: Maximum number of sessions to return (optional)
        next_token: Pagination token for retrieving next page (optional)

    Returns:
        Tuple of (list of SessionMetadata objects, next_token if more sessions exist)
        Sessions are sorted by last_message_at descending (most recent first)

    TODO: Implement based on your DynamoDB schema

    Recommended schema:
        PK: USER#{user_id}
        SK: SESSION#{created_at_iso}#{session_id}

    Query example with pagination:
        - Query with PK = USER#{user_id}
        - FilterExpression: begins_with(SK, 'SESSION#')
        - ScanIndexForward=False for newest first
        - Use ExclusiveStartKey for pagination (decode from next_token)
        - Use Limit parameter for limit
    """
    try:
        # TODO: Implement DynamoDB query with pagination
        # Example pseudocode:
        # import boto3
        # from boto3.dynamodb.conditions import Key
        # dynamodb = boto3.resource('dynamodb')
        # table = dynamodb.Table(table_name)
        #
        # # Decode next_token to get ExclusiveStartKey if provided
        # exclusive_start_key = None
        # if next_token:
        #     try:
        #         decoded = base64.b64decode(next_token).decode('utf-8')
        #         # Parse decoded token to reconstruct DynamoDB key
        #         exclusive_start_key = json.loads(decoded)
        #     except Exception as e:
        #         logger.warning(f"Invalid next_token: {e}")
        #
        # query_params = {
        #     'KeyConditionExpression': Key('PK').eq(f'USER#{user_id}'),
        #     'FilterExpression': begins_with('SK', 'SESSION#'),
        #     'ScanIndexForward': False,  # Newest first
        #     'Limit': limit if limit else None
        # }
        # if exclusive_start_key:
        #     query_params['ExclusiveStartKey'] = exclusive_start_key
        #
        # response = table.query(**query_params)
        #
        # sessions = []
        # for item in response['Items']:
        #     try:
        #         metadata = SessionMetadata.model_validate(item)
        #         sessions.append(metadata)
        #     except Exception as e:
        #         logger.warning(f"Failed to parse session item: {e}")
        #         continue
        #
        # # Generate next_token from LastEvaluatedKey if present
        # next_page_token = None
        # if 'LastEvaluatedKey' in response:
        #     next_page_token = base64.b64encode(json.dumps(response['LastEvaluatedKey']).encode('utf-8')).decode('utf-8')
        #
        # return sessions, next_page_token

        logger.info(f"Would list user sessions from DynamoDB table {table_name}")
        # For now, return empty list with no next token
        return [], None

    except Exception as e:
        logger.error(f"Failed to list user sessions from DynamoDB: {e}")
        return [], None


def _deep_merge(base: dict, updates: dict) -> dict:
    """
    Deep merge two dictionaries

    Args:
        base: Base dictionary (existing data)
        updates: Updates to apply (new data)

    Returns:
        Merged dictionary

    Note:
        Updates take precedence. Nested dictionaries are merged recursively.
    """
    result = base.copy()

    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dictionaries
            result[key] = _deep_merge(result[key], value)
        else:
            # Overwrite with new value
            result[key] = value

    return result

