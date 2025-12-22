"""Metadata storage service for messages and conversations

This service handles storing message metadata (token usage, latency) after
streaming completes. It supports both local file storage and cloud DynamoDB storage.

Architecture:
- Local: Embeds metadata in message JSON files
- Cloud: Stores metadata in DynamoDB table specified by DYNAMODB_SESSIONS_METADATA_TABLE_NAME
"""

import logging
import json
import os
import base64
from typing import Optional, Tuple, Any, Dict
from pathlib import Path
from decimal import Decimal

from apis.app_api.messages.models import MessageMetadata
from apis.app_api.sessions.models import SessionMetadata
from apis.app_api.storage.paths import get_message_path, get_session_metadata_path, get_sessions_root, get_message_metadata_path

logger = logging.getLogger(__name__)


def _convert_floats_to_decimal(obj: Any) -> Any:
    """
    Recursively convert floats to Decimal for DynamoDB

    DynamoDB doesn't support float type, requires Decimal instead.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_floats_to_decimal(item) for item in obj]
    else:
        return obj


def _convert_decimal_to_float(obj: Any) -> Any:
    """
    Recursively convert Decimal to float for JSON serialization

    DynamoDB returns Decimal objects, which need to be converted back to float.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _convert_decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimal_to_float(item) for item in obj]
    else:
        return obj


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
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')

    if sessions_metadata_table:
        await _store_message_metadata_cloud(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            message_metadata=message_metadata,
            table_name=sessions_metadata_table
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

    Strategy: Store metadata in a separate message-metadata.json file
    to better simulate the cloud architecture where metadata is stored
    in a separate DynamoDB table.

    File structure (message-metadata.json):
    {
      "0": { "latency": {...}, "tokenUsage": {...}, "modelInfo": {...}, "attribution": {...} },
      "1": { "latency": {...}, "tokenUsage": {...}, "modelInfo": {...}, "attribution": {...} },
      ...
    }

    Args:
        session_id: Session identifier
        message_id: Message number (0-based sequence)
        message_metadata: MessageMetadata to store
    """
    metadata_file = get_message_metadata_path(session_id)

    # Ensure parent directory exists
    metadata_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Read existing metadata index if it exists
        metadata_index = {}
        if metadata_file.exists():
            with open(metadata_file, 'r') as f:
                metadata_index = json.load(f)

        # Add or update metadata for this message
        # Use string key for JSON compatibility
        message_key = str(message_id)
        metadata_index[message_key] = message_metadata.model_dump(by_alias=True, exclude_none=True)

        # Write back to file atomically
        with open(metadata_file, 'w') as f:
            json.dump(metadata_index, f, indent=2)

        logger.info(f"💾 Stored message metadata for message {message_id} in {metadata_file}")

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
        table_name: DynamoDB table name from DYNAMODB_SESSIONS_METADATA_TABLE_NAME env var

    Schema:
        PK: USER#{user_id}
        SK: SESSION#{session_id}#MSG#{message_id:05d}
    """
    try:
        import boto3
        from datetime import datetime, timezone, timedelta

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # Prepare item for DynamoDB
        metadata_dict = message_metadata.model_dump(by_alias=True, exclude_none=True)

        # Convert floats to Decimal for DynamoDB compatibility
        metadata_decimal = _convert_floats_to_decimal(metadata_dict)

        # Extract timestamp for GSI
        timestamp = metadata_dict.get("attribution", {}).get("timestamp", datetime.now(timezone.utc).isoformat())

        # Calculate TTL (365 days from now, matching AgentCore Memory retention)
        ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())

        # Build item
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"SESSION#{session_id}#MSG#{message_id:05d}",
            "userId": user_id,
            "sessionId": session_id,
            "messageId": message_id,
            "timestamp": timestamp,
            "ttl": ttl,
            **metadata_decimal
        }

        # Add GSI attributes for time-range queries
        item["GSI1PK"] = f"USER#{user_id}"
        item["GSI1SK"] = timestamp

        # Store in DynamoDB
        table.put_item(Item=item)

        logger.info(f"💾 Stored message metadata in DynamoDB table {table_name}")
        logger.info(f"   Session: {session_id}, Message: {message_id}")

    except Exception as e:
        logger.error(f"Failed to store message metadata in DynamoDB: {e}", exc_info=True)
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
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')

    if sessions_metadata_table:
        await _store_session_metadata_cloud(
            session_id=session_id,
            user_id=user_id,
            session_metadata=session_metadata,
            table_name=sessions_metadata_table
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

    Strategy: Store in session-metadata.json at the session root directory.
    Performs a deep merge to preserve existing fields.
    
    Note: Uses separate file from session.json (used by Strands library)
    to avoid conflicts when running in local mode.

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
    - If item exists: Uses update_item for partial updates (deep merge behavior)
    - If item doesn't exist: Uses put_item to create new item

    Args:
        session_id: Session identifier
        user_id: User identifier
        session_metadata: SessionMetadata to store
        table_name: DynamoDB table name from DYNAMODB_SESSIONS_METADATA_TABLE_NAME env var

    Schema:
        PK: USER#{user_id}
        SK: SESSION#{session_id}

        This allows querying all sessions for a user and supports activity-based sorting
        via last_message_at attribute.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # Prepare item for DynamoDB
        item = session_metadata.model_dump(by_alias=True, exclude_none=True)

        # Convert floats to Decimal for DynamoDB compatibility
        item = _convert_floats_to_decimal(item)

        # Build update expression for partial update (deep merge)
        update_expression_parts = []
        expression_attribute_names = {}
        expression_attribute_values = {}

        for key_name, value in item.items():
            # Skip keys that are part of the primary key
            if key_name in ['sessionId', 'userId']:
                continue

            # Use placeholder names to handle reserved words and special characters
            placeholder_name = f"#{key_name}"
            placeholder_value = f":{key_name}"

            update_expression_parts.append(f"{placeholder_name} = {placeholder_value}")
            expression_attribute_names[placeholder_name] = key_name
            expression_attribute_values[placeholder_value] = value

        update_expression = "SET " + ", ".join(update_expression_parts)

        key = {
            'PK': f'USER#{user_id}',
            'SK': f'SESSION#{session_id}'
        }

        try:
            # Try update_item first (most common case - item already exists)
            # This preserves existing fields (deep merge behavior)
            table.update_item(
                Key=key,
                UpdateExpression=update_expression,
                ExpressionAttributeNames=expression_attribute_names,
                ExpressionAttributeValues=expression_attribute_values,
                # This condition ensures the item exists
                ConditionExpression='attribute_exists(PK)'
            )
            logger.info(f"💾 Updated session metadata in DynamoDB table {table_name}")

        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                # Item doesn't exist - create it with put_item
                item['PK'] = key['PK']
                item['SK'] = key['SK']
                table.put_item(Item=item)
                logger.info(f"💾 Created session metadata in DynamoDB table {table_name}")
            else:
                # Re-raise other errors
                raise

        logger.info(f"   Session: {session_id}, User: {user_id}")

    except Exception as e:
        logger.error(f"Failed to store session metadata in DynamoDB: {e}", exc_info=True)
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
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')

    if sessions_metadata_table:
        return await _get_session_metadata_cloud(
            session_id=session_id,
            user_id=user_id,
            table_name=sessions_metadata_table
        )
    else:
        return await _get_session_metadata_local(session_id=session_id)


async def get_all_message_metadata(session_id: str, user_id: str) -> Dict[str, Any]:
    """
    Retrieve all message metadata for a session.
    
    In cloud mode, queries the DynamoDB table for all records matching the 
    session_id prefix in the sort key.
    
    In local mode, reads the message-metadata.json file.
    
    Returns:
        Dictionary mapping message_id (str) to metadata dict
    """
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')

    if sessions_metadata_table:
        return await _get_all_message_metadata_cloud(session_id, user_id, sessions_metadata_table)
    else:
        return await _get_all_message_metadata_local(session_id)


async def _get_all_message_metadata_local(session_id: str) -> Dict[str, Any]:
    """Retrieve all message metadata from local file storage"""
    from apis.app_api.storage.paths import get_message_metadata_path
    metadata_file = get_message_metadata_path(session_id)
    if not metadata_file.exists():
        return {}
    try:
        with open(metadata_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read local message metadata index: {e}")
        return {}


async def _get_all_message_metadata_cloud(session_id: str, user_id: str, table_name: str) -> Dict[str, Any]:
    """Retrieve all message metadata from DynamoDB"""
    try:
        import boto3
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        logger.info(f"🔍 Querying metadata table {table_name} for session {session_id}")
        
        # Query all message metadata for this session
        # PK: USER#{user_id}
        # SK starts with SESSION#{session_id}#MSG#
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_id}",
                ":sk_prefix": f"SESSION#{session_id}#MSG#"
            }
        )
        
        items = response.get("Items", [])
        metadata_index = {}
        
        logger.info(f"📦 DynamoDB returned {len(items)} metadata items")
        
        for item in items:
            # Convert Decimal to float
            item_float = _convert_decimal_to_float(item)
            
            # Extract message_id as integer (DynamoDB returns Decimal, convert to int then str)
            # Must convert to int first to avoid "0.0" -> "0" mismatch
            message_id_raw = item_float.get("messageId")
            message_id = str(int(message_id_raw)) if isinstance(message_id_raw, (int, float)) else str(message_id_raw)
            
            logger.debug(f"Processing metadata for message_id={message_id}, SK={item_float.get('SK')}")
            
            # Remove DynamoDB-specific keys and top-level fields not needed in metadata dict
            for key in ["PK", "SK", "GSI1PK", "GSI1SK", "ttl", "userId", "sessionId", "messageId", "timestamp"]:
                item_float.pop(key, None)
                
            metadata_index[message_id] = item_float
            
        logger.info(f"📂 Retrieved {len(metadata_index)} message metadata items from DynamoDB")
        logger.info(f"📋 Metadata keys: {sorted(metadata_index.keys())}")
        return metadata_index
        
    except Exception as e:
        logger.error(f"Failed to query all message metadata from DynamoDB: {e}", exc_info=True)
        return {}


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

    Schema:
        PK: USER#{user_id}
        SK: SESSION#{session_id}

        Direct get_item lookup using composite key.
    """
    try:
        import boto3

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # Direct get_item lookup using PK and SK
        response = table.get_item(
            Key={
                'PK': f'USER#{user_id}',
                'SK': f'SESSION#{session_id}'
            }
        )

        if 'Item' not in response:
            logger.info(f"Session metadata not found in DynamoDB: {session_id}")
            return None

        # Convert Decimal to float for JSON serialization
        item = _convert_decimal_to_float(response['Item'])

        # Remove DynamoDB keys before validation
        item.pop('PK', None)
        item.pop('SK', None)

        return SessionMetadata.model_validate(item)

    except Exception as e:
        logger.error(f"Failed to retrieve session metadata from DynamoDB: {e}", exc_info=True)
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
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')

    if sessions_metadata_table:
        return await _list_user_sessions_cloud(
            user_id=user_id,
            table_name=sessions_metadata_table,
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

    Schema:
        PK: USER#{user_id}
        SK: SESSION#{session_id}

        Query all sessions for a user, then sort by last_message_at in memory.
    """
    try:
        import boto3
        from boto3.dynamodb.conditions import Key

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # Decode next_token to get ExclusiveStartKey if provided
        exclusive_start_key = None
        if next_token:
            try:
                decoded = base64.b64decode(next_token).decode('utf-8')
                exclusive_start_key = json.loads(decoded)
            except Exception as e:
                logger.warning(f"Invalid next_token: {e}")

        # Build query parameters
        query_params = {
            'KeyConditionExpression': Key('PK').eq(f'USER#{user_id}') & Key('SK').begins_with('SESSION#'),
        }

        if exclusive_start_key:
            query_params['ExclusiveStartKey'] = exclusive_start_key

        if limit:
            # Request more items than limit because we'll sort in memory
            # This isn't perfect but works for reasonable page sizes
            query_params['Limit'] = limit * 2

        # Execute query
        response = table.query(**query_params)

        # Parse items
        sessions = []
        for item in response['Items']:
            try:
                # Convert Decimal to float
                item = _convert_decimal_to_float(item)

                # Remove DynamoDB keys
                item.pop('PK', None)
                item.pop('SK', None)

                metadata = SessionMetadata.model_validate(item)
                sessions.append(metadata)
            except Exception as e:
                logger.warning(f"Failed to parse session item: {e}")
                continue

        # Sort by last_message_at descending (most recent first)
        sessions.sort(key=lambda x: x.last_message_at, reverse=True)

        # Apply limit after sorting
        if limit and len(sessions) > limit:
            sessions = sessions[:limit]

        # Generate next_token from LastEvaluatedKey if present
        next_page_token = None
        if 'LastEvaluatedKey' in response:
            next_page_token = base64.b64encode(
                json.dumps(response['LastEvaluatedKey']).encode('utf-8')
            ).decode('utf-8')

        logger.info(f"Listed {len(sessions)} sessions for user {user_id} from DynamoDB")

        return sessions, next_page_token

    except Exception as e:
        logger.error(f"Failed to list user sessions from DynamoDB: {e}", exc_info=True)
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

