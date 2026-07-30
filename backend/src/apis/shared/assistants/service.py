"""Assistant service layer

This service handles storing and retrieving assistant data using DynamoDB.

Architecture:
- Cloud: Stores assistants in DynamoDB table specified by DYNAMODB_ASSISTANTS_TABLE_NAME
"""

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .models import AgentBinding, AgentModelConfig, Assistant
from .serialization import from_ddb, to_ddb_safe
from apis.shared.timestamps import to_iso, utc_now_iso

logger = logging.getLogger(__name__)


def _generate_assistant_id() -> str:
    """Generate a unique assistant ID with AST prefix"""
    return f"ast-{uuid.uuid4().hex[:12]}"


def _get_current_timestamp() -> str:
    """Get current timestamp in ISO 8601 format"""
    return utc_now_iso()


async def create_assistant_draft(owner_id: str, owner_name: str, name: Optional[str] = None) -> Assistant:
    """
    Create a minimal draft assistant with auto-generated ID

    This is used when the user clicks "Create New" to immediately
    generate an assistant ID that can be used for tagging documents.

    Args:
        owner_id: User identifier who owns this assistant (internal)
        owner_name: Display name of the owner (public)
        name: Optional assistant name (defaults to "Untitled Assistant")

    Returns:
        Assistant object with status=DRAFT
    """
    now = _get_current_timestamp()
    assistant_id = _generate_assistant_id()

    # Get vector index name from environment (defaults to 'assistants-index' if not set)
    vector_index_id = os.environ.get("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "assistants-index")

    assistant = Assistant(
        assistant_id=assistant_id,
        owner_id=owner_id,
        owner_name=owner_name,
        name=name or "Untitled Assistant",
        description="",
        instructions="",
        vector_index_id=vector_index_id,
        visibility="PRIVATE",
        tags=[],
        starters=[],
        usage_count=0,
        created_at=now,
        updated_at=now,
        status="DRAFT",
    )

    # Store the draft assistant
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    await _create_assistant_cloud(assistant, assistants_table)

    return assistant


async def create_assistant(
    owner_id: str,
    owner_name: str,
    name: str,
    description: str,
    instructions: str,
    vector_index_id: Optional[str] = None,
    visibility: str = "PRIVATE",
    tags: Optional[List[str]] = None,
    starters: Optional[List[str]] = None,
    emoji: Optional[str] = None,
    bindings: Optional[List[AgentBinding]] = None,
    model_settings: Optional[AgentModelConfig] = None,
    tagline: Optional[str] = None,
) -> Assistant:
    """
    Create a complete assistant with all required fields

    Args:
        owner_id: User identifier who owns this assistant (internal)
        owner_name: Display name of the owner (public)
        name: Assistant display name
        description: Short summary for UI cards
        instructions: System prompt for the assistant
        vector_index_id: Optional S3 vector index name (defaults to S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME from environment)
        visibility: Access control (PRIVATE, PUBLIC, SHARED)
        tags: Search keywords
        starters: Conversation starter prompts

    Returns:
        Assistant object with status=COMPLETE
    """
    now = _get_current_timestamp()
    assistant_id = _generate_assistant_id()

    # Get vector index name from environment
    if not vector_index_id:
        vector_index_id = os.environ.get("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME")

    assistant = Assistant(
        assistant_id=assistant_id,
        owner_id=owner_id,
        owner_name=owner_name,
        name=name,
        description=description,
        instructions=instructions,
        vector_index_id=vector_index_id,
        visibility=visibility,
        tags=tags or [],
        starters=starters or [],
        emoji=emoji,
        usage_count=0,
        created_at=now,
        updated_at=now,
        status="COMPLETE",
        bindings=bindings,
        model_settings=model_settings,
        tagline=tagline,
    )

    # Store the assistant
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    await _create_assistant_cloud(assistant, assistants_table)

    return assistant


async def _create_assistant_cloud(assistant: Assistant, table_name: str) -> None:
    """
    Store assistant in DynamoDB

    Args:
        assistant: Assistant object to store
        table_name: DynamoDB table name from DYNAMODB_ASSISTANTS_TABLE_NAME env var
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        # to_ddb_safe: modelConfig.params / binding config may carry floats, which
        # DynamoDB rejects — convert to Decimal on the whole item (strings pass through).
        item = to_ddb_safe(assistant.model_dump(by_alias=True, exclude_none=True))
        item["PK"] = f"AST#{assistant.assistant_id}"
        item["SK"] = "METADATA"

        # Add GSI keys for owner listings
        item["GSI_PK"] = f"OWNER#{assistant.owner_id}"
        item["GSI_SK"] = f"STATUS#{assistant.status}#CREATED#{assistant.created_at}"

        # Add GSI2 keys for visibility-based listings (VisibilityStatusIndex)
        # Reuse GSI_SK since both indexes use the same sort key pattern
        item["GSI2_PK"] = f"VISIBILITY#{assistant.visibility}"
        item["GSI2_SK"] = item["GSI_SK"]  # Reuse the same sort key value

        table.put_item(Item=item)

        logger.info(f"💾 Stored assistant {assistant.assistant_id} in DynamoDB table {table_name}")

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to store assistant in DynamoDB: {error_code} - {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to store assistant in DynamoDB: {e}")
        raise


async def get_assistant(assistant_id: str, owner_id: str) -> Optional[Assistant]:
    """
    Retrieve assistant by ID

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)

    Returns:
        Assistant object if found and owned by user, None otherwise
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    return await _get_assistant_cloud(assistant_id, owner_id, assistants_table)


