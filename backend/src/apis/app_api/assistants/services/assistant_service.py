"""Assistant service layer

This service handles storing and retrieving assistant data.
It supports both local file storage and cloud DynamoDB storage.

Architecture:
- Local: Stores assistants as individual JSON files in backend/src/assistants/
- Cloud: Stores assistants in DynamoDB table specified by ASSISTANTS_TABLE_NAME
"""

import base64
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from apis.app_api.assistants.models import Assistant
from apis.app_api.storage.paths import get_assistant_path, get_assistants_root

logger = logging.getLogger(__name__)


def _generate_assistant_id() -> str:
    """Generate a unique assistant ID with AST prefix"""
    return f"ast-{uuid.uuid4().hex[:12]}"


def _get_current_timestamp() -> str:
    """Get current timestamp in ISO 8601 format"""
    return datetime.utcnow().isoformat() + "Z"


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
    vector_index_id = os.environ.get("ASSISTANTS_VECTOR_STORE_INDEX_NAME", "assistants-index")

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
        usage_count=0,
        created_at=now,
        updated_at=now,
        status="DRAFT",
    )

    # Store the draft assistant
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        await _create_assistant_cloud(assistant, assistants_table)
    else:
        await _create_assistant_local(assistant)

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
) -> Assistant:
    """
    Create a complete assistant with all required fields

    Args:
        owner_id: User identifier who owns this assistant (internal)
        owner_name: Display name of the owner (public)
        name: Assistant display name
        description: Short summary for UI cards
        instructions: System prompt for the assistant
        vector_index_id: Optional S3 vector index name (defaults to ASSISTANTS_VECTOR_STORE_INDEX_NAME from environment)
        visibility: Access control (PRIVATE, PUBLIC, SHARED)
        tags: Search keywords

    Returns:
        Assistant object with status=COMPLETE
    """
    now = _get_current_timestamp()
    assistant_id = _generate_assistant_id()

    # Get vector index name from environment
    if not vector_index_id:
        vector_index_id = os.environ.get("ASSISTANTS_VECTOR_STORE_INDEX_NAME")

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
        usage_count=0,
        created_at=now,
        updated_at=now,
        status="COMPLETE",
    )

    # Store the assistant
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        await _create_assistant_cloud(assistant, assistants_table)
    else:
        await _create_assistant_local(assistant)

    return assistant


async def _create_assistant_local(assistant: Assistant) -> None:
    """
    Store assistant in local file storage

    Args:
        assistant: Assistant object to store
    """
    assistant_file = get_assistant_path(assistant.assistant_id)

    # Ensure parent directory exists
    assistant_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Write assistant data to file
        with open(assistant_file, "w") as f:
            json.dump(assistant.model_dump(by_alias=True, exclude_none=True), f, indent=2)

        logger.info(f"💾 Stored assistant {assistant.assistant_id} in {assistant_file}")

    except Exception as e:
        logger.error(f"Failed to store assistant in local file: {e}")
        raise


