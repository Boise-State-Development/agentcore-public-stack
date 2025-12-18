"""Storage service for managed models

This service handles CRUD operations for managed models.
Supports both local file storage and cloud DynamoDB storage.

Architecture:
- Local: Stores models in JSON files under backend/src/managed_models/
- Cloud: Stores models in DynamoDB table specified by DYNAMODB_MANAGED_MODELS_TABLE_NAME
"""

import logging
import json
import os
import uuid
from typing import List, Optional
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from apis.app_api.admin.models import ManagedModel, ManagedModelCreate, ManagedModelUpdate

logger = logging.getLogger(__name__)

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')


def _python_to_dynamodb(obj):
    """
    Convert Python objects to DynamoDB-compatible format.
    Converts floats to Decimal for DynamoDB storage.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _python_to_dynamodb(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_python_to_dynamodb(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _dynamodb_to_python(obj):
    """
    Convert DynamoDB objects to Python format.
    Converts Decimal to float for JSON serialization.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _dynamodb_to_python(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_dynamodb_to_python(item) for item in obj]
    return obj


def get_managed_models_dir() -> Path:
    """
    Get the directory for managed models storage

    Returns:
        Path: Directory for managed models
    """
    models_dir = os.environ.get('MANAGED_MODELS_DIR')
    if models_dir:
        return Path(models_dir)

    # Default: backend/src/managed_models
    # Navigate from this file: admin/services/managed_models.py -> admin -> app_api -> apis -> src -> managed_models
    return Path(__file__).parent.parent.parent.parent.parent / "managed_models"


def get_model_path(model_id: str) -> Path:
    """
    Get the file path for a specific model

    Args:
        model_id: Model identifier

    Returns:
        Path: Full path to the model file
    """
    models_dir = get_managed_models_dir()
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir / f"{model_id}.json"


