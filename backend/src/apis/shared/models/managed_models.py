"""Storage service for managed models

This service handles CRUD operations for managed models.
Requires DynamoDB storage via DYNAMODB_MANAGED_MODELS_TABLE_NAME.
"""

import logging
import os
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from .models import ManagedModel, ManagedModelCreate, ManagedModelUpdate

logger = logging.getLogger(__name__)


def _resolve_supports_caching(supports_caching: Optional[bool], provider: str) -> bool:
    """
    Resolve the supports_caching value based on explicit setting or provider defaults.

    Args:
        supports_caching: Explicit value from model data (None if not set)
        provider: The model provider (bedrock, openai, gemini)

    Returns:
        bool: Whether the model supports caching
    """
    if supports_caching is not None:
        return supports_caching

    # Default behavior: Only Bedrock models support caching by default
    # Admins can explicitly set this to False for Bedrock models that don't support it
    return provider.lower() == 'bedrock'


def _resolve_mantle_endpoint_path(endpoint_path: Optional[str], provider: str) -> Optional[str]:
    """Resolve the Bedrock Mantle endpoint path for a model.

    Only meaningful for ``provider == 'mantle'`` — Mantle serves different
    models on different OpenAI-compatible paths (``/v1`` vs ``/openai/v1``)
    and exposes no API to discover which, so the value is recorded per model
    (from the model card / curated catalog). Defaults to ``/v1`` for Mantle
    models when unset; ``None`` for every other provider (the field is inert
    there).
    """
    if provider.lower() != 'mantle':
        return None
    return endpoint_path or '/v1'

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')


async def _clear_existing_default_cloud(table_name: str, exclude_id: Optional[str] = None) -> None:
    """
    Clear isDefault flag from all models in DynamoDB except the specified one.

    Args:
        table_name: DynamoDB table name
        exclude_id: Model ID to exclude from clearing (the new default)
    """
    table = dynamodb.Table(table_name)

    try:
        # Scan for all models
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

        # Update any models that have isDefault=True
        for item in items:
            model_id = item.get('id')

            # Skip the excluded model
            if exclude_id and model_id == exclude_id:
                continue

            # If this model is currently default, clear it
            if item.get('isDefault', False):
                table.update_item(
                    Key={
                        'PK': f'MODEL#{model_id}',
                        'SK': f'MODEL#{model_id}'
                    },
                    UpdateExpression='SET #isDefault = :false, #updatedAt = :now',
                    ExpressionAttributeNames={
                        '#isDefault': 'isDefault',
                        '#updatedAt': 'updatedAt'
                    },
                    ExpressionAttributeValues={
                        ':false': False,
                        ':now': datetime.now(timezone.utc).isoformat()
                    }
                )
                logger.info(f"Cleared default flag from model: {item.get('modelName', model_id)}")

    except ClientError as e:
        logger.error(f"Failed to clear existing default in DynamoDB: {e}")
        raise


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
    if not managed_models_table:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    return await _create_managed_model_cloud(model_data, managed_models_table)


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

    # If setting as default, clear any existing default first
    if model_data.is_default:
        await _clear_existing_default_cloud(table_name)

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
        allowed_app_roles=model_data.allowed_app_roles,
        available_to_roles=model_data.available_to_roles,
        enabled=model_data.enabled,
        input_price_per_million_tokens=model_data.input_price_per_million_tokens,
        output_price_per_million_tokens=model_data.output_price_per_million_tokens,
        cache_write_price_per_million_tokens=model_data.cache_write_price_per_million_tokens,
        cache_read_price_per_million_tokens=model_data.cache_read_price_per_million_tokens,
        knowledge_cutoff_date=model_data.knowledge_cutoff_date,
        supports_caching=_resolve_supports_caching(model_data.supports_caching, model_data.provider),
        is_default=model_data.is_default,
        mantle_endpoint_path=_resolve_mantle_endpoint_path(model_data.mantle_endpoint_path, model_data.provider),
        supported_params=model_data.supported_params,
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
        'allowedAppRoles': model_data.allowed_app_roles,
        'availableToRoles': model_data.available_to_roles,
        'enabled': model_data.enabled,
        'inputPricePerMillionTokens': model_data.input_price_per_million_tokens,
        'outputPricePerMillionTokens': model_data.output_price_per_million_tokens,
        'supportsCaching': _resolve_supports_caching(model_data.supports_caching, model_data.provider),
        'isDefault': model_data.is_default,
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
    resolved_mantle_path = _resolve_mantle_endpoint_path(model_data.mantle_endpoint_path, model_data.provider)
    if resolved_mantle_path is not None:
        item['mantleEndpointPath'] = resolved_mantle_path
    if model_data.supported_params is not None:
        item['supportedParams'] = model_data.supported_params.model_dump(by_alias=True, exclude_none=True)

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
    if not managed_models_table:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    return await _get_managed_model_cloud(model_id, managed_models_table)


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
    List managed models, optionally filtered by user roles (legacy JWT role check).

    NOTE: This function uses legacy JWT role filtering. For new code, prefer using
    ModelAccessService.filter_accessible_models() which supports both AppRoles
    and legacy JWT roles.

    Args:
        user_roles: List of user JWT roles for filtering (None = admin view, all models)

    Returns:
        List of ManagedModel objects
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')
    if not managed_models_table:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    models = await _list_managed_models_cloud(managed_models_table)

    # Filter by user roles if provided (legacy JWT role check only)
    # For hybrid AppRole + JWT role filtering, use ModelAccessService
    if user_roles is not None:
        models = [
            model for model in models
            if model.enabled and any(role in model.available_to_roles for role in user_roles)
        ]

    return models