async def get_assistant_with_access_check(
    assistant_id: str, user_id: str, user_email: str = None
) -> Tuple[Optional[Assistant], Optional[str]]:
    """
    Retrieve assistant by ID with visibility-based access control.

    Checks visibility:
    - PRIVATE: Only owner can access
    - PUBLIC: Anyone can access (returns "viewer" for non-owners)
    - SHARED: Only owner or users with share records can access

    Returns:
        Tuple of (assistant, permission). Permission is one of:
        - "owner" — caller owns the assistant
        - "editor" — caller has an editor share record
        - "viewer" — caller has a viewer share record OR assistant is PUBLIC
        - None — assistant not found or access denied

        (None, None) means either not found (404) or access denied (403).
        Caller must distinguish via assistant_exists() if it needs the distinction.
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    # Get assistant without ownership check first
    assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)

    if not assistant:
        return None, None

    # Owner always has full access regardless of visibility
    if assistant.owner_id == user_id:
        return assistant, "owner"

    # Non-owner access depends on visibility
    if assistant.visibility == "PRIVATE":
        logger.warning(
            f"Access denied: user {user_id} attempted to access PRIVATE assistant {assistant_id} owned by {assistant.owner_id}"
        )
        return None, None

    if assistant.visibility == "SHARED":
        if not user_email:
            logger.warning(f"Access denied: user_email required for SHARED assistant {assistant_id}")
            return None, None

        share_permission = await check_share_access(assistant_id, user_email)
        if not share_permission:
            logger.warning(
                f"Access denied: user {user_id} ({user_email}) attempted to access SHARED assistant {assistant_id} without share record"
            )
            return None, None
        return assistant, share_permission

    # PUBLIC: non-owners are viewers
    return assistant, "viewer"


async def resolve_assistant_permission(
    assistant_id: str, user_id: str, user_email: Optional[str] = None
) -> Tuple[Optional[Assistant], Optional[str]]:
    """
    Resolve the requesting user's permission on an assistant.

    Unlike get_assistant_with_access_check (which gates on visibility), this
    helper resolves *any* permission the user has regardless of visibility —
    so an editor share on a PRIVATE assistant still resolves to "editor".
    Returns (assistant, permission) or (assistant, None) if the user has no
    permission, or (None, None) if the assistant doesn't exist.

    Used by write routes that need to gate on permission level (owner/editor)
    rather than visibility.
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)
    if not assistant:
        return None, None

    if assistant.owner_id == user_id:
        return assistant, "owner"

    if user_email:
        share_permission = await check_share_access(assistant_id, user_email)
        if share_permission:
            return assistant, share_permission

    return assistant, None


async def bump_last_used_at(assistant_id: str, throttle_hours: int = 24) -> bool:
    """Record that an assistant was used in chat, throttled to one write
    per `throttle_hours`.

    Drives the KB-sync inactivity guard (docs/specs/assistant-kb-sync.md
    §7.3). A conditional write keeps concurrent chat turns from stampeding:
    only the caller that actually advances the stale timestamp gets True —
    that winner is responsible for resuming any paused_inactive policies.
    Any user's use counts, not just the owner's. Never raises.
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        return False

    try:
        import boto3
        from botocore.exceptions import ClientError

        now_dt = datetime.now(timezone.utc)
        floor = to_iso(now_dt - timedelta(hours=throttle_hours))
        table = boto3.resource("dynamodb").Table(assistants_table)
        table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"},
            UpdateExpression="SET lastUsedAt = :now",
            ExpressionAttributeValues={":now": to_iso(now_dt), ":floor": floor},
            ConditionExpression=(
                "attribute_exists(PK) AND (attribute_not_exists(lastUsedAt) OR lastUsedAt < :floor)"
            ),
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False  # fresh enough, or assistant gone — nothing to do
        logger.warning(f"Failed to bump lastUsedAt for {assistant_id}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Failed to bump lastUsedAt for {assistant_id}: {e}")
        return False


async def assistant_exists(assistant_id: str) -> bool:
    """
    Check if assistant exists (without access check)

    Args:
        assistant_id: Assistant identifier

    Returns:
        True if assistant exists, False otherwise
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)

    return assistant is not None