async def create_managed_model(model_data: ManagedModelCreate) -> ManagedModel:
    """
    Create a new managed model

    Args:
        model_data: Model creation data

    Returns:
        ManagedModel: Created model with ID and timestamps

    Raises:
        ValueError: If a model with the same modelId already exists
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')

    if managed_models_table:
        return await _create_managed_model_cloud(model_data, managed_models_table)
    else:
        return await _create_managed_model_local(model_data)


async def _create_managed_model_local(model_data: ManagedModelCreate) -> ManagedModel:
    """
    Create a new managed model in local file storage

    Args:
        model_data: Model creation data

    Returns:
        ManagedModel: Created model

    Raises:
        ValueError: If a model with the same modelId already exists
    """
    # Check if model already exists
    existing_models = await _list_managed_models_local()
    for model in existing_models:
        if model.model_id == model_data.model_id:
            raise ValueError(f"Model with modelId '{model_data.model_id}' already exists")

    # Generate unique ID
    model_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Create model object
    model = ManagedModel(
        id=model_id,
        model_id=model_data.model_id,
        model_name=model_data.model_name,
        provider=model_data.provider,
        provider_name=model_data.provider_name,
        input_modalities=model_data.input_modalities,
        output_modalities=model_data.output_modalities,
        max_input_tokens=model_data.max_input_tokens,
        max_output_tokens=model_data.max_output_tokens,
        available_to_roles=model_data.available_to_roles,
        enabled=model_data.enabled,
        input_price_per_million_tokens=model_data.input_price_per_million_tokens,
        output_price_per_million_tokens=model_data.output_price_per_million_tokens,
        cache_write_price_per_million_tokens=model_data.cache_write_price_per_million_tokens,
        cache_read_price_per_million_tokens=model_data.cache_read_price_per_million_tokens,
        is_reasoning_model=model_data.is_reasoning_model,
        knowledge_cutoff_date=model_data.knowledge_cutoff_date,
        created_at=now,
        updated_at=now,
    )

    # Save to file
    model_file = get_model_path(model_id)
    model_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(model_file, 'w') as f:
            json.dump(model.model_dump(by_alias=True), f, indent=2, default=str)

        logger.info(f"💾 Created managed model: {model.model_name} (ID: {model_id})")
        return model

    except Exception as e:
        logger.error(f"Failed to create managed model in local storage: {e}")
        raise


async def _create_managed_model_cloud(model_data: ManagedModelCreate, table_name: str) -> ManagedModel:
    """
    Create a new managed model in DynamoDB

    Args:
        model_data: Model creation data
        table_name: DynamoDB table name

    Returns:
        ManagedModel: Created model

    Raises:
        ValueError: If a model with the same modelId already exists
    """
    table = dynamodb.Table(table_name)

    # Check if model with same modelId already exists using GSI
    try:
        response = table.query(
            IndexName='ModelIdIndex',
            KeyConditionExpression='GSI1PK = :gsi1pk',
            ExpressionAttributeValues={
                ':gsi1pk': f'MODEL#{model_data.model_id}'
            },
            Limit=1
        )

        if response.get('Items'):
            raise ValueError(f"Model with modelId '{model_data.model_id}' already exists")

    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            logger.error(f"Error checking for existing model: {e}")
            raise

    # Generate unique ID and timestamps
    model_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Create model object
    model = ManagedModel(
        id=model_id,
        model_id=model_data.model_id,
        model_name=model_data.model_name,
        provider=model_data.provider,
        provider_name=model_data.provider_name,
        input_modalities=model_data.input_modalities,
        output_modalities=model_data.output_modalities,
        max_input_tokens=model_data.max_input_tokens,
        max_output_tokens=model_data.max_output_tokens,
        available_to_roles=model_data.available_to_roles,
        enabled=model_data.enabled,
        input_price_per_million_tokens=model_data.input_price_per_million_tokens,
        output_price_per_million_tokens=model_data.output_price_per_million_tokens,
        cache_write_price_per_million_tokens=model_data.cache_write_price_per_million_tokens,
        cache_read_price_per_million_tokens=model_data.cache_read_price_per_million_tokens,
        is_reasoning_model=model_data.is_reasoning_model,
        knowledge_cutoff_date=model_data.knowledge_cutoff_date,
        created_at=now,
        updated_at=now,
    )

    # Prepare DynamoDB item
    item = {
        'PK': f'MODEL#{model_id}',
        'SK': f'MODEL#{model_id}',
        'GSI1PK': f'MODEL#{model_data.model_id}',
        'GSI1SK': f'MODEL#{model_id}',
        'id': model_id,
        'modelId': model_data.model_id,
        'modelName': model_data.model_name,
        'provider': model_data.provider,
        'providerName': model_data.provider_name,
        'inputModalities': model_data.input_modalities,
        'outputModalities': model_data.output_modalities,
        'maxInputTokens': model_data.max_input_tokens,
        'maxOutputTokens': model_data.max_output_tokens,
        'availableToRoles': model_data.available_to_roles,
        'enabled': model_data.enabled,
        'inputPricePerMillionTokens': model_data.input_price_per_million_tokens,
        'outputPricePerMillionTokens': model_data.output_price_per_million_tokens,
        'isReasoningModel': model_data.is_reasoning_model,
        'createdAt': now.isoformat(),
        'updatedAt': now.isoformat(),
    }

    # Add optional fields
    if model_data.cache_write_price_per_million_tokens is not None:
        item['cacheWritePricePerMillionTokens'] = model_data.cache_write_price_per_million_tokens
    if model_data.cache_read_price_per_million_tokens is not None:
        item['cacheReadPricePerMillionTokens'] = model_data.cache_read_price_per_million_tokens
    if model_data.knowledge_cutoff_date is not None:
        item['knowledgeCutoffDate'] = model_data.knowledge_cutoff_date

    # Convert floats to Decimal for DynamoDB
    item = _python_to_dynamodb(item)

    try:
        # Put item with condition to prevent overwrites
        table.put_item(
            Item=item,
            ConditionExpression='attribute_not_exists(PK)'
        )

        logger.info(f"💾 Created managed model in DynamoDB: {model.model_name} (ID: {model_id})")
        return model

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ValueError(f"Model with ID '{model_id}' already exists")
        logger.error(f"Failed to create managed model in DynamoDB: {e}")
        raise


async def get_managed_model(model_id: str) -> Optional[ManagedModel]:
    """
    Get an managed model by ID

    Args:
        model_id: Model identifier

    Returns:
        ManagedModel if found, None otherwise
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')

    if managed_models_table:
        return await _get_managed_model_cloud(model_id, managed_models_table)
    else:
        return await _get_managed_model_local(model_id)


