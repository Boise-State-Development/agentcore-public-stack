"""Metadata storage service for messages and conversations

This service handles storing message metadata (token usage, latency) after
streaming completes. It uses DynamoDB for storage.

Architecture:
- Cloud: Stores metadata in DynamoDB table specified by DYNAMODB_SESSIONS_METADATA_TABLE_NAME
"""

import logging
import json
import math
import os
import base64
from typing import Iterable, List, Optional, Tuple, Any, Dict
from decimal import Decimal

# Relative imports from shared sessions module
from .models import MessageMetadata, PausedTurnSnapshot, PendingInterrupt, SessionMetadata, SessionPreferences

# Import preview session helper
from agents.main_agent.session.preview_session_manager import is_preview_session

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


def _coerce_cost_total(raw: Any) -> float:
    """Normalize a ``MessageMetadata.cost`` value to a finite float total.

    ``MessageMetadata.cost`` is ``Optional[Union[float, Dict[str, float]]]`` —
    the streaming path stores a breakdown dict (``{"total": ..., "inputCost": ...}``)
    while the legacy path stores a bare float. Downstream summary writers
    only want the scalar total; passing the dict through caused
    ``Decimal(str(...))`` to throw ``ConversionSyntax`` at the DynamoDB
    boundary. NaN/inf and non-numeric values collapse to 0.0.
    """
    if isinstance(raw, dict):
        raw = raw.get("total")
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return value