async def _get_assistant_cloud(assistant_id: str, owner_id: str, table_name: str) -> Optional[Assistant]:
    """
    Retrieve assistant from DynamoDB

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)
        table_name: DynamoDB table name

    Returns:
        Assistant object if found and owned by user, None otherwise
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        response = table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})

        if "Item" not in response:
            logger.info(f"Assistant {assistant_id} not found in DynamoDB")
            return None

        item = response["Item"]

        # Verify ownership
        if item.get("ownerId") != owner_id:
            logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
            return None

        return Assistant.model_validate(from_ddb(item))

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ResourceNotFoundException":
            logger.info(f"Table {table_name} not found")
        else:
            logger.error(f"Failed to retrieve assistant from DynamoDB: {error_code} - {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to retrieve assistant from DynamoDB: {e}", exc_info=True)
        return None


async def _get_assistant_cloud_without_ownership_check(assistant_id: str, table_name: str) -> Optional[Assistant]:
    """
    Retrieve assistant from DynamoDB without ownership verification

    Args:
        assistant_id: Assistant identifier
        table_name: DynamoDB table name

    Returns:
        Assistant object if found, None if not found

    Raises:
        Exception: On DynamoDB errors (not ResourceNotFoundException)
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        response = table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})

        if "Item" not in response:
            logger.info(f"Assistant {assistant_id} not found in DynamoDB")
            return None

        item = response["Item"]
        return Assistant.model_validate(from_ddb(item))

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        if error_code == "ResourceNotFoundException":
            logger.info(f"Table {table_name} not found")
            return None
        else:
            # Don't suppress real errors like AccessDeniedException
            logger.error(f"DynamoDB error retrieving assistant {assistant_id}: {error_code} - {error_message}")
            raise Exception(f"DynamoDB error ({error_code}): {error_message}") from e
    except Exception as e:
        logger.error(f"Failed to retrieve assistant {assistant_id} from DynamoDB: {e}", exc_info=True)
        raise


async def update_assistant(
    assistant_id: str,
    owner_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    visibility: Optional[str] = None,
    tags: Optional[List[str]] = None,
    starters: Optional[List[str]] = None,
    emoji: Optional[str] = None,
    status: Optional[str] = None,
    image_url: Optional[str] = None,
    bindings: Optional[List[AgentBinding]] = None,
    model_settings: Optional[AgentModelConfig] = None,
    tagline: Optional[str] = None,
) -> Optional[Assistant]:
    """
    Update assistant fields (deep merge)

    Only provided fields are updated; existing fields are preserved.
    Note: vector_index_id is not user-configurable and cannot be updated via this method.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)
        name: Optional new name
        description: Optional new description
        instructions: Optional new instructions
        visibility: Optional new visibility
        tags: Optional new tags
        starters: Optional new conversation starters
        status: Optional new status
        image_url: Optional new image URL

    Returns:
        Updated Assistant object if found and updated, None otherwise
    """
    # Get existing assistant
    existing = await get_assistant(assistant_id, owner_id)

    if not existing:
        return None

    # Build update dict with only provided fields
    updates = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if instructions is not None:
        updates["instructions"] = instructions
    if visibility is not None:
        updates["visibility"] = visibility
    if tags is not None:
        updates["tags"] = tags
    if starters is not None:
        updates["starters"] = starters
    if emoji is not None:
        updates["emoji"] = emoji
    if status is not None:
        updates["status"] = status
    if image_url is not None:
        updates["image_url"] = image_url
    # Phase 1: an explicit bindings list (incl. []) replaces the set; absent = untouched.
    # Clearing modelConfig to null via merge isn't supported yet (exclude_none) — R5.
    if bindings is not None:
        updates["bindings"] = bindings
    if model_settings is not None:
        updates["model_settings"] = model_settings
    if tagline is not None:
        updates["tagline"] = tagline

    # Always update the updated_at timestamp
    updates["updated_at"] = _get_current_timestamp()

    # Create updated assistant (merge with existing)
    existing_dict = existing.model_dump(by_alias=False)
    existing_dict.update(updates)

    updated_assistant = Assistant.model_validate(existing_dict)

    # Store updated assistant
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    await _update_assistant_cloud(updated_assistant, assistants_table)

    return updated_assistant


