"""Assistant service layer

This service handles storing and retrieving assistant data.
It supports both local file storage and cloud DynamoDB storage.

Architecture:
- Local: Stores assistants as individual JSON files in backend/src/assistants/
- Cloud: Stores assistants in DynamoDB table specified by ASSISTANTS_TABLE_NAME
"""

import logging
import json
import os
import base64
import uuid
from typing import Optional, Tuple, List
from pathlib import Path
from datetime import datetime

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
    vector_index_id = os.environ.get('ASSISTANTS_VECTOR_STORE_INDEX_NAME', 'assistants-index')
    
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
        status="DRAFT"
    )
    
    # Store the draft assistant
    assistants_table = os.environ.get('ASSISTANTS_TABLE_NAME')
    
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
    tags: Optional[List[str]] = None
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
        vector_index_id = os.environ.get('ASSISTANTS_VECTOR_STORE_INDEX_NAME')
    
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
        status="COMPLETE"
    )
    
    # Store the assistant
    assistants_table = os.environ.get('ASSISTANTS_TABLE_NAME')
    
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
        with open(assistant_file, 'w') as f:
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
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        item = assistant.model_dump(by_alias=True, exclude_none=True)
        item['PK'] = f'AST#{assistant.assistant_id}'
        item['SK'] = 'METADATA'
        
        # Add GSI keys for owner listings
        item['GSI_PK'] = f'OWNER#{assistant.owner_id}'
        item['GSI_SK'] = f'STATUS#{assistant.status}#CREATED#{assistant.created_at}'
        
        table.put_item(Item=item)
        
        logger.info(f"💾 Stored assistant {assistant.assistant_id} in DynamoDB table {table_name}")
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
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
    assistants_table = os.environ.get('ASSISTANTS_TABLE_NAME')
    
    if assistants_table:
        return await _get_assistant_cloud(assistant_id, owner_id, assistants_table)
    else:
        return await _get_assistant_local(assistant_id, owner_id)


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
        with open(assistant_file, 'r') as f:
            data = json.load(f)
        
        # Verify ownership
        if data.get('ownerId') != owner_id:
            logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
            return None
        
        return Assistant.model_validate(data)
    
    except Exception as e:
        logger.error(f"Failed to read assistant from local file: {e}")
        return None


async def _get_assistant_cloud(
    assistant_id: str,
    owner_id: str,
    table_name: str
) -> Optional[Assistant]:
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
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        response = table.get_item(
            Key={
                'PK': f'AST#{assistant_id}',
                'SK': 'METADATA'
            }
        )
        
        if 'Item' not in response:
            logger.info(f"Assistant {assistant_id} not found in DynamoDB")
            return None
        
        item = response['Item']
        
        # Verify ownership
        if item.get('ownerId') != owner_id:
            logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
            return None
        
        return Assistant.model_validate(item)
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'ResourceNotFoundException':
            logger.info(f"Table {table_name} not found")
        else:
            logger.error(f"Failed to retrieve assistant from DynamoDB: {error_code} - {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to retrieve assistant from DynamoDB: {e}", exc_info=True)
        return None


async def update_assistant(
    assistant_id: str,
    owner_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    visibility: Optional[str] = None,
    tags: Optional[List[str]] = None,
    status: Optional[str] = None
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
        updates['name'] = name
    if description is not None:
        updates['description'] = description
    if instructions is not None:
        updates['instructions'] = instructions
    if visibility is not None:
        updates['visibility'] = visibility
    if tags is not None:
        updates['tags'] = tags
    if status is not None:
        updates['status'] = status
    
    # Always update the updated_at timestamp
    updates['updated_at'] = _get_current_timestamp()
    
    # Create updated assistant (merge with existing)
    existing_dict = existing.model_dump(by_alias=False)
    existing_dict.update(updates)
    
    updated_assistant = Assistant.model_validate(existing_dict)
    
    # Store updated assistant
    assistants_table = os.environ.get('ASSISTANTS_TABLE_NAME')
    
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
        with open(assistant_file, 'w') as f:
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
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        # Get existing assistant to check if status changed
        existing_response = table.get_item(
            Key={
                'PK': f'AST#{assistant.assistant_id}',
                'SK': 'METADATA'
            }
        )
        
        if 'Item' not in existing_response:
            raise ValueError(f"Assistant {assistant.assistant_id} not found")
        
        existing_item = existing_response['Item']
        old_status = existing_item.get('status')
        status_changed = old_status != assistant.status
        
        # Build update expression
        update_parts = []
        expression_attribute_values = {}
        expression_attribute_names = {}
        
        # Fields that should never be updated (immutable or composite keys)
        immutable_fields = {'PK', 'SK', 'GSI_PK', 'GSI_SK', 'assistantId', 'createdAt', 'ownerId'}
        
        # Always update updatedAt
        update_parts.append("updatedAt = :updated_at")
        expression_attribute_values[":updated_at"] = assistant.updated_at
        
        # Update all fields from assistant model (excluding immutable fields)
        assistant_dict = assistant.model_dump(by_alias=True, exclude_none=True)
        
        # DynamoDB reserved keywords that need to be escaped
        reserved_keywords = {'status', 'name', 'data', 'size', 'type', 'value'}
        
        for key, value in assistant_dict.items():
            # Skip immutable fields and composite keys
            if key in immutable_fields:
                continue
            
            # Skip updatedAt since we're already adding it explicitly above
            if key == 'updatedAt':
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
        if status_changed:
            update_parts.append("GSI_SK = :gsi_sk")
            expression_attribute_values[":gsi_sk"] = f'STATUS#{assistant.status}#CREATED#{assistant.created_at}'
        
        update_expression = "SET " + ", ".join(update_parts)
        
        update_params = {
            'Key': {
                'PK': f'AST#{assistant.assistant_id}',
                'SK': 'METADATA'
            },
            'UpdateExpression': update_expression,
            'ExpressionAttributeValues': expression_attribute_values,
            'ReturnValues': 'NONE'
        }
        
        if expression_attribute_names:
            update_params['ExpressionAttributeNames'] = expression_attribute_names
        
        table.update_item(**update_params)
        
        logger.info(f"💾 Updated assistant {assistant.assistant_id} in DynamoDB table {table_name}")
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
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
    include_drafts: bool = False
) -> Tuple[List[Assistant], Optional[str]]:
    """
    List assistants for a user with pagination support
    
    Args:
        owner_id: User identifier
        limit: Maximum number of assistants to return (optional)
        next_token: Pagination token for retrieving next page (optional)
        include_archived: Whether to include archived assistants
        include_drafts: Whether to include draft assistants
    
    Returns:
        Tuple of (list of Assistant objects, next_token if more assistants exist)
        Assistants are sorted by created_at descending (most recent first)
    """
    assistants_table = os.environ.get('ASSISTANTS_TABLE_NAME')
    
    if assistants_table:
        return await _list_user_assistants_cloud(
            owner_id, table_name=assistants_table, limit=limit,
            next_token=next_token, include_archived=include_archived,
            include_drafts=include_drafts
        )
    else:
        return await _list_user_assistants_local(
            owner_id, limit=limit, next_token=next_token,
            include_archived=include_archived, include_drafts=include_drafts
        )