async def _get_managed_model_local(model_id: str) -> Optional[ManagedModel]:
    """
    Get an managed model from local file storage

    Args:
        model_id: Model identifier

    Returns:
        ManagedModel if found, None otherwise
    """
    model_file = get_model_path(model_id)

    if not model_file.exists():
        return None

    try:
        with open(model_file, 'r') as f:
            data = json.load(f)

        return ManagedModel.model_validate(data)

    except Exception as e:
        logger.error(f"Failed to read managed model from local storage: {e}")
        return None


async def _get_managed_model_cloud(model_id: str, table_name: str) -> Optional[ManagedModel]:
    """
    Get an managed model from DynamoDB

    Args:
        model_id: Model identifier
        table_name: DynamoDB table name

    Returns:
        ManagedModel if found, None otherwise
    """
    table = dynamodb.Table(table_name)

    try:
        response = table.get_item(
            Key={
                'PK': f'MODEL#{model_id}',
                'SK': f'MODEL#{model_id}'
            }
        )

        item = response.get('Item')
        if not item:
            return None

        # Convert DynamoDB Decimal to Python float
        item = _dynamodb_to_python(item)

        # Remove DynamoDB-specific keys
        item.pop('PK', None)
        item.pop('SK', None)
        item.pop('GSI1PK', None)
        item.pop('GSI1SK', None)

        return ManagedModel.model_validate(item)

    except ClientError as e:
        logger.error(f"Failed to get managed model from DynamoDB: {e}")
        return None


async def list_managed_models(user_roles: Optional[List[str]] = None) -> List[ManagedModel]:
    """
    List managed models, optionally filtered by user roles

    Args:
        user_roles: List of user roles for filtering (None = admin view, all models)

    Returns:
        List of ManagedModel objects
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')

    if managed_models_table:
        models = await _list_managed_models_cloud(managed_models_table)
    else:
        models = await _list_managed_models_local()

    # Filter by user roles if provided
    if user_roles is not None:
        models = [
            model for model in models
            if model.enabled and any(role in model.available_to_roles for role in user_roles)
        ]

    return models


async def _list_managed_models_local() -> List[ManagedModel]:
    """
    List all managed models from local file storage

    Returns:
        List of ManagedModel objects
    """
    models_dir = get_managed_models_dir()

    if not models_dir.exists():
        logger.info(f"Managed models directory does not exist: {models_dir}")
        return []

    models = []

    try:
        for model_file in models_dir.glob("*.json"):
            try:
                with open(model_file, 'r') as f:
                    data = json.load(f)

                model = ManagedModel.model_validate(data)
                models.append(model)

            except Exception as e:
                logger.warning(f"Failed to read model from {model_file}: {e}")
                continue

        # Sort by creation date (newest first)
        models.sort(key=lambda x: x.created_at, reverse=True)

        logger.info(f"Found {len(models)} managed models in local storage")
        return models

    except Exception as e:
        logger.error(f"Failed to list managed models from local storage: {e}")
        return []


async def _list_managed_models_cloud(table_name: str) -> List[ManagedModel]:
    """
    List all managed models from DynamoDB

    Args:
        table_name: DynamoDB table name

    Returns:
        List of ManagedModel objects
    """
    table = dynamodb.Table(table_name)
    models = []

    try:
        # Scan table for all models (PK starts with MODEL#)
        response = table.scan(
            FilterExpression='begins_with(PK, :pk_prefix)',
            ExpressionAttributeValues={
                ':pk_prefix': 'MODEL#'
            }
        )

        items = response.get('Items', [])

        # Handle pagination
        while 'LastEvaluatedKey' in response:
            response = table.scan(
                FilterExpression='begins_with(PK, :pk_prefix)',
                ExpressionAttributeValues={
                    ':pk_prefix': 'MODEL#'
                },
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            items.extend(response.get('Items', []))

        # Convert items to ManagedModel objects
        for item in items:
            try:
                # Convert DynamoDB Decimal to Python float
                item = _dynamodb_to_python(item)

                # Remove DynamoDB-specific keys
                item.pop('PK', None)
                item.pop('SK', None)
                item.pop('GSI1PK', None)
                item.pop('GSI1SK', None)

                model = ManagedModel.model_validate(item)
                models.append(model)

            except Exception as e:
                logger.warning(f"Failed to parse model from DynamoDB: {e}")
                continue

        # Sort by creation date (newest first)
        models.sort(key=lambda x: x.created_at, reverse=True)

        logger.info(f"Found {len(models)} managed models in DynamoDB")
        return models

    except ClientError as e:
        logger.error(f"Failed to list managed models from DynamoDB: {e}")
        return []


async def update_managed_model(model_id: str, updates: ManagedModelUpdate) -> Optional[ManagedModel]:
    """
    Update an managed model

    Args:
        model_id: Model identifier
        updates: Fields to update

    Returns:
        Updated ManagedModel if found, None otherwise
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')

    if managed_models_table:
        return await _update_managed_model_cloud(model_id, updates, managed_models_table)
    else:
        return await _update_managed_model_local(model_id, updates)