async def store_message_metadata(
    session_id: str,
    user_id: str,
    message_id: int,
    message_metadata: MessageMetadata
) -> None:
    """
    Store message metadata after streaming completes

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
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    await _store_message_metadata_cloud(
        session_id=session_id,
        user_id=user_id,
        message_id=message_id,
        message_metadata=message_metadata,
        table_name=sessions_metadata_table
    )



async def store_user_display_text(
    session_id: str,
    user_id: str,
    message_id: int,
    display_text: str,
) -> None:
    """
    Store the original user message text for clean UI display.

    When the prompt sent to the model differs from what the user typed
    (e.g. RAG augmentation, file attachment content blocks), this stores
    the original so the frontend can show the clean version. The full
    augmented prompt stays in AgentCore Memory for the LLM.

    Uses a D# (display) prefix SK pattern to separate from C# cost records.

    Args:
        session_id: Session identifier
        user_id: User identifier
        message_id: 0-based message index (user message position)
        display_text: Original user message before prompt modification
    """
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    # Skip preview sessions
    if is_preview_session(session_id):
        return

    try:
        import boto3
        from datetime import datetime, timezone, timedelta

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(sessions_metadata_table)

        timestamp = datetime.now(timezone.utc).isoformat()
        ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())

        item = {
            "PK": f"USER#{user_id}",
            "SK": f"D#{session_id}#{message_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"D#{message_id}",
            "sessionId": session_id,
            "messageId": message_id,
            "userId": user_id,
            "displayText": display_text,
            "timestamp": timestamp,
            "ttl": ttl,
        }

        table.put_item(Item=item)
        logger.info(f"💾 Stored displayText for user message {message_id} in session {session_id}")

    except Exception as e:
        # Non-critical: displayText is a UI enhancement, don't break the request
        logger.error(f"Failed to store user displayText: {e}", exc_info=True)



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

        # Bump session-level aggregates (totalCost, lastContextTokens,
        # contextWindow) for the session-cost badge. Best-effort — drift is
        # repaired by lazy backfill on the next metadata read.
        await _bump_session_aggregates(
            session_id=session_id,
            user_id=user_id,
            message_metadata=message_metadata,
            table=table,
        )

        # Update pre-aggregated cost summary for fast quota checks
        # This is done asynchronously and non-blocking - failures don't affect the main flow
        await _update_cost_summary_async(
            user_id=user_id,
            timestamp=timestamp,
            message_metadata=message_metadata
        )

    except Exception as e:
        logger.error(f"Failed to store message metadata in DynamoDB: {e}", exc_info=True)
        # Propagate error - metadata storage is critical for cost tracking and audit trail
        from fastapi import HTTPException
        from apis.shared.errors import ErrorCode, create_error_response
        raise HTTPException(
            status_code=503,
            detail=create_error_response(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message="Failed to store message metadata in database",
                detail=str(e)
            )
        )



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

        # Extract cost and usage from metadata. cost may be a breakdown dict
        # ({"total": ..., "inputCost": ...}) on the streaming path or a bare
        # float on the legacy path; the summary writer needs the scalar total.
        cost = _coerce_cost_total(message_metadata.cost)
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

                    # `or 0` (not `.get(..., 0)`) — managed-model rows can
                    # store an explicit None for cache_read pricing, which
                    # would otherwise propagate into arithmetic below.
                    input_price = pricing_dict.get("inputPricePerMtok") or 0
                    cache_read_price = pricing_dict.get("cacheReadPricePerMtok") or 0

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
        from apis.shared.storage import get_metadata_storage
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
        # JUSTIFICATION: Cost summary updates are fire-and-forget background operations.
        # They are called asynchronously after the main message storage completes.
        # Failures here should not break the user's chat request, but we log for monitoring.
        # The cost data is already stored in the primary cost record (C# prefix), so this
        # is just updating pre-aggregated summaries for faster quota checks.
        logger.error(f"Failed to update cost summary (non-critical): {e}", exc_info=True)



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

        from apis.shared.storage.dynamodb_storage import DynamoDBStorage
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
        # JUSTIFICATION: System rollup updates are supplementary analytics for admin dashboard.
        # They are fire-and-forget background operations that should not block user requests.
        # The primary cost data is already stored in individual cost records (C# prefix).
        # Rollup failures only affect admin dashboard aggregates, not user functionality.
        logger.error(f"Failed to update system rollups (non-critical): {e}", exc_info=True)



async def store_session_metadata(
    session_id: str,
    user_id: str,
    session_metadata: SessionMetadata
) -> None:
    """
    Store or update session metadata

    Args:
        session_id: Session identifier
        user_id: User identifier
        session_metadata: SessionMetadata object to store

    Note:
        This performs a deep merge - existing fields are preserved unless
        explicitly overwritten by new values.
    """
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    await _store_session_metadata_cloud(
        session_id=session_id,
        user_id=user_id,
        session_metadata=session_metadata,
        table_name=sessions_metadata_table
    )



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

                # Move session: put new SK first, then delete old SK
                # Using high-level Table API (put_item + delete_item) instead of
                # transact_write_items to avoid low-level serialization issues
                logger.debug(f"🔄 Moving session: old_sk={old_sk[:50]}..., new_sk={new_sk[:50]}...")
                try:
                    # Convert floats to Decimal for DynamoDB compatibility
                    decimal_item = _convert_floats_to_decimal(merged_item)

                    # Put new item first — if this fails, original is untouched
                    table.put_item(Item=decimal_item)
                    # Delete old item
                    table.delete_item(Key={'PK': pk, 'SK': old_sk})
                    logger.info(f"💾 Moved session metadata in DynamoDB (SK changed)")
                except Exception as move_error:
                    logger.error(f"Session move failed - PK={pk}, old_SK={old_sk}, new_SK={new_sk}")
                    logger.error(f"Move error: {move_error}")
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
        # Propagate error - session metadata storage is critical for session management
        from fastapi import HTTPException
        from apis.shared.errors import ErrorCode, create_error_response
        raise HTTPException(
            status_code=503,
            detail=create_error_response(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message="Failed to store session metadata in database",
                detail=str(e)
            )
        )


async def ensure_session_metadata_exists(session_id: str, user_id: str) -> bool:
    """Idempotently create a session metadata row if it doesn't exist yet.

    Returns ``True`` when a new row was created (caller can use this as the
    "first turn" signal, e.g. to fire title generation).

    Existence is gated on a ``SessionLookupIndex`` GSI lookup rather than a
    conditional ``put_item``: the main-table SK encodes ``lastMessageAt``
    (rotated each turn by ``update_session_activity`` to keep recency
    listing correct), so each call generates a different SK and an
    ``attribute_not_exists(PK)`` ConditionExpression would be evaluated
    against an item that never existed at that exact key — the put would
    always succeed and the same session would gain a new duplicate row
    every turn.

    The GSI is eventually consistent, so a residual race remains for
    genuinely concurrent first-turn requests for the same brand-new
    session_id. That window is bounded to the GSI replication lag (sub-
    100ms typical) and is the same one tracked alongside the schema
    change in issue #175.

    No-op for preview sessions, which intentionally skip persistence.
    """
    if is_preview_session(session_id):
        return False

    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    try:
        import boto3
        from datetime import datetime, timezone

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if existing is not None:
            return False

        now = datetime.now(timezone.utc).isoformat()
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"S#ACTIVE#{now}#{session_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": "META",
            "sessionId": session_id,
            "userId": user_id,
            "title": "New Conversation",
            "status": "active",
            "createdAt": now,
            "lastMessageAt": now,
            "messageCount": 0,
            "starred": False,
            "tags": [],
        }

        table.put_item(Item=item)
        logger.info(f"💾 Pre-created session metadata for {session_id}")
        return True
    except Exception as e:
        # Best-effort: failures must not block the stream. update_session_activity
        # self-heals by retrying this call once if the row is missing post-stream.
        logger.error(f"ensure_session_metadata_exists failed: {e}", exc_info=True)
        return False


async def update_session_title(session_id: str, user_id: str, title: str) -> None:
    """Update only the title attribute on the session row.

    Uses a targeted ``UpdateExpression`` so it can run concurrently with
    ``store_session_metadata`` (which does a full-row merge) without racing
    on other fields like ``messageCount`` or ``lastMessageAt``. Looks up the
    current SK via the GSI because the SK contains a timestamp.

    No-op when the session row doesn't exist (preview sessions, sessions
    deleted mid-turn).
    """
    if is_preview_session(session_id):
        return

    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            logger.info(f"update_session_title: session {session_id} not found, skipping")
            return
        sk = existing.get("SK")
        if not sk:
            logger.warning(f"update_session_title: session {session_id} has no SK")
            return

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET title = :t",
            ExpressionAttributeValues={":t": title},
        )
        logger.info(f"💾 Updated title for session {session_id}")
    except Exception as e:
        logger.error(f"update_session_title failed: {e}", exc_info=True)


async def update_session_activity(
    session_id: str,
    user_id: str,
    *,
    last_model: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    system_prompt_hash: Optional[str] = None,
) -> bool:
    """Per-turn session activity update with targeted writes.

    Increments ``messageCount``, advances ``lastMessageAt`` to now, and
    merges agent-derived preferences. No other attributes are written, so
    concurrent writers (``update_session_title``, ``add_pending_interrupt``)
    cannot be clobbered by this path.

    Phase A is a targeted ``UpdateExpression`` on the current SK. Phase B
    rotates the SK because ``lastMessageAt`` is encoded in it for recency
    listing — fresh-read after Phase A, put at the new SK, delete the old.
    The Phase B carry picks up any concurrent write that landed between
    Phase A and the fresh read; the residual race window is bounded to
    that small interval (full elimination requires the schema change in
    issue #175).

    Self-heals when the row is missing by calling
    ``ensure_session_metadata_exists`` and retrying the lookup once.
    No-op for preview sessions. Returns ``True`` when the update applied.
    """
    if is_preview_session(session_id):
        return False

    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    try:
        import boto3
        from datetime import datetime, timezone

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            # Pre-create may have failed at /invocations entry — try once
            # more so we don't lose the session record entirely.
            await ensure_session_metadata_exists(session_id, user_id)
            existing = await _get_session_by_gsi(session_id, user_id, table)
            if not existing:
                logger.warning(
                    "update_session_activity: session %s missing and could not be created",
                    session_id,
                )
                return False

        old_sk = existing.get("SK")
        if not old_sk:
            logger.warning("update_session_activity: session %s has no SK", session_id)
            return False

        # Merge preferences: existing values take effect for keys the
        # caller didn't pass (e.g. assistantId set by the assistant-attach
        # flow). We replace the whole `preferences` map in one SET so the
        # update works whether the attribute exists yet or not — DynamoDB
        # disallows updating both a parent path and its children in the
        # same expression.
        existing_prefs_raw = existing.get("preferences") or {}
        try:
            existing_prefs = SessionPreferences.model_validate(existing_prefs_raw)
        except Exception:
            existing_prefs = SessionPreferences()
        prefs_dict = existing_prefs.model_dump(by_alias=False, exclude_none=True)
        if last_model is not None:
            prefs_dict["last_model"] = last_model
        if enabled_tools is not None:
            prefs_dict["enabled_tools"] = enabled_tools
        if system_prompt_hash is not None:
            prefs_dict["system_prompt_hash"] = system_prompt_hash
        merged_prefs = SessionPreferences(**prefs_dict).model_dump(by_alias=True, exclude_none=True)

        now = datetime.now(timezone.utc).isoformat()
        pk = f"USER#{user_id}"

        # Phase A: targeted update of owned attributes on the current SK.
        # Disjoint from title, starred, tags, pendingInterrupts.
        table.update_item(
            Key={"PK": pk, "SK": old_sk},
            UpdateExpression="ADD messageCount :one SET lastMessageAt = :t, preferences = :p",
            ExpressionAttributeValues={
                ":one": 1,
                ":t": now,
                ":p": _convert_floats_to_decimal(merged_prefs),
            },
        )

        # Phase B: SK rotation. lastMessageAt is encoded in the SK for
        # recency listing, so a per-turn change forces a row move. Fresh
        # read carries any concurrent write (e.g. title-gen) that landed
        # between Phase A and now.
        new_sk = f"S#ACTIVE#{now}#{session_id}"
        if new_sk != old_sk:
            fresh_resp = table.get_item(Key={"PK": pk, "SK": old_sk})
            fresh = fresh_resp.get("Item")
            if not fresh:
                logger.warning(
                    "update_session_activity: row vanished between Phase A and Phase B for %s",
                    session_id,
                )
                return True
            carried = {k: v for k, v in fresh.items() if k not in ("PK", "SK")}
            new_item = {"PK": pk, "SK": new_sk, **carried}
            table.put_item(Item=new_item)
            table.delete_item(Key={"PK": pk, "SK": old_sk})

        logger.info("Updated session activity for %s (sk_rotated=%s)", session_id, new_sk != old_sk)
        return True
    except Exception as e:
        logger.error("update_session_activity failed for %s: %s", session_id, e, exc_info=True)
        return False


async def set_selected_prompt_id(
    session_id: str,
    user_id: str,
    prompt_id: Optional[str],
) -> bool:
    """Set ``preferences.selected_prompt_id`` on the session row, in place.

    Targeted SET on the existing row — does NOT rotate the SK, does NOT
    bump ``messageCount``. Safe to call alongside ``update_session_activity``
    in the same turn without double-counting.

    Self-heals via ``ensure_session_metadata_exists`` if the row is
    missing (first-turn-of-new-session case). No-op for preview sessions.
    Pass ``None`` to clear the preference.

    Returns ``True`` on success, ``False`` if the row could not be located
    or the update failed. Failures are logged, never raised — a missing
    preference write must not break a conversation turn.

    Concurrency: this is a Read-Modify-Write on the whole ``preferences``
    map. If another writer (``update_session_activity`` finishing a
    parallel turn, the BFF metadata PUT, etc.) lands between the GetItem
    and UpdateItem here, last-write-wins on the full map. The window is
    short and the same race already exists for every preference field
    written through ``update_session_activity``. The proper fix is a
    nested-attribute SET (``SET preferences.selectedPromptId = :p``),
    which DynamoDB supports but rejects when the parent map is missing —
    so we'd need to pre-create ``preferences`` everywhere first. Tracked
    separately from this feature.
    """
    if is_preview_session(session_id):
        return False

    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        logger.warning("DYNAMODB_SESSIONS_METADATA_TABLE_NAME not set — skipping prompt-id persist")
        return False

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            await ensure_session_metadata_exists(session_id, user_id)
            existing = await _get_session_by_gsi(session_id, user_id, table)
            if not existing:
                logger.warning("set_selected_prompt_id: session %s could not be located", session_id)
                return False

        sk = existing.get("SK")
        if not sk:
            return False

        existing_prefs_raw = existing.get("preferences") or {}
        try:
            existing_prefs = SessionPreferences.model_validate(existing_prefs_raw)
        except Exception:
            existing_prefs = SessionPreferences()

        prefs_dict = existing_prefs.model_dump(by_alias=False, exclude_none=True)
        if prompt_id is None:
            prefs_dict.pop("selected_prompt_id", None)
        else:
            prefs_dict["selected_prompt_id"] = prompt_id

        # Idempotent no-op: skip the round-trip when the persisted value
        # already matches. Common for follow-up turns within the same
        # conversation, where the frontend re-sends the same selection.
        if existing_prefs.selected_prompt_id == prompt_id:
            return True

        merged_prefs = SessionPreferences(**prefs_dict).model_dump(
            by_alias=True, exclude_none=True
        )

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET preferences = :p",
            ExpressionAttributeValues={":p": _convert_floats_to_decimal(merged_prefs)},
        )
        return True
    except Exception as e:
        logger.error("set_selected_prompt_id failed for %s: %s", session_id, e, exc_info=True)
        return False


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
        # JUSTIFICATION: GSI lookup is a fallback mechanism for finding sessions.
        # If the GSI doesn't exist yet (during initial deployment) or the query fails,
        # we gracefully return None and let the caller handle it. This is not a critical
        # failure - the session might not exist, or we're in a transitional state.
        logger.debug(f"GSI lookup failed (may not exist yet): {e}")
        return None




async def _bump_session_aggregates(
    session_id: str,
    user_id: str,
    message_metadata: MessageMetadata,
    table,
) -> None:
    """Atomically update the session row's denormalized cost + context fields.

    Powers the session-cost badge above the chat composer. Single
    ``update_item`` call:

      - ``ADD totalCost :c``  — concurrent-safe across overlapping turns.
      - ``SET lastContextTokens :t, contextWindow :w`` — last-write-wins,
        which is the right behavior for "most recent turn."

    The session row's SK encodes ``lastMessageAt`` so we don't know it
    directly; query the ``SessionLookupIndex`` GSI once to find it. Any
    failure is swallowed — drift is repaired on the next metadata read by
    ``_backfill_session_aggregates``.
    """
    try:
        cost_value = _coerce_cost_total(message_metadata.cost)
        token_usage = message_metadata.token_usage
        # `input_tokens` from Bedrock is the *uncached* portion only — the
        # cached prefix lives in `cache_read_input_tokens` and newly-cached
        # tokens in `cache_write_input_tokens`. Sum all three so the badge
        # reflects true context-window occupancy.
        if token_usage:
            input_tokens = (
                (token_usage.input_tokens or 0)
                + (token_usage.cache_read_input_tokens or 0)
                + (token_usage.cache_write_input_tokens or 0)
            )
        else:
            input_tokens = 0
        context_window = getattr(message_metadata, "context_window", None)
        # context_window may be tucked under model_extra (since
        # MessageMetadata uses extra="allow") or absent entirely.
        if context_window is None and isinstance(getattr(message_metadata, "model_extra", None), dict):
            context_window = message_metadata.model_extra.get("contextWindow") \
                or message_metadata.model_extra.get("context_window")

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            logger.debug("bump_session_aggregates: session %s not found, skipping", session_id)
            return

        sk = existing.get("SK")
        if not sk:
            return

        update_parts_set = ["lastContextTokens = :t"]
        values: Dict[str, Any] = {":c": Decimal(str(cost_value)), ":t": int(input_tokens)}

        if context_window:
            update_parts_set.append("contextWindow = :w")
            values[":w"] = int(context_window)

        update_expression = "ADD totalCost :c SET " + ", ".join(update_parts_set)

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=values,
        )
        logger.debug(
            "bumped session aggregates for %s: +$%.6f, lastContextTokens=%d",
            session_id, cost_value, input_tokens,
        )
    except Exception as e:
        # Non-fatal — lazy backfill compensates on next read.
        logger.debug("bump_session_aggregates failed (will be backfilled on read): %s", e)


async def _backfill_session_aggregates(
    session_id: str,
    user_id: str,
    session_item: Dict[str, Any],
    table,
) -> None:
    """One-shot backfill for legacy sessions missing denormalized aggregates.

    Queries the ``SessionLookupIndex`` GSI for all ``C#`` cost records for
    this session, sums their cost, and writes the totals back to the
    session row. Mutates ``session_item`` in place so the caller's current
    request sees the values. After this runs, subsequent reads are O(1).

    Called from ``_get_session_metadata_cloud`` only when ``totalCost`` is
    missing, so it executes at most once per legacy session.
    """
    try:
        from boto3.dynamodb.conditions import Key

        sk = session_item.get("SK")
        if not sk:
            return

        # Sum cost across all C# records for this session.
        total_cost = 0.0
        last_context_tokens: Optional[int] = None
        last_timestamp: Optional[str] = None

        last_evaluated_key = None
        while True:
            query_kwargs = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": (
                    Key("GSI_PK").eq(f"SESSION#{session_id}")
                    & Key("GSI_SK").begins_with("C#")
                ),
            }
            if last_evaluated_key:
                query_kwargs["ExclusiveStartKey"] = last_evaluated_key

            response = table.query(**query_kwargs)
            for rec in response.get("Items", []):
                rec_float = _convert_decimal_to_float(rec)
                if rec_float.get("userId") != user_id:
                    continue
                cost_raw = rec_float.get("cost")
                total_cost += _coerce_cost_total(cost_raw)

                # Pick up the most recent turn's context tokens. `inputTokens`
                # alone is the uncached delta; sum with cache reads/writes to
                # match true context-window occupancy.
                token_usage = rec_float.get("tokenUsage") or {}
                ts = rec_float.get("timestamp")
                if ts and (last_timestamp is None or ts > last_timestamp):
                    last_timestamp = ts
                    last_context_tokens = int(
                        (token_usage.get("inputTokens") or 0)
                        + (token_usage.get("cacheReadInputTokens") or 0)
                        + (token_usage.get("cacheWriteInputTokens") or 0)
                    )

            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        # Write back to the session row (in place + persisted).
        session_item["totalCost"] = total_cost
        if last_context_tokens is not None:
            session_item["lastContextTokens"] = last_context_tokens

        update_parts = ["totalCost = :c"]
        values: Dict[str, Any] = {":c": Decimal(str(total_cost))}
        if last_context_tokens is not None:
            update_parts.append("lastContextTokens = :t")
            values[":t"] = int(last_context_tokens)

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeValues=values,
        )
        logger.info(
            "Backfilled session aggregates for %s: totalCost=$%.6f, lastContextTokens=%s",
            session_id, total_cost, last_context_tokens,
        )
    except Exception as e:
        # Non-fatal — frontend just sees the un-aggregated state, badge
        # stays hidden, and we'll try again on the next read.
        logger.warning("backfill_session_aggregates failed for %s: %s", session_id, e)


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
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    return await _get_session_metadata_cloud(
        session_id=session_id,
        user_id=user_id,
        table_name=sessions_metadata_table
    )


async def session_exists_for_other_user(session_id: str, current_user_id: str) -> bool:
    """Return True if a session metadata row exists for *session_id* but is
    owned by a user other than *current_user_id*.

    Used by routes that mutate session-keyed records (e.g. PUT
    /sessions/{session_id}/metadata) to refuse writes that would create a
    second metadata row for the same session id under a different user
    partition. The storage layer's ``_get_session_by_gsi`` already
    declines to return rows that don't match the caller's user, so
    callers can't tell the difference between "doesn't exist" and
    "exists, not yours" — this helper closes that gap.

    The function looks at the SessionLookupIndex GSI directly and
    inspects the raw record's ``userId`` attribute; it deliberately does
    not reuse ``_get_session_by_gsi`` because that helper filters on
    ownership and returns None in both cases.

    Returns False if the GSI is unavailable, no row exists, or the row
    is owned by the current user.
    """
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    try:
        import boto3
        from boto3.dynamodb.conditions import Key

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(sessions_metadata_table)

        response = table.query(
            IndexName='SessionLookupIndex',
            KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}')
            & Key('GSI_SK').eq('META'),
        )
        items = response.get('Items', [])
        if not items:
            return False
        # The GSI is keyed by session_id+'META', so at most one row should
        # ever land here under healthy data. Defensively check every row.
        for item in items:
            owner = item.get('userId')
            if owner and owner != current_user_id:
                return True
        return False
    except Exception as exc:
        # Failing closed (returning True) would block legitimate writes
        # whenever the GSI is unavailable; failing open is the same risk
        # window the existing GET path already accepts. Log loudly.
        logger.warning(
            "session_exists_for_other_user: GSI lookup failed for %s: %s",
            session_id,
            exc,
        )
        return False


async def get_all_message_metadata(session_id: str, user_id: str) -> Dict[str, Any]:
    """
    Retrieve all message metadata for a session.

    Queries the DynamoDB table for all records matching the session_id prefix
    in the sort key.

    Returns:
        Dictionary mapping message_id (str) to metadata dict
    """
    sessions_metadata_table = os.environ.get('DYNAMODB_SESSIONS_METADATA_TABLE_NAME')
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    return await _get_all_message_metadata_cloud(session_id, user_id, sessions_metadata_table)


async def _get_all_message_metadata_cloud(session_id: str, user_id: str, table_name: str) -> Dict[str, Any]:
    """
    Retrieve all message metadata (cost records + display text) for a session from DynamoDB

    Uses the SessionLookupIndex GSI to query records by session ID.
    Cost records have SK pattern: C#{timestamp}#{uuid}, GSI_SK: C#{timestamp}
    Display text records have SK pattern: D#{session_id}#{message_id}, GSI_SK: D#{message_id}

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

        # Query cost records (C#) and display text records (D#) in parallel
        cost_response = table.query(
            IndexName='SessionLookupIndex',
            KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').begins_with('C#')
        )
        display_response = table.query(
            IndexName='SessionLookupIndex',
            KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').begins_with('D#')
        )

        items = cost_response.get("Items", [])
        display_items = display_response.get("Items", [])
        metadata_index = {}

        logger.info(f"📦 DynamoDB returned {len(items)} cost record items, {len(display_items)} display text items")

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

        # Merge displayText from D# records into metadata index
        for item in display_items:
            if item.get('userId') != user_id:
                continue
            item_float = _convert_decimal_to_float(item)
            message_id_raw = item_float.get("messageId")
            message_id = str(int(message_id_raw)) if isinstance(message_id_raw, (int, float)) else str(message_id_raw)
            display_text = item_float.get("displayText")
            if display_text:
                if message_id in metadata_index:
                    metadata_index[message_id]["displayText"] = display_text
                else:
                    metadata_index[message_id] = {"displayText": display_text}
                logger.debug(f"🔗 Merged displayText for user message {message_id}")

        logger.info(f"📋 Metadata keys: {sorted(metadata_index.keys())}")
        return metadata_index

    except Exception as e:
        logger.error(f"Failed to query message metadata from DynamoDB: {e}", exc_info=True)
        return {}


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

        # Lazy backfill of session-cost-badge aggregates for legacy
        # sessions that pre-date write-time aggregation. One-shot per
        # session — subsequent reads see the denormalized values directly.
        if "totalCost" not in item:
            await _backfill_session_aggregates(
                session_id=session_id,
                user_id=user_id,
                session_item=item,
                table=table,
            )

        # Remove DynamoDB keys before validation
        for key in ['PK', 'SK', 'GSI_PK', 'GSI_SK']:
            item.pop(key, None)

        # Dedupe pending interrupts at the storage boundary so list_append
        # re-emits don't surface as duplicate consent prompts.
        if "pendingInterrupts" in item:
            item["pendingInterrupts"] = _dedupe_interrupt_dicts(item["pendingInterrupts"])

        # Lift the running compaction-summary turn count out of the nested
        # `compaction` map so it shows up as a top-level field on the
        # response model. Older sessions without compaction state simply
        # leave the field unset.
        compaction_data = item.get("compaction")
        if isinstance(compaction_data, dict):
            total = compaction_data.get("totalSummarizedTurns")
            if total is not None:
                item["totalSummarizedTurns"] = int(total)

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
            # JUSTIFICATION: Invalid pagination tokens should not break the request.
            # We fall back to starting from the beginning, which is a reasonable default.
            # This handles cases where tokens are corrupted, expired, or malformed.
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
    if not sessions_metadata_table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")

    return await _list_user_sessions_cloud(
        user_id=user_id,
        table_name=sessions_metadata_table,
        limit=limit,
        next_token=next_token
    )


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
                # JUSTIFICATION: Invalid pagination tokens should not break the request.
                # We fall back to no pagination, which is a reasonable default.
                # This handles cases where tokens are corrupted, expired, or malformed.
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
            query_params['Limit'] = limit

        # Pagination loop: DynamoDB's Limit caps items *evaluated*, not items
        # *returned* after application-level filtering (preview sessions, parse
        # failures). A single query may return fewer valid sessions than the
        # requested limit while still having more data in the partition. We keep
        # querying until we fill the page or exhaust the partition.
        sessions: list[SessionMetadata] = []
        last_evaluated_key = None

        while True:
            response = table.query(**query_params)

            for item in response['Items']:
                try:
                    item = _convert_decimal_to_float(item)

                    for key in ['PK', 'SK', 'GSI_PK', 'GSI_SK']:
                        item.pop(key, None)

                    # Skip preview sessions - they should not appear in user's session list
                    session_id = item.get('sessionId', '')
                    if is_preview_session(session_id):
                        continue

                    if "pendingInterrupts" in item:
                        item["pendingInterrupts"] = _dedupe_interrupt_dicts(item["pendingInterrupts"])

                    metadata = SessionMetadata.model_validate(item)
                    sessions.append(metadata)

                    # Stop collecting once we have enough
                    if limit and len(sessions) >= limit:
                        break
                except Exception as e:
                    # JUSTIFICATION: When listing sessions from DynamoDB, individual session parsing
                    # failures should not break the entire list operation. We skip corrupted sessions
                    # and continue processing others. This provides better UX than failing completely.
                    logger.warning(f"Failed to parse session item: {e}")
                    continue

            last_evaluated_key = response.get('LastEvaluatedKey')

            # Stop if we've filled the page or there's no more data
            if (limit and len(sessions) >= limit) or not last_evaluated_key:
                break

            # Continue querying from where DynamoDB left off
            query_params['ExclusiveStartKey'] = last_evaluated_key

        # Generate next_token only when there is genuinely more data to fetch
        next_page_token = None
        if last_evaluated_key and limit and len(sessions) >= limit:
            next_page_token = base64.b64encode(
                json.dumps(last_evaluated_key).encode('utf-8')
            ).decode('utf-8')

        logger.info(f"Listed {len(sessions)} sessions for user {user_id} from DynamoDB")

        return sessions, next_page_token

    except Exception as e:
        logger.error(f"Failed to list user sessions from DynamoDB: {e}", exc_info=True)
        # Propagate error - session listing failures should be visible to the user
        from fastapi import HTTPException
        from apis.shared.errors import ErrorCode, create_error_response
        raise HTTPException(
            status_code=503,
            detail=create_error_response(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message="Failed to list user sessions from database",
                detail=str(e)
            )
        )


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


# ============================================================================
# Pending OAuth interrupts
# ============================================================================
#
# Pending interrupts persist the breadcrumb the SSE stream emits when the
# agent pauses on `oauth_required`, so the frontend can rediscover them on
# reload. We do read-modify-write through the SessionLookupIndex GSI:
# OAuth flows are rare and one-at-a-time per user, so the simplicity wins
# over an UpdateExpression with list_append/REMOVE-by-index gymnastics.


def _interrupts_to_dynamo(interrupts: Iterable[PendingInterrupt]) -> List[Dict[str, Any]]:
    """Serialize PendingInterrupt list for DynamoDB storage (camelCase keys)."""
    return [item.model_dump(by_alias=True, exclude_none=True) for item in interrupts]


def _dedupe_interrupt_dicts(raw: Any) -> List[Dict[str, Any]]:
    """Last-write-wins dedupe of raw interrupt dicts by ``interruptId``.

    ``add_pending_interrupt`` uses ``list_append`` to be race-free against
    concurrent writers, which means re-emits of the same interrupt across
    stream replays accumulate as duplicate list entries. Storage-layer
    callers run this on the raw list before handing it to the model so
    Pydantic validation sees a clean list. Insertion order of the first
    occurrence is preserved.
    """
    if not raw or not isinstance(raw, list):
        return []
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        iid = entry.get("interruptId") or entry.get("interrupt_id")
        if not iid:
            continue
        if iid not in by_id:
            order.append(iid)
        by_id[iid] = entry
    return [by_id[iid] for iid in order]


def _interrupts_from_dynamo(raw: Any) -> List[PendingInterrupt]:
    """Parse stored interrupt entries with dedupe and corrupted-entry tolerance."""
    parsed: List[PendingInterrupt] = []
    for entry in _dedupe_interrupt_dicts(raw):
        try:
            parsed.append(PendingInterrupt.model_validate(entry))
        except Exception as exc:  # pragma: no cover — corrupted entry shouldn't break load
            logger.warning("Skipping unparseable pending_interrupts entry: %s", exc)
    return parsed


async def add_pending_interrupt(
    session_id: str,
    user_id: str,
    interrupt: PendingInterrupt,
) -> None:
    """Append a pending OAuth interrupt to the session record.

    Uses ``list_append`` with ``if_not_exists`` so concurrent writers can't
    lose each other's entries — no read-modify-write window. Re-emits of
    the same ``interrupt_id`` across stream replays accumulate as duplicate
    list entries and are collapsed last-write-wins by
    ``_interrupts_from_dynamo`` on read.

    No-op when the session metadata record is missing (preview sessions,
    sessions deleted mid-turn). The frontend will fall back to its in-memory
    consent state in that case.
    """
    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        logger.warning("DYNAMODB_SESSIONS_METADATA_TABLE_NAME not set; skipping pending_interrupts persistence")
        return

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            logger.info("Skipping pending_interrupts add — session %s not found", session_id)
            return

        sk = existing.get("SK")
        if not sk:
            logger.warning("Session %s has no SK; cannot update pending_interrupts", session_id)
            return

        new_entry = interrupt.model_dump(by_alias=True, exclude_none=True)

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET #pi = list_append(if_not_exists(#pi, :empty), :new)",
            ExpressionAttributeNames={"#pi": "pendingInterrupts"},
            ExpressionAttributeValues={":empty": [], ":new": [new_entry]},
        )
        logger.info(
            "Persisted pending_interrupt %s (provider=%s) for session %s",
            interrupt.interrupt_id, interrupt.provider_id, session_id,
        )
    except Exception as e:
        # Persistence failure must not break the live SSE flow — the in-memory
        # consent on the live tab still works; refresh-resume just won't.
        logger.error("Failed to persist pending_interrupt: %s", e, exc_info=True)