async def _update_assistant_cloud(assistant: Assistant, table_name: str) -> None:
    """
    Update assistant in DynamoDB

    Args:
        assistant: Updated assistant object
        table_name: DynamoDB table name
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        # Get existing assistant to check if status changed
        existing_response = table.get_item(Key={"PK": f"AST#{assistant.assistant_id}", "SK": "METADATA"})

        if "Item" not in existing_response:
            raise ValueError(f"Assistant {assistant.assistant_id} not found")

        existing_item = existing_response["Item"]
        old_status = existing_item.get("status")
        old_visibility = existing_item.get("visibility")
        status_changed = old_status != assistant.status
        visibility_changed = old_visibility != assistant.visibility

        # Build update expression
        update_parts = []
        expression_attribute_values = {}
        expression_attribute_names = {}

        # Fields that should never be updated (immutable or composite keys).
        #
        # GSI5_* and ``listing`` belong to the marketplace write path
        # (``assistants.listing_repository``) and must NOT be rewritten from here.
        # Reads hydrate ``Assistant`` (extra="allow") straight from the raw item, so the
        # index keys round-trip as extra model fields; without this guard a routine author
        # edit would re-write a stale GSI5 key onto a delisted agent and silently put it
        # back in the store. ``listing`` is excluded for the same reason in the other
        # direction: a stale in-memory copy must not clobber a concurrent review decision.
        immutable_fields = {
            "PK", "SK",
            "GSI_PK", "GSI_SK", "GSI2_PK", "GSI2_SK", "GSI5_PK", "GSI5_SK",
            "assistantId", "createdAt", "ownerId",
            "listing",
        }

        # Always update updatedAt
        update_parts.append("updatedAt = :updated_at")
        expression_attribute_values[":updated_at"] = assistant.updated_at

        # Update all fields from assistant model (excluding immutable fields).
        # to_ddb_safe converts any float in modelConfig.params / binding config to Decimal.
        assistant_dict = to_ddb_safe(assistant.model_dump(by_alias=True, exclude_none=True))

        # DynamoDB reserved keywords that need to be escaped
        reserved_keywords = {"status", "name", "data", "size", "type", "value"}

        for key, value in assistant_dict.items():
            # Skip immutable fields and composite keys
            if key in immutable_fields:
                continue

            # Skip updatedAt since we're already adding it explicitly above
            if key == "updatedAt":
                continue

            # Handle reserved words by using ExpressionAttributeNames
            if key in reserved_keywords:
                placeholder = f"#{key}"
                update_parts.append(f"{placeholder} = :{key}")
                expression_attribute_names[placeholder] = key
                expression_attribute_values[f":{key}"] = value
            else:
                update_parts.append(f"{key} = :{key}")
                expression_attribute_values[f":{key}"] = value

        # Update GSI_SK if status changed
        # Both GSI_SK and GSI2_SK use the same sort key pattern, so we can reuse the value
        if status_changed:
            gsi_sk_value = f"STATUS#{assistant.status}#CREATED#{assistant.created_at}"
            update_parts.append("GSI_SK = :gsi_sk")
            expression_attribute_values[":gsi_sk"] = gsi_sk_value
        else:
            # Status didn't change, reuse existing GSI_SK value
            gsi_sk_value = existing_item.get("GSI_SK")

        # Update GSI2 keys if status or visibility changed
        # Reuse GSI_SK value since both indexes use the same sort key pattern
        if status_changed or visibility_changed:
            update_parts.append("GSI2_PK = :gsi2_pk")
            update_parts.append("GSI2_SK = :gsi2_sk")  # Reuse the same value as GSI_SK
            expression_attribute_values[":gsi2_pk"] = f"VISIBILITY#{assistant.visibility}"
            expression_attribute_values[":gsi2_sk"] = gsi_sk_value

        update_expression = "SET " + ", ".join(update_parts)

        update_params = {
            "Key": {"PK": f"AST#{assistant.assistant_id}", "SK": "METADATA"},
            "UpdateExpression": update_expression,
            "ExpressionAttributeValues": expression_attribute_values,
            "ReturnValues": "NONE",
        }

        if expression_attribute_names:
            update_params["ExpressionAttributeNames"] = expression_attribute_names

        table.update_item(**update_params)

        logger.info(f"💾 Updated assistant {assistant.assistant_id} in DynamoDB table {table_name}")

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to update assistant in DynamoDB: {error_code} - {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to update assistant in DynamoDB: {e}")
        raise


async def list_user_assistants(
    owner_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None,
    include_drafts: bool = False,
    include_public: bool = False,
) -> Tuple[List[Assistant], Optional[str]]:
    """
    List assistants for a user with pagination support

    Args:
        owner_id: User identifier
        limit: Maximum number of assistants to return (optional)
        next_token: Pagination token for retrieving next page (optional)
        include_drafts: Whether to include draft assistants
        include_public: Deprecated/Ignored. Public assistants are no longer listed in a general index.

    Returns:
        Tuple of (list of Assistant objects, next_token if more assistants exist)
        Assistants are sorted by created_at descending (most recent first)
    """
    # Force include_public to False as the feature is removed
    include_public = False

    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    return await _list_user_assistants_cloud(
        owner_id,
        table_name=assistants_table,
        limit=limit,
        next_token=next_token,
        include_drafts=include_drafts,
        include_public=include_public,
    )


def _apply_pagination(
    assistants: List[Assistant], limit: Optional[int] = None, next_token: Optional[str] = None
) -> Tuple[List[Assistant], Optional[str]]:
    """
    Apply pagination to a list of assistants

    Args:
        assistants: List of assistants (sorted by created_at descending)
        limit: Maximum number of assistants to return
        next_token: Pagination token (base64-encoded created_at timestamp)

    Returns:
        Tuple of (paginated assistants, next_token if more assistants exist)
    """
    start_index = 0

    # Decode next_token if provided
    if next_token:
        try:
            decoded = base64.b64decode(next_token).decode("utf-8")
            # Find first assistant with created_at < decoded timestamp
            for idx, assistant in enumerate(assistants):
                if assistant.created_at < decoded:
                    start_index = idx
                    break
            else:
                # No assistant found with timestamp < decoded, reached end
                start_index = len(assistants)
        except Exception as e:
            logger.warning(f"Invalid next_token: {e}, starting from beginning")
            start_index = 0

    # Apply start index
    paginated_assistants = assistants[start_index:]

    # Apply limit
    if limit and limit > 0:
        paginated_assistants = paginated_assistants[:limit]
        # Check if there are more assistants
        if start_index + limit < len(assistants):
            # Use created_at of last assistant as next token
            last_assistant = paginated_assistants[-1]
            next_token = base64.b64encode(last_assistant.created_at.encode("utf-8")).decode("utf-8")
        else:
            next_token = None
    else:
        next_token = None

    return paginated_assistants, next_token


async def _list_user_assistants_cloud(
    owner_id: str,
    table_name: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None,
    include_drafts: bool = False,
    include_public: bool = False,
) -> Tuple[List[Assistant], Optional[str]]:
    """
    List assistants for a user from DynamoDB with pagination

    Args:
        owner_id: User identifier
        table_name: DynamoDB table name
        limit: Maximum number of assistants to return (optional)
        next_token: Pagination token (optional)
        include_drafts: Whether to include draft assistants
        include_public: Ignored.

    Returns:
        Tuple of (list of Assistant objects, next_token if more exist)
    """
    try:
        import boto3
        from boto3.dynamodb.conditions import Key
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        # Build filter expression for status
        filter_parts = []
        expression_attribute_values = {}

        if not include_drafts:
            filter_parts.append("#status <> :draft")
            expression_attribute_values[":draft"] = "DRAFT"

        # Parse pagination token
        owner_exclusive_start_key = None
        if next_token:
            try:
                decoded = base64.b64decode(next_token).decode("utf-8")
                token_data = json.loads(decoded)
                owner_exclusive_start_key = token_data.get("owner_key")
            except Exception as e:
                logger.warning(f"Invalid next_token: {e}, ignoring pagination")

        # Build base query parameters
        base_query_params = {
            "ScanIndexForward": False,  # Descending order (most recent first)
        }

        if limit and limit > 0:
            base_query_params["Limit"] = limit

        if filter_parts:
            base_query_params["FilterExpression"] = " AND ".join(filter_parts)
            base_query_params["ExpressionAttributeNames"] = {"#status": "status"}
            base_query_params["ExpressionAttributeValues"] = expression_attribute_values

        # Query user's own assistants
        owner_query_params = {
            **base_query_params,
            "IndexName": "OwnerStatusIndex",
            "KeyConditionExpression": Key("GSI_PK").eq(f"OWNER#{owner_id}"),
        }

        if owner_exclusive_start_key:
            owner_query_params["ExclusiveStartKey"] = owner_exclusive_start_key

        owner_response = table.query(**owner_query_params)

        all_assistants = []
        for item in owner_response.get("Items", []):
            try:
                all_assistants.append(Assistant.model_validate(from_ddb(item)))
            except Exception as e:
                logger.warning(f"Failed to parse assistant item: {e}")
                continue

        owner_last_key = owner_response.get("LastEvaluatedKey")

        # Generate next_token from LastEvaluatedKeys
        next_page_token = None
        token_data = {}
        if owner_last_key:
            token_data["owner_key"] = owner_last_key

        if token_data:
            encoded = json.dumps(token_data)
            next_page_token = base64.b64encode(encoded.encode("utf-8")).decode("utf-8")

        logger.info(f"Listed {len(all_assistants)} assistants for user {owner_id}")
        return all_assistants, next_page_token

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to list user assistants from DynamoDB: {error_code} - {e}")
        return [], None
    except Exception as e:
        logger.error(f"Failed to list user assistants from DynamoDB: {e}", exc_info=True)
        return [], None


class AssistantListedError(Exception):
    """A hard delete refused because the Agent carries a marketplace listing (§5.2).

    Separate from returning ``False`` (which means "not found / not yours") because the
    caller has to tell these apart: one is a 404, the other is a 409 whose message names the
    way forward.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def assert_deletable(assistant_id: str, owner_id: str) -> None:
    """Raise ``AssistantListedError`` if this Agent's listing forbids deletion (§5.2).

    Exists so a caller that does destructive work *before* the record delete can refuse
    up front. ``/assistants/{id}`` soft-deletes documents and removes sync policies first,
    so discovering the refusal at the record write would leave the Agent gutted and still
    in the store — worse than either outcome on its own.

    Silent when the Agent is missing or not the caller's: that is the delete path's own 404,
    and pre-empting it here would turn "not found" into "not deletable".
    """
    existing = await get_assistant(assistant_id, owner_id)
    if existing:
        _assert_listing_allows_delete(existing)