async def _update_managed_model_local(model_id: str, updates: ManagedModelUpdate) -> Optional[ManagedModel]:
    """
    Update an managed model in local file storage

    Args:
        model_id: Model identifier
        updates: Fields to update

    Returns:
        Updated ManagedModel if found, None otherwise

    Raises:
        ValueError: If updating modelId to a value that already exists for another model
    """
    model = await _get_managed_model_local(model_id)

    if not model:
        return None

    # Apply updates
    update_data = updates.model_dump(exclude_none=True, by_alias=True)
    
    # Check if modelId is being updated and if it conflicts with another model
    if 'modelId' in update_data:
        new_model_id = update_data['modelId']
        # Only check for duplicates if the modelId is actually changing
        if new_model_id != model.model_id:
            existing_models = await _list_managed_models_local()
            for existing_model in existing_models:
                # Skip the current model being updated
                if existing_model.id != model_id and existing_model.model_id == new_model_id:
                    raise ValueError(f"Model with modelId '{new_model_id}' already exists")

    model_dict = model.model_dump(by_alias=True)

    # Update fields
    for key, value in update_data.items():
        model_dict[key] = value

    # Update timestamp
    model_dict['updatedAt'] = datetime.now(timezone.utc).isoformat()

    # Save back to file
    model_file = get_model_path(model_id)

    try:
        with open(model_file, 'w') as f:
            json.dump(model_dict, f, indent=2, default=str)

        # Return updated model
        updated_model = ManagedModel.model_validate(model_dict)
        logger.info(f"💾 Updated managed model: {updated_model.model_name} (ID: {model_id})")
        return updated_model

    except ValueError:
        # Re-raise ValueError (duplicate modelId)
        raise
    except Exception as e:
        logger.error(f"Failed to update managed model in local storage: {e}")
        return None


