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
            user_id=user_id,
            message_id=message_id,
            message_metadata=message_metadata
        )


async def _store_message_metadata_local(
    session_id: str,
    user_id: str,
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
        user_id: User identifier
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

        # Update cost summary in local storage (for consistency with cloud behavior)
        # This updates the pre-aggregated cost summary using the local file storage backend
        timestamp = message_metadata.attribution.timestamp if message_metadata.attribution else None
        if timestamp:
            await _update_cost_summary_async(
                user_id=user_id,
                timestamp=timestamp,
                message_metadata=message_metadata
            )

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
    Store message metadata (cost record) in DynamoDB and update cost summary

    This stores cost/usage data as a separate record with C# prefix SK pattern.
    Cost records are independent of session records and persist even when sessions
    are deleted (for audit trail and billing accuracy).

    Args:
        session_id: Session identifier
        user_id: User identifier
        message_id: Message number (stored as attribute, not in SK)
        message_metadata: MessageMetadata to store
        table_name: DynamoDB table name from DYNAMODB_SESSIONS_METADATA_TABLE_NAME env var

    Schema:
        PK: USER#{user_id}
        SK: C#{timestamp}#{uuid}

        GSI1: UserTimestampIndex (time-range queries by user)
            GSI1PK: USER#{user_id}
            GSI1SK: {timestamp}

        GSI2: SessionLookupIndex (per-session cost queries)
            GSI_PK: SESSION#{session_id}
            GSI_SK: C#{timestamp}

    Benefits:
        - Clean separation from session records (S# prefix)
        - Time-ordered by default
        - Unique SK via UUID prevents collisions
        - Per-session cost queries via SessionLookupIndex GSI
        - Time-range queries via UserTimestampIndex GSI
        - TTL only affects cost records (sessions don't have ttl)
    """
    try:
        import boto3
        import uuid as uuid_lib
        from datetime import datetime, timezone, timedelta

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # Prepare item for DynamoDB
        metadata_dict = message_metadata.model_dump(by_alias=True, exclude_none=True)

        # Convert floats to Decimal for DynamoDB compatibility
        metadata_decimal = _convert_floats_to_decimal(metadata_dict)

        # Extract timestamp for SK and GSI
        timestamp = metadata_dict.get("attribution", {}).get("timestamp", datetime.now(timezone.utc).isoformat())

        # Generate unique ID for SK to prevent collisions
        unique_id = str(uuid_lib.uuid4())

        # Calculate TTL (365 days from now, matching AgentCore Memory retention)
        # Only cost records have TTL - sessions persist until soft-deleted
        ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())

        # Build item with new SK pattern
        item = {
            # Primary key with C# prefix for cost records
            "PK": f"USER#{user_id}",
            "SK": f"C#{timestamp}#{unique_id}",

            # GSI1 keys for UserTimestampIndex - enables time-range queries across all user messages
            "GSI1PK": f"USER#{user_id}",
            "GSI1SK": timestamp,

            # GSI keys for SessionLookupIndex - enables per-session cost queries
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"C#{timestamp}",

            # Session reference (for linking back to session)
            "sessionId": session_id,
            "messageId": message_id,

            # Attribution
            "userId": user_id,
            "timestamp": timestamp,

            # TTL - only cost records have this attribute
            "ttl": ttl,

            # Cost and usage metadata
            **metadata_decimal
        }

        # Store in DynamoDB
        table.put_item(Item=item)

        logger.info(f"💾 Stored cost record in DynamoDB table {table_name}")
        logger.info(f"   Session: {session_id}, Message: {message_id}, SK: C#{timestamp}#{unique_id[:8]}...")

        # Update pre-aggregated cost summary for fast quota checks
        # This is done asynchronously and non-blocking - failures don't affect the main flow
        await _update_cost_summary_async(
            user_id=user_id,
            timestamp=timestamp,
            message_metadata=message_metadata
        )

    except Exception as e:
        logger.error(f"Failed to store message metadata in DynamoDB: {e}", exc_info=True)
        # Don't raise - metadata storage failures shouldn't break the app


async def _update_cost_summary_async(
    user_id: str,
    timestamp: str,
    message_metadata: MessageMetadata
) -> None:
    """
    Update pre-aggregated cost summary (async, non-blocking)

    This atomically increments the user's cost summary in DynamoDB for <10ms quota checks.
    Uses atomic ADD operations for concurrent safety.
    Also updates per-model breakdown and calculates cache savings.

    Additionally triggers system-wide rollup updates (async, fire-and-forget) for:
    - Daily rollups (ROLLUP#DAILY)
    - Monthly rollups (ROLLUP#MONTHLY)
    - Per-model rollups (ROLLUP#MODEL)

    Args:
        user_id: User identifier
        timestamp: ISO timestamp of the message
        message_metadata: MessageMetadata containing cost, usage, and model info
    """
    try:
        import asyncio
        from datetime import datetime

        # Extract cost and usage from metadata
        cost = message_metadata.cost or 0.0
        token_usage = message_metadata.token_usage

        usage_delta = {}
        cache_read_tokens = 0
        if token_usage:
            cache_read_tokens = token_usage.cache_read_input_tokens or 0
            usage_delta = {
                "inputTokens": token_usage.input_tokens or 0,
                "outputTokens": token_usage.output_tokens or 0,
                "cacheReadInputTokens": cache_read_tokens,
                "cacheWriteInputTokens": token_usage.cache_write_input_tokens or 0,
            }

        # Extract model info for per-model breakdown
        model_id = None
        model_name = None
        provider = None
        if message_metadata.model_info:
            model_id = message_metadata.model_info.model_id
            model_name = message_metadata.model_info.model_name
            provider = message_metadata.model_info.provider

        # Calculate cache savings from pricing snapshot
        # Savings = (cache_read_tokens * input_price) - (cache_read_tokens * cache_read_price)
        cache_savings = 0.0
        if cache_read_tokens > 0:
            logger.debug(f"🔍 Cache savings calculation: cache_read_tokens={cache_read_tokens}")
            if message_metadata.model_info:
                pricing = message_metadata.model_info.pricing_snapshot
                logger.debug(f"🔍 Pricing snapshot: {pricing}")
                if pricing:
                    # Get pricing values (handle both dict and Pydantic model)
                    if hasattr(pricing, 'model_dump'):
                        pricing_dict = pricing.model_dump(by_alias=True)
                    else:
                        pricing_dict = pricing

                    logger.debug(f"🔍 Pricing dict: {pricing_dict}")

                    input_price = pricing_dict.get("inputPricePerMtok", 0)
                    cache_read_price = pricing_dict.get("cacheReadPricePerMtok", 0)

                    # Calculate savings: what we would have paid vs what we actually paid
                    standard_cost = (cache_read_tokens / 1_000_000) * input_price
                    actual_cache_cost = (cache_read_tokens / 1_000_000) * cache_read_price
                    cache_savings = standard_cost - actual_cache_cost

                    logger.info(
                        f"💰 Cache savings: ${cache_savings:.6f} "
                        f"({cache_read_tokens:,} tokens @ input=${input_price}/Mtok vs cache_read=${cache_read_price}/Mtok, "
                        f"standard_cost=${standard_cost:.6f}, actual_cache_cost=${actual_cache_cost:.6f})"
                    )
                else:
                    logger.warning(f"⚠️ No pricing snapshot available for cache savings calculation")
            else:
                logger.warning(f"⚠️ No model_info available for cache savings calculation")

        # Determine period key from timestamp (YYYY-MM format) and date (YYYY-MM-DD)
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            period = dt.strftime('%Y-%m')
            date = dt.strftime('%Y-%m-%d')
        except (ValueError, AttributeError):
            # Fallback to current month/day if timestamp parsing fails
            from datetime import timezone
            now = datetime.now(timezone.utc)
            period = now.strftime('%Y-%m')
            date = now.strftime('%Y-%m-%d')

        # Use storage abstraction for the atomic update
        from apis.app_api.storage.metadata_storage import get_metadata_storage
        storage = get_metadata_storage()

        await storage.update_user_cost_summary(
            user_id=user_id,
            period=period,
            cost_delta=cost,
            usage_delta=usage_delta,
            timestamp=timestamp,
            model_id=model_id,
            model_name=model_name,
            cache_savings_delta=cache_savings,
            provider=provider
        )

        model_info_str = f", model={model_id}" if model_id else ""
        savings_str = f", savings=${cache_savings:.6f}" if cache_savings > 0 else ""
        logger.info(f"📊 Updated cost summary: user={user_id}, period={period}, cost=${cost:.6f}{model_info_str}{savings_str}")

        # Fire-and-forget: Update system-wide rollups asynchronously
        # These updates don't block the main request flow
        asyncio.create_task(
            _update_system_rollups_async(
                user_id=user_id,
                period=period,
                date=date,
                cost=cost,
                usage_delta=usage_delta,
                cache_savings=cache_savings,
                model_id=model_id,
                model_name=model_name,
                provider=provider
            )
        )

    except Exception as e:
        # Log but don't raise - cost summary updates shouldn't break the app
        logger.error(f"Failed to update cost summary: {e}", exc_info=True)


async def _update_system_rollups_async(
    user_id: str,
    period: str,
    date: str,
    cost: float,
    usage_delta: dict,
    cache_savings: float,
    model_id: str | None,
    model_name: str | None,
    provider: str | None
) -> None:
    """
    Update system-wide rollups for admin dashboard (async, fire-and-forget)

    This updates:
    - Daily rollup (ROLLUP#DAILY, SK: YYYY-MM-DD)
    - Monthly rollup (ROLLUP#MONTHLY, SK: YYYY-MM)
    - Per-model rollup (ROLLUP#MODEL, SK: YYYY-MM#model_id)

    These updates are non-blocking and failures don't affect the main request flow.
    The rollups support the admin cost dashboard with pre-aggregated system-wide metrics.

    Args:
        user_id: User identifier (for tracking unique active users)
        period: Monthly period (YYYY-MM)
        date: Daily date (YYYY-MM-DD)
        cost: Cost delta to add
        usage_delta: Token usage delta
        cache_savings: Cache savings delta
        model_id: Model identifier
        model_name: Human-readable model name
        provider: LLM provider
    """
    try:
        # Check if we're using DynamoDB storage (rollups only make sense in cloud mode)
        system_rollup_table = os.environ.get("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME")
        if not system_rollup_table:
            logger.debug("System rollup table not configured, skipping rollup updates")
            return

        from apis.app_api.storage.dynamodb_storage import DynamoDBStorage
        storage = DynamoDBStorage()

        # Track active users using conditional writes
        # Returns (is_new_today, is_new_this_month) - True if first request for that period
        is_new_today, is_new_this_month = await storage.track_active_user(
            user_id=user_id,
            period=period,
            date=date
        )

        # Update daily rollup
        await storage.update_daily_rollup(
            date=date,
            cost_delta=cost,
            usage_delta=usage_delta,
            is_new_user=is_new_today,
            model_id=model_id
        )

        # Update monthly rollup
        await storage.update_monthly_rollup(
            period=period,
            cost_delta=cost,
            usage_delta=usage_delta,
            cache_savings_delta=cache_savings,
            is_new_user=is_new_this_month,
            model_id=model_id
        )

        # Update per-model rollup if model info is available
        if model_id and model_name and provider:
            # Track active users per model separately (user may use multiple models)
            is_new_user_for_model = await storage.track_active_user_for_model(
                user_id=user_id,
                period=period,
                model_id=model_id
            )

            await storage.update_model_rollup(
                period=period,
                model_id=model_id,
                model_name=model_name,
                provider=provider,
                cost_delta=cost,
                usage_delta=usage_delta,
                is_new_user_for_model=is_new_user_for_model
            )

        logger.debug(f"📈 Updated system rollups: date={date}, period={period}, new_today={is_new_today}, new_month={is_new_this_month}")

    except Exception as e:
        # Log but don't raise - rollup updates are supplementary
        logger.error(f"Failed to update system rollups: {e}", exc_info=True)


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
    Store session metadata in DynamoDB with new SK pattern

    This creates or updates the session record in DynamoDB.
    For updates where last_message_at changes, the record is moved (delete old, put new)
    because the SK contains the timestamp.

    Args:
        session_id: Session identifier
        user_id: User identifier
        session_metadata: SessionMetadata to store
        table_name: DynamoDB table name from DYNAMODB_SESSIONS_METADATA_TABLE_NAME env var

    Schema:
        PK: USER#{user_id}
        SK: S#ACTIVE#{last_message_at}#{session_id} (active sessions)
            S#DELETED#{deleted_at}#{session_id} (deleted sessions)

        GSI: SessionLookupIndex
            GSI_PK: SESSION#{session_id}
            GSI_SK: META

    This allows:
    - Querying all active sessions: begins_with(SK, 'S#ACTIVE#')
    - Sessions sorted by timestamp in SK (no in-memory sorting needed)
    - Direct session lookup via GSI
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
        from datetime import datetime, timezone

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # First, check if session exists via GSI to get current SK
        existing_session = await _get_session_by_gsi(session_id, user_id, table)

        # Prepare item for DynamoDB
        item = session_metadata.model_dump(by_alias=True, exclude_none=True)

        # Convert floats to Decimal for DynamoDB compatibility
        item = _convert_floats_to_decimal(item)

        # Determine SK based on session status
        last_message_at = session_metadata.last_message_at or datetime.now(timezone.utc).isoformat()

        if session_metadata.deleted:
            deleted_at = session_metadata.deleted_at or datetime.now(timezone.utc).isoformat()
            new_sk = f"S#DELETED#{deleted_at}#{session_id}"
        else:
            new_sk = f"S#ACTIVE#{last_message_at}#{session_id}"

        # Build primary key
        pk = f'USER#{user_id}'

        # Add GSI keys for direct lookup
        item['GSI_PK'] = f'SESSION#{session_id}'
        item['GSI_SK'] = 'META'

        if existing_session:
            # Session exists - check if SK needs to change
            old_sk = existing_session.get('SK')

            if old_sk and old_sk != new_sk:
                # SK changed (timestamp updated) - need transactional move
                # Deep merge existing with new data
                merged_item = _deep_merge(
                    {k: v for k, v in existing_session.items() if k not in ['PK', 'SK']},
                    item
                )
                merged_item['PK'] = pk
                merged_item['SK'] = new_sk

                # Transactional delete + put
                logger.debug(f"🔄 Transacting session move: old_sk={old_sk[:50]}..., new_sk={new_sk[:50]}...")
                try:
                    dynamodb.meta.client.transact_write_items(
                        TransactItems=[
                            {
                                'Delete': {
                                    'TableName': table_name,
                                    'Key': {
                                        'PK': {'S': pk},
                                        'SK': {'S': old_sk}
                                    }
                                }
                            },
                            {
                                'Put': {
                                    'TableName': table_name,
                                    'Item': _convert_to_dynamodb_format(merged_item)
                                }
                            }
                        ]
                    )
                    logger.info(f"💾 Moved session metadata in DynamoDB (SK changed)")
                except Exception as tx_error:
                    logger.error(f"Transaction failed - PK={pk}, old_SK={old_sk}, new_SK={new_sk}")
                    raise
            else:
                # SK unchanged - simple update with deep merge
                # Build update expression for partial update
                update_expression_parts = []
                expression_attribute_names = {}
                expression_attribute_values = {}

                for key_name, value in item.items():
                    # Skip keys that are part of the primary key or GSI
                    if key_name in ['sessionId', 'userId', 'PK', 'SK']:
                        continue

                    placeholder_name = f"#{key_name}"
                    placeholder_value = f":{key_name}"

                    update_expression_parts.append(f"{placeholder_name} = {placeholder_value}")
                    expression_attribute_names[placeholder_name] = key_name
                    expression_attribute_values[placeholder_value] = value

                if update_expression_parts:
                    update_expression = "SET " + ", ".join(update_expression_parts)
                    table.update_item(
                        Key={'PK': pk, 'SK': old_sk},
                        UpdateExpression=update_expression,
                        ExpressionAttributeNames=expression_attribute_names,
                        ExpressionAttributeValues=expression_attribute_values
                    )
                logger.info(f"💾 Updated session metadata in DynamoDB table {table_name}")
        else:
            # New session - create with put_item
            item['PK'] = pk
            item['SK'] = new_sk
            table.put_item(Item=item)
            logger.info(f"💾 Created session metadata in DynamoDB table {table_name}")

        logger.info(f"   Session: {session_id}, User: {user_id}")

    except Exception as e:
        logger.error(f"Failed to store session metadata in DynamoDB: {e}", exc_info=True)
        # Don't raise - metadata storage failures shouldn't break the app


async def _get_session_by_gsi(session_id: str, user_id: str, table) -> Optional[dict]:
    """
    Get session record using GSI (SessionLookupIndex)

    This allows looking up a session by ID without knowing its SK (which contains timestamp).

    Args:
        session_id: Session identifier
        user_id: User identifier (for ownership verification)
        table: DynamoDB table resource

    Returns:
        Raw DynamoDB item dict if found, None otherwise
    """
    try:
        from boto3.dynamodb.conditions import Key

        response = table.query(
            IndexName='SessionLookupIndex',
            KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').eq('META')
        )

        items = response.get('Items', [])
        if not items:
            return None

        item = items[0]

        # Verify user ownership
        if item.get('userId') != user_id:
            logger.warning(f"Session {session_id} belongs to different user")
            return None

        return _convert_decimal_to_float(item)

    except Exception as e:
        # GSI might not exist yet - fall back to None
        logger.debug(f"GSI lookup failed (may not exist yet): {e}")
        return None


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
        elif isinstance(value, str):
            result[key] = {'S': value}
        elif isinstance(value, bool):
            result[key] = {'BOOL': value}
        elif isinstance(value, (int, float, Decimal)):
            result[key] = {'N': str(value)}
        elif isinstance(value, list):
            # Always use List type (L), not String Set (SS)
            # SS has restrictions: no empty strings, no duplicates, cannot be empty
            # Using L is safer and preserves order
            result[key] = {'L': [_convert_single_value_to_dynamodb(v) for v in value]}
        elif isinstance(value, dict):
            result[key] = {'M': _convert_to_dynamodb_format(value)}
    return result


def _convert_single_value_to_dynamodb(value) -> dict:
    """Convert a single value to DynamoDB format"""
    if value is None:
        return {'NULL': True}
    elif isinstance(value, str):
        return {'S': value}
    elif isinstance(value, bool):
        return {'BOOL': value}
    elif isinstance(value, (int, float, Decimal)):
        return {'N': str(value)}
    elif isinstance(value, list):
        return {'L': [_convert_single_value_to_dynamodb(v) for v in value]}
    elif isinstance(value, dict):
        return {'M': _convert_to_dynamodb_format(value)}
    else:
        return {'S': str(value)}


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
    """
    Retrieve all message metadata (cost records) for a session from DynamoDB

    Uses the SessionLookupIndex GSI to query cost records by session ID.
    Cost records have SK pattern: C#{timestamp}#{uuid}
    GSI pattern: GSI_PK=SESSION#{session_id}, GSI_SK=C#{timestamp}

    Args:
        session_id: Session identifier
        user_id: User identifier
        table_name: DynamoDB table name

    Returns:
        Dictionary mapping message_id (str) to metadata dict
    """
    try:
        import boto3
        from boto3.dynamodb.conditions import Key

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        logger.info(f"🔍 Querying cost records via GSI for session {session_id}")

        # Query cost records for this session using GSI
        # GSI_PK: SESSION#{session_id}
        # GSI_SK: begins_with C# for cost records
        response = table.query(
            IndexName='SessionLookupIndex',
            KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').begins_with('C#')
        )

        items = response.get("Items", [])
        metadata_index = {}

        logger.info(f"📦 DynamoDB returned {len(items)} cost record items")

        for item in items:
            # Verify user ownership
            if item.get('userId') != user_id:
                logger.warning(f"Cost record belongs to different user, skipping")
                continue

            # Convert Decimal to float
            item_float = _convert_decimal_to_float(item)

            # Extract message_id as integer (DynamoDB returns Decimal, convert to int then str)
            # Must convert to int first to avoid "0.0" -> "0" mismatch
            message_id_raw = item_float.get("messageId")
            message_id = str(int(message_id_raw)) if isinstance(message_id_raw, (int, float)) else str(message_id_raw)

            logger.debug(f"Processing cost record for message_id={message_id}, SK={item_float.get('SK')}")

            # Remove DynamoDB-specific keys and top-level fields not needed in metadata dict
            for key in ["PK", "SK", "GSI_PK", "GSI_SK", "ttl", "userId", "sessionId", "messageId", "timestamp"]:
                item_float.pop(key, None)

            metadata_index[message_id] = item_float

        logger.info(f"📂 Retrieved {len(metadata_index)} cost records from DynamoDB")
        logger.info(f"📋 Metadata keys: {sorted(metadata_index.keys())}")
        return metadata_index

    except Exception as e:
        logger.error(f"Failed to query message metadata from DynamoDB: {e}", exc_info=True)
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
    Retrieve session metadata from DynamoDB using GSI

    With the new SK pattern (S#ACTIVE#{last_message_at}#{session_id}), we can't
    use get_item directly because we don't know the last_message_at timestamp.
    Instead, we use the SessionLookupIndex GSI for direct session lookup by ID.

    Args:
        session_id: Session identifier
        user_id: User identifier
        table_name: DynamoDB table name

    Returns:
        SessionMetadata object if found, None otherwise

    Schema:
        GSI: SessionLookupIndex
            GSI_PK: SESSION#{session_id}
            GSI_SK: META

    This allows looking up sessions by ID without knowing the timestamp.
    """
    try:
        import boto3
        from boto3.dynamodb.conditions import Key

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        # Use GSI for session lookup by ID
        response = table.query(
            IndexName='SessionLookupIndex',
            KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').eq('META')
        )

        items = response.get('Items', [])
        if not items:
            logger.info(f"Session metadata not found in DynamoDB: {session_id}")
            return None

        item = items[0]

        # Verify user ownership
        if item.get('userId') != user_id:
            logger.warning(f"Session {session_id} belongs to different user")
            return None

        # Convert Decimal to float for JSON serialization
        item = _convert_decimal_to_float(item)

        # Remove DynamoDB keys before validation
        for key in ['PK', 'SK', 'GSI_PK', 'GSI_SK']:
            item.pop(key, None)

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
    List active sessions for a user from DynamoDB with efficient pagination

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
        SK: S#ACTIVE#{last_message_at}#{session_id}

    Performance improvements over old schema:
        - Query only returns session records (no cost records with C# prefix)
        - No in-memory filtering needed
        - Sessions sorted by timestamp in SK (no in-memory sorting)
        - True server-side pagination via DynamoDB's native mechanism
        - O(page_size) instead of O(sessions + messages)
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

        # Build query parameters with new S#ACTIVE# prefix
        # This cleanly separates from:
        # - S#DELETED# (soft-deleted sessions)
        # - C# (cost records)
        query_params = {
            'KeyConditionExpression': Key('PK').eq(f'USER#{user_id}') & Key('SK').begins_with('S#ACTIVE#'),
            'ScanIndexForward': False  # Descending order (most recent first) - timestamp is in SK!
        }

        if exclusive_start_key:
            query_params['ExclusiveStartKey'] = exclusive_start_key

        if limit:
            # With new schema, we can use exact limit - no over-fetching needed
            query_params['Limit'] = limit

        # Execute query
        response = table.query(**query_params)

        # Parse items - no filtering needed, all items are active sessions
        sessions = []
        for item in response['Items']:
            try:
                # Convert Decimal to float
                item = _convert_decimal_to_float(item)

                # Remove DynamoDB keys
                for key in ['PK', 'SK', 'GSI_PK', 'GSI_SK']:
                    item.pop(key, None)

                metadata = SessionMetadata.model_validate(item)
                sessions.append(metadata)
            except Exception as e:
                logger.warning(f"Failed to parse session item: {e}")
                continue

        # No in-memory sorting needed! With new SK pattern S#ACTIVE#{last_message_at}#{session_id},
        # DynamoDB returns items already sorted by timestamp (via ScanIndexForward=False)

        # No limit trimming needed either - we use exact Limit in query params

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