def _assert_listing_allows_delete(existing) -> None:
    """The §5.2 rule itself, so the guard and the delete cannot disagree."""
    listing = getattr(existing, "listing", None)
    state = getattr(listing, "state", None) if listing else None
    if state is None or state == "private":
        return
    # The message names the path forward rather than just refusing — an author who is told
    # "no" without being told "do this instead" files a ticket.
    nudge = (
        "Request withdrawal first, then delete it once an admin has removed it from the store."
        if state in ("published", "withdrawal_requested")
        else "Take it back to private first, then delete it."
    )
    raise AssistantListedError(
        f"This agent can't be deleted while it has a marketplace listing ({state}). {nudge}"
    )


async def delete_assistant(assistant_id: str, owner_id: str) -> bool:
    """
    Delete an assistant permanently (hard delete)

    ⚠️ **Refused while a listing exists** (version-snapshots §5.2). Ownership alone used to
    be enough, so an author could hard-delete an approved Agent straight out of the store —
    the same unilateral removal that ``withdrawal_requested`` exists to stop, except
    irreversible and taking the review history with it.

    ``taken_down`` is covered deliberately: an author must not be able to delete their way
    out of a takedown record. So is ``withdrawal_requested`` — a pending request is not a
    granted one. Only ``private`` (or no listing at all) may be deleted, which is reachable
    from every other state through the normal paths.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)

    Returns:
        True if deleted successfully, False if not found or not owned

    Raises:
        AssistantListedError: the Agent has a listing that is not ``private``
    """
    # Verify ownership first
    existing = await get_assistant(assistant_id, owner_id)

    if not existing:
        return False

    _assert_listing_allows_delete(existing)

    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    return await _delete_assistant_cloud(assistant_id, assistants_table)