async def _update_managed_model_cloud(model_id: str, updates: ManagedModelUpdate, table_name: str) -> Optional[ManagedModel]:
    """
    Update an managed model in DynamoDB

    Args:
        model_id: Model identifier
        updates: Fields to update
        table_name: DynamoDB table name

    Returns:
        Updated ManagedModel if found, None otherwise

    Raises:
        ValueError: If updating modelId to a value that already exists for another model
    """
    table = dynamodb.Table(table_name)

    # Get the existing model first
    existing_model = await _get_managed_model_cloud(model_id, table_name)
    if not existing_model:
        return None

    # Get update data
    update_data = updates.model_dump(exclude_none=True, by_alias=True)

    if not update_data:
        return existing_model  # No updates to apply

    # Check if modelId is being updated and if it conflicts with another model
    if 'modelId' in update_data:
        new_model_id = update_data['modelId']
        if new_model_id != existing_model.model_id:
            # Check for duplicates using GSI
            try:
                response = table.query(
                    IndexName='ModelIdIndex',
                    KeyConditionExpression='GSI1PK = :gsi1pk',
                    ExpressionAttributeValues={
                        ':gsi1pk': f'MODEL#{new_model_id}'
                    },
                    Limit=1
                )

                # Check if the found item is a different model
                items = response.get('Items', [])
                for item in items:
                    if item.get('id') != model_id:
                        raise ValueError(f"Model with modelId '{new_model_id}' already exists")

            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    logger.error(f"Error checking for existing model: {e}")
                    raise

    # Build update expression
    update_expression_parts = []
    expression_attribute_names = {}
    expression_attribute_values = {}

    # Add updatedAt timestamp
    update_data['updatedAt'] = datetime.now(timezone.utc).isoformat()

    # Track if we need to update GSI keys
    update_gsi = 'modelId' in update_data and update_data['modelId'] != existing_model.model_id

    for key, value in update_data.items():
        attr_name = f"#{key}"
        attr_value = f":{key}"
        update_expression_parts.append(f"{attr_name} = {attr_value}")
        expression_attribute_names[attr_name] = key
        expression_attribute_values[attr_value] = _python_to_dynamodb(value)

    # Update GSI keys if modelId changed
    if update_gsi:
        new_model_id = update_data['modelId']
        expression_attribute_names['#GSI1PK'] = 'GSI1PK'
        expression_attribute_values[':GSI1PK'] = f'MODEL#{new_model_id}'
        update_expression_parts.append('#GSI1PK = :GSI1PK')

    update_expression = "SET " + ", ".join(update_expression_parts)

    try:
        response = table.update_item(
            Key={
                'PK': f'MODEL#{model_id}',
                'SK': f'MODEL#{model_id}'
            },
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values,
            ReturnValues='ALL_NEW',
            ConditionExpression='attribute_exists(PK)'
        )

        # Convert response to ManagedModel
        item = response.get('Attributes')
        if not item:
            return None

        # Convert DynamoDB Decimal to Python float
        item = _dynamodb_to_python(item)

        # Remove DynamoDB-specific keys
        item.pop('PK', None)
        item.pop('SK', None)
        item.pop('GSI1PK', None)
        item.pop('GSI1SK', None)

        updated_model = ManagedModel.model_validate(item)
        logger.info(f"💾 Updated managed model in DynamoDB: {updated_model.model_name} (ID: {model_id})")
        return updated_model

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return None  # Model not found
        logger.error(f"Failed to update managed model in DynamoDB: {e}")
        raise


async def delete_managed_model(model_id: str) -> bool:
    """
    Delete an managed model

    Args:
        model_id: Model identifier

    Returns:
        True if deleted, False if not found
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')

    if managed_models_table:
        return await _delete_managed_model_cloud(model_id, managed_models_table)
    else:
        return await _delete_managed_model_local(model_id)


async def _delete_managed_model_local(model_id: str) -> bool:
    """
    Delete an managed model from local file storage

    Args:
        model_id: Model identifier

    Returns:
        True if deleted, False if not found
    """
    model_file = get_model_path(model_id)

    if not model_file.exists():
        return False

    try:
        model_file.unlink()
        logger.info(f"🗑️  Deleted managed model: {model_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to delete managed model from local storage: {e}")
        return False


async def _delete_managed_model_cloud(model_id: str, table_name: str) -> bool:
    """
    Delete an managed model from DynamoDB

    Args:
        model_id: Model identifier
        table_name: DynamoDB table name

    Returns:
        True if deleted, False if not found
    """
    table = dynamodb.Table(table_name)

    try:
        response = table.delete_item(
            Key={
                'PK': f'MODEL#{model_id}',
                'SK': f'MODEL#{model_id}'
            },
            ReturnValues='ALL_OLD',
            ConditionExpression='attribute_exists(PK)'
        )

        # Check if item was actually deleted
        if response.get('Attributes'):
            logger.info(f"🗑️  Deleted managed model from DynamoDB: {model_id}")
            return True
        return False

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False  # Model not found
        logger.error(f"Failed to delete managed model from DynamoDB: {e}")
        return False