async def remove_pending_interrupts(
    session_id: str,
    user_id: str,
    interrupt_ids: Iterable[str],
) -> None:
    """Drop the given ``interrupt_ids`` from the session's pending list.

    No-op for unknown ids and missing sessions. Used by the resume path
    (after the agent successfully completes the resumed turn) and by the
    explicit dismiss endpoint.
    """
    drop_set = {iid for iid in interrupt_ids if iid}
    if not drop_set:
        return

    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        return

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            return

        sk = existing.get("SK")
        if not sk:
            return

        current = _interrupts_from_dynamo(existing.get("pendingInterrupts") or [])
        kept = [p for p in current if p.interrupt_id not in drop_set]

        if len(kept) == len(current):
            return  # Nothing matched

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET #pi = :pi",
            ExpressionAttributeNames={"#pi": "pendingInterrupts"},
            ExpressionAttributeValues={":pi": _interrupts_to_dynamo(kept)},
        )
        logger.info(
            "Cleared %d pending_interrupt(s) from session %s",
            len(current) - len(kept), session_id,
        )
    except Exception as e:
        logger.error("Failed to remove pending_interrupts: %s", e, exc_info=True)


async def get_pending_interrupts(session_id: str, user_id: str) -> List[PendingInterrupt]:
    """Return the current pending OAuth interrupts for a session.

    Returns an empty list when the session doesn't exist or has none.
    """
    metadata = await get_session_metadata(session_id, user_id)
    if not metadata:
        return []
    return list(metadata.pending_interrupts or [])


