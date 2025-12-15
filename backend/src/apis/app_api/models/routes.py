"""Models API routes

Provides endpoints for users to list available models based on their roles.
"""

from fastapi import APIRouter, HTTPException, Depends, status
import logging

from apis.app_api.admin.models import ManagedModelsListResponse
from apis.shared.auth import User, get_current_user
from apis.app_api.admin.services.managed_models import list_managed_models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ManagedModelsListResponse)
async def list_models_for_user(
    current_user: User = Depends(get_current_user),
):
    """
    List models available to the current user.

    This endpoint returns models filtered by the user's roles. Only models
    that are:
    1. Enabled
    2. Available to at least one of the user's roles

    will be returned.

    Args:
        current_user: Authenticated user (injected by dependency)

    Returns:
        ManagedModelsListResponse with list of available models

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    logger.info(f"User {current_user.email} requesting available models (roles: {current_user.roles})")

    try:
        # List models filtered by user's roles
        models = await list_managed_models(user_roles=current_user.roles)

        logger.info(f"✅ Found {len(models)} models available to user {current_user.email}")

        # Convert ManagedModel instances to dicts for Pydantic v2 validation
        models_dict = [model.model_dump(by_alias=True) for model in models]

        return ManagedModelsListResponse(
            models=models_dict,
            total_count=len(models),
        )

    except Exception as e:
        logger.error(f"Unexpected error listing models for user: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing models: {str(e)}"
        )