async def _create_assistant_cloud(assistant: Assistant, table_name: str) -> None:
    """
    Store assistant in DynamoDB

    Args:
        assistant: Assistant object to store
        table_name: DynamoDB table name from ASSISTANTS_TABLE_NAME env var
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        item = assistant.model_dump(by_alias=True, exclude_none=True)
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
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        return await _get_assistant_cloud(assistant_id, owner_id, assistants_table)
    else:
        return await _get_assistant_local(assistant_id, owner_id)


async def get_assistant_with_access_check(assistant_id: str, user_id: str, user_email: str = None) -> Optional[Assistant]:
    """
    Retrieve assistant by ID with visibility-based access control

    Checks visibility:
    - PRIVATE: Only owner can access
    - PUBLIC: Anyone can access
    - SHARED: Only owner or users with share records can access

    Args:
        assistant_id: Assistant identifier
        user_id: User identifier requesting access
        user_email: User's email address (required for SHARED visibility check)

    Returns:
        Assistant object if found and access granted, None otherwise
        None can mean either:
        - Assistant not found (404)
        - Access denied (403 for PRIVATE assistant not owned by user, or SHARED without share record)
    """
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    # Get assistant without ownership check first
    if assistants_table:
        assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)
    else:
        assistant = await _get_assistant_local_without_ownership_check(assistant_id)

    if not assistant:
        # Assistant not found
        return None

    # Check visibility-based access
    if assistant.visibility == "PRIVATE":
        # Only owner can access PRIVATE assistants
        if assistant.owner_id != user_id:
            logger.warning(f"Access denied: user {user_id} attempted to access PRIVATE assistant {assistant_id} owned by {assistant.owner_id}")
            return None
    elif assistant.visibility == "SHARED":
        # SHARED: owner or users with share records can access
        if assistant.owner_id != user_id:
            # Not the owner, check if user has share access
            if not user_email:
                logger.warning(f"Access denied: user_email required for SHARED assistant {assistant_id}")
                return None

            has_share = await check_share_access(assistant_id, user_email)
            if not has_share:
                logger.warning(
                    f"Access denied: user {user_id} ({user_email}) attempted to access SHARED assistant {assistant_id} without share record"
                )
                return None
    # PUBLIC is accessible by anyone

    return assistant


async def assistant_exists(assistant_id: str) -> bool:
    """
    Check if assistant exists (without access check)

    Args:
        assistant_id: Assistant identifier

    Returns:
        True if assistant exists, False otherwise
    """
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)
    else:
        assistant = await _get_assistant_local_without_ownership_check(assistant_id)

    return assistant is not None


async def get_all_assistants() -> List[Assistant]:
    """
    Retrieve all assistants

    Returns:
        List of Assistant objects
    """
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if assistants_table:
        return await _get_all_assistants_cloud(assistants_table)
    else:
        return await _get_all_assistants_local()


async def _get_assistant_local(assistant_id: str, owner_id: str) -> Optional[Assistant]:
    """
    Retrieve assistant from local file storage

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)

    Returns:
        Assistant object if found and owned by user, None otherwise
    """
    assistant_file = get_assistant_path(assistant_id)

    if not assistant_file.exists():
        return None

    try:
        with open(assistant_file, "r") as f:
            data = json.load(f)

        # Verify ownership
        if data.get("ownerId") != owner_id:
            logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
            return None

        return Assistant.model_validate(data)

    except Exception as e:
        logger.error(f"Failed to read assistant from local file: {e}")
        return None


async def _get_assistant_local_without_ownership_check(assistant_id: str) -> Optional[Assistant]:
    """
    Retrieve assistant from local file storage without ownership verification

    Args:
        assistant_id: Assistant identifier

    Returns:
        Assistant object if found, None otherwise
    """
    assistant_file = get_assistant_path(assistant_id)

    if not assistant_file.exists():
        return None

    try:
        with open(assistant_file, "r") as f:
            data = json.load(f)

        return Assistant.model_validate(data)

    except Exception as e:
        logger.error(f"Failed to read assistant from local file: {e}")
        return None


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

        return Assistant.model_validate(item)

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
        return Assistant.model_validate(item)

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        if error_code == 'ResourceNotFoundException':
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
    status: Optional[str] = None,
    image_url: Optional[str] = None,
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
    if status is not None:
        updates["status"] = status
    if image_url is not None:
        updates["image_url"] = image_url

    # Always update the updated_at timestamp
    updates["updated_at"] = _get_current_timestamp()

    # Create updated assistant (merge with existing)
    existing_dict = existing.model_dump(by_alias=False)
    existing_dict.update(updates)

    updated_assistant = Assistant.model_validate(existing_dict)

    # Store updated assistant
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        await _update_assistant_cloud(updated_assistant, assistants_table)
    else:
        await _update_assistant_local(updated_assistant)

    return updated_assistant


async def _update_assistant_local(assistant: Assistant) -> None:
    """
    Update assistant in local file storage

    Args:
        assistant: Updated assistant object
    """
    assistant_file = get_assistant_path(assistant.assistant_id)

    try:
        with open(assistant_file, "w") as f:
            json.dump(assistant.model_dump(by_alias=True, exclude_none=True), f, indent=2)

        logger.info(f"💾 Updated assistant {assistant.assistant_id} in {assistant_file}")

    except Exception as e:
        logger.error(f"Failed to update assistant in local file: {e}")
        raise


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

        # Fields that should never be updated (immutable or composite keys)
        immutable_fields = {"PK", "SK", "GSI_PK", "GSI_SK", "GSI2_PK", "GSI2_SK", "assistantId", "createdAt", "ownerId"}

        # Always update updatedAt
        update_parts.append("updatedAt = :updated_at")
        expression_attribute_values[":updated_at"] = assistant.updated_at

        # Update all fields from assistant model (excluding immutable fields)
        assistant_dict = assistant.model_dump(by_alias=True, exclude_none=True)

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
    include_archived: bool = False,
    include_drafts: bool = False,
    include_public: bool = False,
) -> Tuple[List[Assistant], Optional[str]]:
    """
    List assistants for a user with pagination support

    Args:
        owner_id: User identifier
        limit: Maximum number of assistants to return (optional)
        next_token: Pagination token for retrieving next page (optional)
        include_archived: Whether to include archived assistants
        include_drafts: Whether to include draft assistants
        include_public: Deprecated/Ignored. Public assistants are no longer listed in a general index.

    Returns:
        Tuple of (list of Assistant objects, next_token if more assistants exist)
        Assistants are sorted by created_at descending (most recent first)
    """
    # Force include_public to False as the feature is removed
    include_public = False

    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        return await _list_user_assistants_cloud(
            owner_id,
            table_name=assistants_table,
            limit=limit,
            next_token=next_token,
            include_archived=include_archived,
            include_drafts=include_drafts,
            include_public=include_public,
        )
    else:
        return await _list_user_assistants_local(
            owner_id,
            limit=limit,
            next_token=next_token,
            include_archived=include_archived,
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


async def _list_user_assistants_local(
    owner_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None,
    include_archived: bool = False,
    include_drafts: bool = False,
    include_public: bool = False,
) -> Tuple[List[Assistant], Optional[str]]:
    """
    List assistants for a user from local file storage with pagination

    Args:
        owner_id: User identifier
        limit: Maximum number of assistants to return (optional)
        next_token: Pagination token (optional)
        include_archived: Whether to include archived assistants
        include_drafts: Whether to include draft assistants
        include_public: Ignored.

    Returns:
        Tuple of (list of Assistant objects, next_token if more exist)
    """
    assistants_root = get_assistants_root()

    if not assistants_root.exists():
        logger.info(f"Assistants directory does not exist: {assistants_root}")
        return [], None

    assistants = []

    try:
        # Iterate through all assistant files
        for assistant_file in assistants_root.glob("assistant_*.json"):
            try:
                with open(assistant_file, "r") as f:
                    data = json.load(f)

                # Parse assistant
                assistant = Assistant.model_validate(data)

                # Filter logic: include if owned by user
                is_owner = data.get("ownerId") == owner_id

                if not is_owner:
                    continue

                # Filter by status
                if not include_archived and assistant.status == "ARCHIVED":
                    continue
                if not include_drafts and assistant.status == "DRAFT":
                    continue

                assistants.append(assistant)

            except Exception as e:
                logger.warning(f"Failed to read assistant file {assistant_file}: {e}")
                continue

        # Sort by created_at descending (most recent first)
        assistants.sort(key=lambda x: x.created_at, reverse=True)

        logger.info(f"Found {len(assistants)} assistants for user {owner_id}")

        # Apply pagination
        paginated_assistants, next_page_token = _apply_pagination(assistants, limit, next_token)

        return paginated_assistants, next_page_token

    except Exception as e:
        logger.error(f"Failed to list user assistants from local storage: {e}")
        return [], None


async def _list_user_assistants_cloud(
    owner_id: str,
    table_name: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None,
    include_archived: bool = False,
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
        include_archived: Whether to include archived assistants
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

        if not include_archived:
            filter_parts.append("#status <> :archived")
            expression_attribute_values[":archived"] = "ARCHIVED"
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
                all_assistants.append(Assistant.model_validate(item))
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


async def archive_assistant(assistant_id: str, owner_id: str) -> Optional[Assistant]:
    """
    Archive an assistant (soft delete - sets status to ARCHIVED)

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)

    Returns:
        Updated Assistant object with status=ARCHIVED, None if not found
    """
    return await update_assistant(assistant_id=assistant_id, owner_id=owner_id, status="ARCHIVED")


async def delete_assistant(assistant_id: str, owner_id: str) -> bool:
    """
    Delete an assistant permanently (hard delete)

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (for ownership verification)

    Returns:
        True if deleted successfully, False otherwise
    """
    # Verify ownership first
    existing = await get_assistant(assistant_id, owner_id)

    if not existing:
        return False

    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")

    if assistants_table:
        return await _delete_assistant_cloud(assistant_id, assistants_table)
    else:
        return await _delete_assistant_local(assistant_id)


async def _delete_assistant_local(assistant_id: str) -> bool:
    """
    Delete assistant from local file storage

    Args:
        assistant_id: Assistant identifier

    Returns:
        True if deleted successfully, False otherwise
    """
    assistant_file = get_assistant_path(assistant_id)

    if not assistant_file.exists():
        return False

    try:
        assistant_file.unlink()
        logger.info(f"🗑️ Deleted assistant {assistant_id} from {assistant_file}")
        return True

    except Exception as e:
        logger.error(f"Failed to delete assistant from local file: {e}")
        return False


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


async def share_assistant(assistant_id: str, owner_id: str, emails: List[str]) -> bool:
    """
    Share an assistant with specified email addresses.

    Creates share records in DynamoDB for each email. Emails are normalized to lowercase.
    Only the owner can share their assistant.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (must be the owner)
        emails: List of email addresses to share with

    Returns:
        True if shares were created successfully, False otherwise
    """
    # Verify ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Cannot share assistant {assistant_id}: not found or not owned by {owner_id}")
        return False

    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        logger.error("Cannot share assistant: ASSISTANTS_TABLE_NAME not configured")
        return False

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
                    }
                )
                logger.info(f"Created share record for assistant {assistant_id} with {email}")
            except ClientError as e:
                logger.error(f"Failed to create share record for {email}: {e}")
                # Continue with other emails even if one fails

        return True

    except Exception as e:
        logger.error(f"Error sharing assistant {assistant_id}: {e}", exc_info=True)
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

    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        logger.error("Cannot unshare assistant: ASSISTANTS_TABLE_NAME not configured")
        return False

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


async def list_assistant_shares(assistant_id: str, owner_id: str) -> List[str]:
    """
    List all email addresses an assistant is shared with.

    Only the owner can view the share list.

    Args:
        assistant_id: Assistant identifier
        owner_id: User identifier (must be the owner)

    Returns:
        List of email addresses (lowercase, normalized)
    """
    # Verify ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Cannot list shares for assistant {assistant_id}: not found or not owned by {owner_id}")
        return []

    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        logger.debug("Cannot list shares: ASSISTANTS_TABLE_NAME not configured")
        return []

    try:
        import boto3
        from boto3.dynamodb.conditions import Key
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Query all share records for this assistant
        response = table.query(KeyConditionExpression=Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("SHARE#"))

        emails = []
        for item in response.get("Items", []):
            email = item.get("email")
            if email:
                emails.append(email)

        logger.info(f"Found {len(emails)} shares for assistant {assistant_id}")
        return emails

    except ClientError as e:
        logger.error(f"Error listing shares for assistant {assistant_id}: {e}")
        return []
    except Exception as e:
        logger.error(f"Error listing shares for assistant {assistant_id}: {e}", exc_info=True)
        return []


async def check_share_access(assistant_id: str, user_email: str) -> bool:
    """
    Check if a user has share access to an assistant.

    Args:
        assistant_id: Assistant identifier
        user_email: User's email address (will be normalized to lowercase)

    Returns:
        True if share record exists, False if not found
        
    Raises:
        Exception: On DynamoDB errors (not ResourceNotFoundException)
    """
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        return False

    try:
        import boto3
        from botocore.exceptions import ClientError

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(assistants_table)

        # Normalize email
        normalized_email = user_email.lower().strip()

        # Check if share record exists
        response = table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": f"SHARE#{normalized_email}"})

        return "Item" in response

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        if error_code == 'ResourceNotFoundException':
            logger.debug(f"Table {assistants_table} not found")
            return False
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
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        logger.debug("Cannot mark share as interacted: ASSISTANTS_TABLE_NAME not configured")
        return False

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
    assistants_table = os.environ.get("ASSISTANTS_TABLE_NAME")
    if not assistants_table:
        logger.debug("Cannot list shared assistants: ASSISTANTS_TABLE_NAME not configured")
        return []

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

            if assistant_id:
                # Get the full assistant metadata
                assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, assistants_table)
                if assistant:
                    # Attach share metadata as dynamic attributes
                    assistant.first_interacted = first_interacted
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