async def set_paused_turn(
    session_id: str,
    user_id: str,
    snapshot: PausedTurnSnapshot,
) -> None:
    """Persist (or replace) the agent-construction snapshot for a paused turn.

    Idempotent overwrite: re-emits within the same turn replace the prior
    snapshot rather than accumulating, since the snapshot is turn-scoped
    rather than interrupt-scoped — multiple OAuth interrupts in a single
    turn share the same construction context.

    No-op when the session metadata record is missing or when the table
    name env var is unset (preview/anonymous flows).
    """
    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        logger.warning("DYNAMODB_SESSIONS_METADATA_TABLE_NAME not set; skipping paused_turn persistence")
        return

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            logger.info("Skipping paused_turn write — session %s not found", session_id)
            return

        sk = existing.get("SK")
        if not sk:
            logger.warning("Session %s has no SK; cannot update paused_turn", session_id)
            return

        snapshot_dict = _convert_floats_to_decimal(
            snapshot.model_dump(by_alias=True, exclude_none=True)
        )

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET #pt = :pt",
            ExpressionAttributeNames={"#pt": "pausedTurn"},
            ExpressionAttributeValues={":pt": snapshot_dict},
        )
        logger.info("Persisted paused_turn snapshot for session %s", session_id)
    except Exception as e:
        # Best-effort: a write failure shouldn't break the live SSE flow.
        # The same-process resume still works via the in-memory agent cache.
        logger.error("Failed to persist paused_turn: %s", e, exc_info=True)