async def list_all_managed_models() -> List[ManagedModel]:
    """
    List all managed models without any filtering.

    This is the base function for admin views and for use with
    ModelAccessService which handles access filtering separately.

    Returns:
        List of all ManagedModel objects
    """
    managed_models_table = os.environ.get('DYNAMODB_MANAGED_MODELS_TABLE_NAME')
    if not managed_models_table:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    return await _list_managed_models_cloud(managed_models_table)


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
                # JUSTIFICATION: When listing models from DynamoDB, individual model parsing
                # failures should not break the entire list operation. We skip corrupted models
                # and continue processing others. This provides better UX than failing completely.
                logger.warning(f"Failed to parse model from DynamoDB: {e}")
                continue

        # Sort by creation date (newest first)
        models.sort(key=lambda x: x.created_at, reverse=True)

        logger.info(f"Found {len(models)} managed models in DynamoDB")
        return models

    except ClientError as e:
        logger.error(f"Failed to list managed models from DynamoDB: {e}", exc_info=True)
        # Propagate error - listing failures should be visible to the user
        from fastapi import HTTPException
        from apis.shared.errors import ErrorCode, create_error_response
        raise HTTPException(
            status_code=503,
            detail=create_error_response(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message="Failed to list managed models from database",
                detail=str(e)
            )
        )


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
    if not managed_models_table:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    return await _update_managed_model_cloud(model_id, updates, managed_models_table)


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

    # If setting as default, clear any existing default first (exclude this model)
    if update_data.get('isDefault', False):
        await _clear_existing_default_cloud(table_name, exclude_id=model_id)

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
    if not managed_models_table:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    return await _delete_managed_model_cloud(model_id, managed_models_table)


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
            return False  # Model not found - this is expected
        logger.error(f"Failed to delete managed model from DynamoDB: {e}", exc_info=True)
        # Propagate error - delete failures should be visible to the user
        from fastapi import HTTPException
        from apis.shared.errors import ErrorCode, create_error_response
        raise HTTPException(
            status_code=503,
            detail=create_error_response(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message="Failed to delete managed model from database",
                detail=str(e)
            )
        )