async def _delete_assistant_cloud(assistant_id: str, table_name: str) -> bool:
    """
    Delete assistant from DynamoDB

    Args:
        assistant_id: Assistant identifier
        table_name: DynamoDB table name

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        # Child rows first. Marketplace problem reports (D15) live under this same
        # partition precisely so they never outlive what they concern — and an orphaned
        # *open* report is worse than untidy: it keeps its sparse GSI6 key, so it would
        # sit in the admin queue forever pointing at an Agent nobody can open. Best
        # effort, because failing to tidy up must not make the Agent undeletable; the
        # queue projection flags a row whose Agent is gone so an admin can still clear it.
        try:
            from .reports import delete_reports_for_agent

            await delete_reports_for_agent(assistant_id)
        except Exception:
            logger.warning(
                f"Failed to delete reports for assistant {assistant_id}; "
                "the admin queue will show them as orphaned",
                exc_info=True,
            )

        table.delete_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})

        logger.info(f"🗑️ Deleted assistant {assistant_id} from DynamoDB table {table_name}")
        return True

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ResourceNotFoundException":
            logger.warning(f"Assistant {assistant_id} not found in DynamoDB")
        else:
            logger.error(f"Failed to delete assistant from DynamoDB: {error_code} - {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to delete assistant from DynamoDB: {e}")
        return False


# ========== Share Management Functions ==========


async def share_assistant(
    assistant_id: str, owner_id: str, emails: List[str], permission: str = "viewer"
) -> bool:
    """
    Share an assistant with specified email addresses.

    Creates share records in DynamoDB for each email. Emails are normalized to lowercase.
    Only the owner can share their assistant.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (must be the owner)
        emails: List of email addresses to share with
        permission: Permission level granted to each new share ("viewer" or "editor").
            Existing share records for the same email are overwritten with this value.

    Returns:
        True if shares were created successfully, False otherwise
    """
    if permission not in ("viewer", "editor"):
        logger.warning(f"Invalid permission '{permission}' for share_assistant; rejecting")
        return False

    # Verify ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Cannot share assistant {assistant_id}: not found or not owned by {owner_id}")
        return False

    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Normalize emails to lowercase
        normalized_emails = [email.lower().strip() for email in emails if email.strip()]

        if not normalized_emails:
            logger.warning(f"No valid emails provided for sharing assistant {assistant_id}")
            return False

        # Create share records for each email
        for email in normalized_emails:
            try:
                table.put_item(
                    Item={
                        "PK": f"AST#{assistant_id}",
                        "SK": f"SHARE#{email}",
                        "GSI3_PK": f"SHARE#{email}",
                        "GSI3_SK": f"AST#{assistant_id}",
                        "assistantId": assistant_id,
                        "email": email,
                        "createdAt": _get_current_timestamp(),
                        "firstInteracted": False,
                        "permission": permission,
                    }
                )
                logger.info(f"Created share record ({permission}) for assistant {assistant_id} with {email}")
            except ClientError as e:
                logger.error(f"Failed to create share record for {email}: {e}")
                # Continue with other emails even if one fails

        return True

    except Exception as e:
        logger.error(f"Error sharing assistant {assistant_id}: {e}", exc_info=True)
        return False


async def update_share_permission(
    assistant_id: str, owner_id: str, email: str, permission: str
) -> bool:
    """
    Update the permission level of an existing share record.

    Only the owner can change share permissions. Returns False if the assistant
    is not owned by `owner_id`, the share record does not exist, or `permission`
    is invalid.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (must be the owner)
        email: Email of the existing share to update (normalized lowercase)
        permission: New permission level ("viewer" or "editor")

    Returns:
        True on success, False otherwise
    """
    if permission not in ("viewer", "editor"):
        logger.warning(f"Invalid permission '{permission}' for update_share_permission")
        return False

    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(
            f"Cannot update share permission on {assistant_id}: not found or not owned by {owner_id}"
        )
        return False

    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    normalized_email = email.lower().strip()
    if not normalized_email:
        return False

    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SHARE#{normalized_email}"},
            UpdateExpression="SET #perm = :perm",
            ExpressionAttributeNames={"#perm": "permission"},
            ExpressionAttributeValues={":perm": permission},
            ConditionExpression="attribute_exists(PK)",
        )
        logger.info(
            f"Updated share permission for assistant {assistant_id}, email {normalized_email} -> {permission}"
        )
        return True

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ConditionalCheckFailedException":
            logger.warning(
                f"Share record not found: assistant={assistant_id}, email={normalized_email}"
            )
        else:
            logger.error(f"Failed to update share permission: {error_code} - {e}")
        return False
    except Exception as e:
        logger.error(f"Error updating share permission: {e}", exc_info=True)
        return False


async def unshare_assistant(assistant_id: str, owner_id: str, emails: List[str]) -> bool:
    """
    Remove shares from an assistant for specified email addresses.

    Deletes share records from DynamoDB. Only the owner can unshare.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (must be the owner)
        emails: List of email addresses to remove from shares

    Returns:
        True if shares were removed successfully, False otherwise
    """
    # Verify ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Cannot unshare assistant {assistant_id}: not found or not owned by {owner_id}")
        return False

    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Normalize emails to lowercase
        normalized_emails = [email.lower().strip() for email in emails if email.strip()]

        if not normalized_emails:
            logger.warning(f"No valid emails provided for unsharing assistant {assistant_id}")
            return False

        # Delete share records for each email
        for email in normalized_emails:
            try:
                table.delete_item(Key={"PK": f"AST#{assistant_id}", "SK": f"SHARE#{email}"})
                logger.info(f"Removed share record for assistant {assistant_id} with {email}")
            except ClientError as e:
                logger.error(f"Failed to delete share record for {email}: {e}")
                # Continue with other emails even if one fails

        return True

    except Exception as e:
        logger.error(f"Error unsharing assistant {assistant_id}: {e}", exc_info=True)
        return False


async def list_assistant_shares(assistant_id: str, owner_id: str) -> List[dict]:
    """
    List all share records (email + permission) for an assistant.

    Caller has already verified they're permitted to read the share list
    (owners and editors); we still verify the assistant exists.

    Args:
        assistant_id: Assistant identifier
        owner_id: True owner of the assistant — used to fetch the assistant.
            Pass the assistant's real ownerId (not the requesting user).

    Returns:
        List of dicts shaped like {"email": str, "permission": "viewer"|"editor"}.
        Missing `permission` on legacy records defaults to "viewer".
    """
    # Verify the assistant exists (and the supplied owner_id matches it)
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Cannot list shares for assistant {assistant_id}: not found or owner mismatch")
        return []

    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    try:
        import boto3
        from boto3.dynamodb.conditions import Key
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Query all share records for this assistant
        response = table.query(
            KeyConditionExpression=Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("SHARE#")
        )

        shares: List[dict] = []
        for item in response.get("Items", []):
            email = item.get("email")
            if not email:
                continue
            shares.append({"email": email, "permission": item.get("permission", "viewer")})

        logger.info(f"Found {len(shares)} shares for assistant {assistant_id}")
        return shares

    except ClientError as e:
        logger.error(f"Error listing shares for assistant {assistant_id}: {e}")
        return []
    except Exception as e:
        logger.error(f"Error listing shares for assistant {assistant_id}: {e}", exc_info=True)
        return []


async def check_share_access(assistant_id: str, user_email: str) -> Optional[str]:
    """
    Look up a user's share permission on an assistant.

    Args:
        assistant_id: Assistant identifier
        user_email: User's email address (will be normalized to lowercase)

    Returns:
        "viewer" or "editor" if the user has a share record, None otherwise.
        Legacy share records without a `permission` attribute resolve to "viewer".

    Raises:
        Exception: On DynamoDB errors (not ResourceNotFoundException)
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Normalize email
        normalized_email = user_email.lower().strip()

        # Check if share record exists
        response = table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": f"SHARE#{normalized_email}"})

        item = response.get("Item")
        if not item:
            return None

        permission = item.get("permission", "viewer")
        if permission not in ("viewer", "editor"):
            # Defensive: unknown stored value -> treat as viewer
            return "viewer"
        return permission

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        if error_code == "ResourceNotFoundException":
            logger.debug(f"Table {assistants_table} not found")
            return None
        else:
            # Don't suppress real errors like AccessDeniedException
            logger.error(f"DynamoDB error checking share access for {assistant_id}: {error_code} - {error_message}")
            raise Exception(f"DynamoDB error ({error_code}): {error_message}") from e
    except Exception as e:
        logger.error(f"Error checking share access for {assistant_id}: {e}", exc_info=True)
        raise