async def get_paused_turn(session_id: str, user_id: str) -> Optional[PausedTurnSnapshot]:
    """Return the persisted paused-turn snapshot for a session, if any."""
    metadata = await get_session_metadata(session_id, user_id)
    if not metadata:
        return None
    return metadata.paused_turn


async def clear_paused_turn(session_id: str, user_id: str) -> None:
    """Drop the paused-turn snapshot for a session.

    Called on successful resume completion, on explicit dismiss, and at the
    start of a non-resume invocation so a stale snapshot from an abandoned
    turn doesn't poison a fresh one.
    """
    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        return

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            return

        sk = existing.get("SK")
        if not sk:
            return

        if "pausedTurn" not in existing:
            return  # Already clear

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="REMOVE #pt",
            ExpressionAttributeNames={"#pt": "pausedTurn"},
        )
        logger.info("Cleared paused_turn for session %s", session_id)
    except Exception as e:
        logger.error("Failed to clear paused_turn: %s", e, exc_info=True)


async def set_truncated_turn(session_id: str, user_id: str) -> None:
    """Mark that the last turn ended in a recoverable max_tokens truncation.

    Lets the client re-show the "Continue" affordance after a page refresh
    (the truncated partial assistant message is already in AgentCore Memory;
    this flag is the only missing piece). Idempotent overwrite. Best-effort:
    a write failure logs but never breaks the live SSE flow. No-op when the
    session record is missing or the table env var is unset.
    """
    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        logger.warning("DYNAMODB_SESSIONS_METADATA_TABLE_NAME not set; skipping truncated_turn persistence")
        return

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            logger.info("Skipping truncated_turn write — session %s not found", session_id)
            return

        sk = existing.get("SK")
        if not sk:
            logger.warning("Session %s has no SK; cannot update truncated_turn", session_id)
            return

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="SET #ltc = :ltc",
            ExpressionAttributeNames={"#ltc": "lastTurnContinuable"},
            ExpressionAttributeValues={":ltc": True},
        )
        logger.info("Persisted truncated_turn marker for session %s", session_id)
    except Exception as e:
        logger.error("Failed to persist truncated_turn: %s", e, exc_info=True)


async def clear_truncated_turn(session_id: str, user_id: str) -> None:
    """Drop the truncated-turn marker.

    Called at the start of any new turn that isn't an interrupt-resume
    (fresh turn or a max_tokens continuation), so a stale marker can't
    resurrect "Continue" against a turn the user moved past. If a
    continuation itself re-truncates, the intercept re-sets the marker.
    """
    sessions_metadata_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not sessions_metadata_table:
        return

    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(sessions_metadata_table)

        existing = await _get_session_by_gsi(session_id, user_id, table)
        if not existing:
            return

        sk = existing.get("SK")
        if not sk:
            return

        if "lastTurnContinuable" not in existing:
            return  # Already clear

        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": sk},
            UpdateExpression="REMOVE #ltc",
            ExpressionAttributeNames={"#ltc": "lastTurnContinuable"},
        )
        logger.info("Cleared truncated_turn for session %s", session_id)
    except Exception as e:
        logger.error("Failed to clear truncated_turn: %s", e, exc_info=True)
