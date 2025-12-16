"""Storage service for managed models

This service handles CRUD operations for managed models.
Supports both local file storage and cloud DynamoDB storage.

Architecture:
- Local: Stores models in JSON files under backend/src/managed_models/
- Cloud: Stores models in DynamoDB table specified by MANAGED_MODELS_TABLE_NAME
"""

import logging
import json
import os
import uuid
from typing import List, Optional
from pathlib import Path
from datetime import datetime, timezone

from apis.app_api.admin.models import ManagedModel, ManagedModelCreate, ManagedModelUpdate

logger = logging.getLogger(__name__)


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
    managed_models_table = os.environ.get('MANAGED_MODELS_TABLE_NAME')

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

    TODO: Implement DynamoDB integration
    """
    # TODO: Implement DynamoDB creation
    logger.info(f"Would create managed model in DynamoDB table {table_name}")
    raise NotImplementedError("DynamoDB storage not yet implemented")


async def get_managed_model(model_id: str) -> Optional[ManagedModel]:
    """
    Get an managed model by ID

    Args:
        model_id: Model identifier

    Returns:
        ManagedModel if found, None otherwise
    """
    managed_models_table = os.environ.get('MANAGED_MODELS_TABLE_NAME')

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

    TODO: Implement DynamoDB integration
    """
    logger.info(f"Would get managed model from DynamoDB table {table_name}")
    return None


async def list_managed_models(user_roles: Optional[List[str]] = None) -> List[ManagedModel]:
    """
    List managed models, optionally filtered by user roles

    Args:
        user_roles: List of user roles for filtering (None = admin view, all models)

    Returns:
        List of ManagedModel objects
    """
    managed_models_table = os.environ.get('MANAGED_MODELS_TABLE_NAME')

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

    TODO: Implement DynamoDB integration
    """
    logger.info(f"Would list managed models from DynamoDB table {table_name}")
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
    managed_models_table = os.environ.get('MANAGED_MODELS_TABLE_NAME')

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

    TODO: Implement DynamoDB integration
    """
    logger.info(f"Would update managed model in DynamoDB table {table_name}")
    return None


async def delete_managed_model(model_id: str) -> bool:
    """
    Delete an managed model

    Args:
        model_id: Model identifier

    Returns:
        True if deleted, False if not found
    """
    managed_models_table = os.environ.get('MANAGED_MODELS_TABLE_NAME')

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

    TODO: Implement DynamoDB integration
    """
    logger.info(f"Would delete managed model from DynamoDB table {table_name}")
    return False