def _apply_pagination(
    assistants: List[Assistant],
    limit: Optional[int] = None,
    next_token: Optional[str] = None
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
            decoded = base64.b64decode(next_token).decode('utf-8')
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
            next_token = base64.b64encode(last_assistant.created_at.encode('utf-8')).decode('utf-8')
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
    include_drafts: bool = False
) -> Tuple[List[Assistant], Optional[str]]:
    """
    List assistants for a user from local file storage with pagination
    
    Args:
        owner_id: User identifier
        limit: Maximum number of assistants to return (optional)
        next_token: Pagination token (optional)
        include_archived: Whether to include archived assistants
        include_drafts: Whether to include draft assistants
    
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
                with open(assistant_file, 'r') as f:
                    data = json.load(f)
                
                # Filter by owner_id
                if data.get('ownerId') != owner_id:
                    continue
                
                # Parse assistant
                assistant = Assistant.model_validate(data)
                
                # Filter by status
                if not include_archived and assistant.status == 'ARCHIVED':
                    continue
                if not include_drafts and assistant.status == 'DRAFT':
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
    include_drafts: bool = False
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
    
    Returns:
        Tuple of (list of Assistant objects, next_token if more exist)
    """
    try:
        import boto3
        from boto3.dynamodb.conditions import Key
        from botocore.exceptions import ClientError
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        # Build filter expression for status
        filter_parts = []
        expression_attribute_values = {}
        
        if not include_archived:
            filter_parts.append("#status <> :archived")
            expression_attribute_values[":archived"] = 'ARCHIVED'
        if not include_drafts:
            filter_parts.append("#status <> :draft")
            expression_attribute_values[":draft"] = 'DRAFT'
        
        # Decode next_token for ExclusiveStartKey
        exclusive_start_key = None
        if next_token:
            try:
                decoded = base64.b64decode(next_token).decode('utf-8')
                exclusive_start_key = json.loads(decoded)
            except Exception as e:
                logger.warning(f"Invalid next_token: {e}, ignoring pagination")
        
        query_params = {
            'IndexName': 'OwnerStatusIndex',
            'KeyConditionExpression': Key('GSI_PK').eq(f'OWNER#{owner_id}'),
            'ScanIndexForward': False,  # Descending order (most recent first)
        }
        
        if limit and limit > 0:
            query_params['Limit'] = limit
        
        if filter_parts:
            query_params['FilterExpression'] = ' AND '.join(filter_parts)
            query_params['ExpressionAttributeNames'] = {'#status': 'status'}
            query_params['ExpressionAttributeValues'] = expression_attribute_values
        
        if exclusive_start_key:
            query_params['ExclusiveStartKey'] = exclusive_start_key
        
        response = table.query(**query_params)
        
        assistants = []
        for item in response.get('Items', []):
            try:
                assistants.append(Assistant.model_validate(item))
            except Exception as e:
                logger.warning(f"Failed to parse assistant item: {e}")
                continue
        
        # Generate next_token from LastEvaluatedKey
        next_page_token = None
        if 'LastEvaluatedKey' in response:
            encoded = json.dumps(response['LastEvaluatedKey'])
            next_page_token = base64.b64encode(encoded.encode('utf-8')).decode('utf-8')
        
        logger.info(f"Listed {len(assistants)} assistants for user {owner_id}")
        return assistants, next_page_token
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
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
    return await update_assistant(
        assistant_id=assistant_id,
        owner_id=owner_id,
        status='ARCHIVED'
    )


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
    
    assistants_table = os.environ.get('ASSISTANTS_TABLE_NAME')
    
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
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        table.delete_item(
            Key={
                'PK': f'AST#{assistant_id}',
                'SK': 'METADATA'
            }
        )
        
        logger.info(f"🗑️ Deleted assistant {assistant_id} from DynamoDB table {table_name}")
        return True
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'ResourceNotFoundException':
            logger.warning(f"Assistant {assistant_id} not found in DynamoDB")
        else:
            logger.error(f"Failed to delete assistant from DynamoDB: {error_code} - {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to delete assistant from DynamoDB: {e}")
        return False