async def mark_share_as_interacted(assistant_id: str, user_email: str) -> bool:
    """
    Mark a share record as interacted (user has opened the assistant in chat).

    This is idempotent - safe to call multiple times. Once set to True, it stays True.

    Args:
        assistant_id: Assistant identifier
        user_email: User's email address (will be normalized to lowercase)

    Returns:
        True if updated successfully, False otherwise
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Normalize email
        normalized_email = user_email.lower().strip()

        # Update the share record to set firstInteracted = True
        # Use condition to only update if the record exists
        table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SHARE#{normalized_email}"},
            UpdateExpression="SET firstInteracted = :true",
            ExpressionAttributeValues={":true": True},
            # Condition: only update if share record exists
            ConditionExpression="attribute_exists(PK)",
        )

        logger.info(f"Marked share as interacted: assistant={assistant_id}, email={normalized_email}")
        return True

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ConditionalCheckFailedException":
            logger.debug(f"Share record not found for assistant {assistant_id}, email {normalized_email}")
        else:
            logger.error(f"Error marking share as interacted: {e}")
        return False
    except Exception as e:
        logger.error(f"Error marking share as interacted: {e}", exc_info=True)
        return False


async def list_shared_with_user(user_email: str) -> List[Assistant]:
    """
    List all assistants shared with a specific user email.

    Args:
        user_email: User's email address (will be normalized to lowercase)

    Returns:
        List of Assistant objects shared with this email
    """
    assistants_table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    try:
        import boto3
        from boto3.dynamodb.conditions import Key
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Normalize email
        normalized_email = user_email.lower().strip()

        # Query GSI3 for all assistants shared with this email
        response = table.query(IndexName="SharedWithIndex", KeyConditionExpression=Key("GSI3_PK").eq(f"SHARE#{normalized_email}"))

        assistants = []
        for item in response.get("Items", []):
            assistant_id = item.get("assistantId")
            first_interacted = item.get("firstInteracted", False)  # Default to False if not present
            share_permission = item.get("permission", "viewer")  # Legacy records default to viewer

            if assistant_id:
                # Get the full assistant metadata
                assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)
                if assistant:
                    # Attach share metadata as dynamic attributes
                    assistant.first_interacted = first_interacted
                    assistant.user_permission = share_permission
                    assistants.append(assistant)

        logger.info(f"Found {len(assistants)} assistants shared with {normalized_email}")
        return assistants

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ResourceNotFoundException":
            logger.debug(f"SharedWithIndex GSI not found - sharing feature may not be deployed yet")
        else:
            logger.error(f"Error listing shared assistants: {e}")
        return []
    except Exception as e:
        logger.error(f"Error listing shared assistants: {e}", exc_info=True)
        return []
