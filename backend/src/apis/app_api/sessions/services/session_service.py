"""Session CRUD service for managing session lifecycle

This service provides operations for session management including:
- Get session by ID (via GSI lookup)
- Soft-delete session (transactional move from S#ACTIVE# to S#DELETED# prefix)
- Cascade delete associated files when session is deleted

The service preserves cost records (C# prefix) for audit trails and billing accuracy.
"""

import logging
import os
from typing import Optional
from datetime import datetime, timezone
from decimal import Decimal

from apis.app_api.sessions.models import SessionMetadata
from apis.app_api.files.service import get_file_upload_service

logger = logging.getLogger(__name__)


def _convert_decimal_to_float(obj):
    """Recursively convert Decimal to float for JSON serialization"""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _convert_decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimal_to_float(item) for item in obj]
    else:
        return obj


def _convert_float_to_decimal(obj):
    """Recursively convert float to Decimal for DynamoDB storage.

    DynamoDB's high-level API (table.put_item) requires Decimal types
    for numeric values, not Python floats.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _convert_float_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_float_to_decimal(item) for item in obj]
    else:
        return obj


def _convert_to_dynamodb_format(item: dict) -> dict:
    """
    Convert a Python dict to DynamoDB low-level format for transact_write_items

    Args:
        item: Python dict with native types

    Returns:
        Dict with DynamoDB type descriptors (e.g., {'S': 'value'}, {'N': '123'})
    """
    result = {}
    for key, value in item.items():
        if value is None:
            continue
        # IMPORTANT: Check bool BEFORE int, since bool is a subclass of int in Python
        elif isinstance(value, bool):
            result[key] = {'BOOL': value}
        elif isinstance(value, str):
            result[key] = {'S': value}
        elif isinstance(value, Decimal):
            result[key] = {'N': str(value)}
        elif isinstance(value, (int, float)):
            result[key] = {'N': str(value)}
        elif isinstance(value, list):
            if not value:
                result[key] = {'L': []}
            else:
                result[key] = {'L': [_convert_single_value_to_dynamodb(v) for v in value]}
        elif isinstance(value, dict):
            result[key] = {'M': _convert_to_dynamodb_format(value)}
    return result


def _convert_single_value_to_dynamodb(value) -> dict:
    """Convert a single value to DynamoDB format"""
    if value is None:
        return {'NULL': True}
    # IMPORTANT: Check bool BEFORE int, since bool is a subclass of int in Python
    elif isinstance(value, bool):
        return {'BOOL': value}
    elif isinstance(value, str):
        return {'S': value}
    elif isinstance(value, Decimal):
        return {'N': str(value)}
    elif isinstance(value, (int, float)):
        return {'N': str(value)}
    elif isinstance(value, list):
        return {'L': [_convert_single_value_to_dynamodb(v) for v in value]}
    elif isinstance(value, dict):
        return {'M': _convert_to_dynamodb_format(value)}
    else:
        return {'S': str(value)}


class SessionService:
    """Service for session CRUD operations.

    Provides methods for:
    - get_session: Retrieve session by ID via GSI lookup
    - delete_session: Soft-delete session (move from S#ACTIVE# to S#DELETED#)

    DynamoDB Schema:
        PK: USER#{user_id}
        SK: S#ACTIVE#{last_message_at}#{session_id} (active sessions)
            S#DELETED#{deleted_at}#{session_id} (deleted sessions)

        GSI: SessionLookupIndex
            GSI_PK: SESSION#{session_id}
            GSI_SK: META
    """

    def __init__(self):
        self.table_name = os.environ.get(
            'DYNAMODB_SESSIONS_METADATA_TABLE_NAME',
            'SessionsMetadata'
        )
        self._dynamodb = None
        self._table = None

    @property
    def dynamodb(self):
        """Lazy-load DynamoDB resource"""
        if self._dynamodb is None:
            import boto3
            self._dynamodb = boto3.resource('dynamodb')
        return self._dynamodb

    @property
    def table(self):
        """Lazy-load DynamoDB table"""
        if self._table is None:
            self._table = self.dynamodb.Table(self.table_name)
        return self._table

    def _is_cloud_mode(self) -> bool:
        """Check if running in cloud mode (DynamoDB available)"""
        return bool(os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME'))

    async def get_session(self, user_id: str, session_id: str) -> Optional[SessionMetadata]:
        """
        Get session by ID using GSI.

        Uses the SessionLookupIndex GSI to look up sessions by ID without
        knowing the full SK (which contains the timestamp).

        Args:
            user_id: User identifier (for ownership verification)
            session_id: Session identifier

        Returns:
            SessionMetadata if found and owned by user, None otherwise
        """
        if not self._is_cloud_mode():
            # Fall back to local storage via metadata service
            from apis.app_api.sessions.services.metadata import get_session_metadata
            return await get_session_metadata(session_id, user_id)

        try:
            from boto3.dynamodb.conditions import Key

            response = self.table.query(
                IndexName='SessionLookupIndex',
                KeyConditionExpression=(
                    Key('GSI_PK').eq(f'SESSION#{session_id}') &
                    Key('GSI_SK').eq('META')
                )
            )

            items = response.get('Items', [])
            if not items:
                logger.info(f"Session not found: {session_id}")
                return None

            item = _convert_decimal_to_float(items[0])

            # Verify user ownership
            if item.get('userId') != user_id:
                logger.warning(f"Session {session_id} belongs to different user")
                return None

            # Remove DynamoDB keys
            for key in ['PK', 'SK', 'GSI_PK', 'GSI_SK']:
                item.pop(key, None)

            return SessionMetadata.model_validate(item)

        except Exception as e:
            logger.error(f"Failed to get session {session_id}: {e}", exc_info=True)
            return None

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        """
        Soft-delete a session.

        Moves the session from S#ACTIVE# to S#DELETED# prefix using a
        transactional write. Cost records (C# prefix) are preserved for
        audit trails and billing accuracy.

        Args:
            user_id: User identifier
            session_id: Session identifier

        Returns:
            True if deletion was successful, False if session not found

        Raises:
            No exceptions raised - errors are logged and False is returned
        """
        if not self._is_cloud_mode():
            logger.warning("Session deletion not supported in local mode")
            return False

        try:
            # Get current session via GSI to find its SK
            session = await self.get_session(user_id, session_id)
            if not session:
                logger.info(f"Session not found for deletion: {session_id}")
                return False

            if session.deleted:
                logger.info(f"Session {session_id} already deleted")
                return True

            now = datetime.now(timezone.utc)
            deleted_at = now.isoformat()

            # Build old and new SKs
            old_sk = f'S#ACTIVE#{session.last_message_at}#{session_id}'
            new_sk = f'S#DELETED#{deleted_at}#{session_id}'
            pk = f'USER#{user_id}'

            # Build the deleted item with all fields
            deleted_item = {
                'PK': pk,
                'SK': new_sk,
                'GSI_PK': f'SESSION#{session_id}',
                'GSI_SK': 'META',
                'sessionId': session_id,
                'userId': user_id,
                'title': session.title or '',
                'status': 'deleted',
                'createdAt': session.created_at,
                'lastMessageAt': session.last_message_at,
                'messageCount': session.message_count or 0,
                'starred': session.starred or False,
                'tags': session.tags or [],
                'deleted': True,
                'deletedAt': deleted_at
            }

            # Include preferences if present
            # Convert floats to Decimals since DynamoDB high-level API requires Decimal for numbers
            if session.preferences:
                prefs = session.preferences.model_dump(by_alias=True)
                deleted_item['preferences'] = _convert_float_to_decimal(prefs)

            # Use high-level API: put_item + delete_item
            # Put new item first, then delete old - if put fails, nothing is lost
            # This is simpler and more reliable than transact_write_items
            self.table.put_item(Item=deleted_item)
            self.table.delete_item(
                Key={'PK': pk, 'SK': old_sk}
            )

            logger.info(f"Soft-deleted session {session_id} for user {user_id}")

            # Note: AgentCore Memory cleanup is now handled via BackgroundTasks
            # in the route handler for true fire-and-forget behavior

            # Note: File cascade delete is also handled via BackgroundTasks
            # in the route handler

            return True

        except self.dynamodb.meta.client.exceptions.TransactionCanceledException as e:
            # Transaction failed - likely the session was already deleted or modified
            logger.warning(f"Transaction cancelled for session {session_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}", exc_info=True)
            return False

    def delete_agentcore_memory(self, session_id: str, user_id: str) -> None:
        """
        Delete conversation content from AgentCore Memory (sync, for background tasks).

        This removes the actual messages from AgentCore Memory storage
        but does NOT affect cost records (which are stored separately
        with C# SK prefix in SessionsMetadata table).

        Uses boto3 bedrock-agentcore client to:
        1. List all events for the session
        2. Delete each event sequentially

        Note: AgentCore Memory doesn't have a bulk delete_session API.
        The bedrock-agent-runtime delete_session API is for a different service.

        Args:
            session_id: Session identifier
            user_id: User identifier (actorId in AgentCore Memory)

        Note:
            - Designed to run as a FastAPI BackgroundTask (fire-and-forget)
            - Failures are logged but don't affect the session deletion response
            - Sequential deletion is fine since this runs in the background
        """
        try:
            # Check if AgentCore Memory is available
            from agents.strands_agent.session.memory_config import load_memory_config

            config = load_memory_config()
            if not config.is_cloud_mode:
                logger.debug("AgentCore Memory not in cloud mode, skipping content deletion")
                return

            if not config.memory_id:
                logger.debug("No memory_id configured, skipping content deletion")
                return

            import boto3

            client = boto3.client('bedrock-agentcore', region_name=config.region)

            # List all events for this session with pagination (max 100 per request)
            all_event_ids = []
            next_token = None

            try:
                while True:
                    list_params = {
                        'memoryId': config.memory_id,
                        'actorId': user_id,
                        'sessionId': session_id,
                        'maxResults': 100  # API max is 100
                    }
                    if next_token:
                        list_params['nextToken'] = next_token

                    events_response = client.list_events(**list_params)
                    events = events_response.get('events', [])

                    # Extract event IDs from this page
                    for event in events:
                        if event.get('eventId'):
                            all_event_ids.append(event['eventId'])

                    # Check for more pages
                    next_token = events_response.get('nextToken')
                    if not next_token:
                        break

            except client.exceptions.ResourceNotFoundException:
                # Session doesn't exist in AgentCore Memory - nothing to delete
                logger.debug(f"Session {session_id} not found in AgentCore Memory")
                return
            except Exception as e:
                logger.warning(f"Failed to list events for session {session_id}: {e}")
                return

            if not all_event_ids:
                logger.debug(f"No events found for session {session_id} in AgentCore Memory")
                return

            # Delete events sequentially - this runs in background so no need
            # for parallel execution overhead
            deleted_count = 0
            for event_id in all_event_ids:
                try:
                    client.delete_event(
                        memoryId=config.memory_id,
                        actorId=user_id,
                        sessionId=session_id,
                        eventId=event_id
                    )
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete event {event_id}: {e}")

            logger.info(
                f"Deleted {deleted_count}/{len(all_event_ids)} events from AgentCore Memory "
                f"for session {session_id}"
            )

        except ImportError:
            logger.debug("AgentCore Memory SDK not available, skipping content deletion")
        except Exception as e:
            # Log but don't raise - content deletion failures shouldn't block session deletion
            logger.error(f"Failed to delete AgentCore Memory content for session {session_id}: {e}")

    def delete_session_files(self, session_id: str) -> None:
        """
        Delete all files associated with a session (sync, for background tasks).

        This deletes both S3 objects and DynamoDB metadata for all files
        in the session, and decrements user quotas accordingly.

        Args:
            session_id: Session identifier

        Note:
            - Designed to run as a FastAPI BackgroundTask (fire-and-forget)
            - Failures are logged but don't affect the session deletion response
        """
        import asyncio

        try:
            file_service = get_file_upload_service()

            # Run the async method synchronously for background task
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                deleted_count = loop.run_until_complete(
                    file_service.delete_session_files(session_id)
                )
                if deleted_count > 0:
                    logger.info(
                        f"Background task deleted {deleted_count} files for session {session_id}"
                    )
            finally:
                loop.close()

        except Exception as e:
            # Log but don't raise - file deletion failures shouldn't affect session deletion
            logger.error(f"Failed to delete files for session {session_id}: {e}")
