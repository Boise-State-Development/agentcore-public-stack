"""Models API routes

Provides endpoints for users to list available models based on their roles.
Supports both AppRole-based access (preferred) and legacy JWT role-based access.
"""

from fastapi import APIRouter, HTTPException, Depends, status
import logging

from apis.app_api.admin.models import ManagedModelsListResponse
from apis.shared.auth import User, get_current_user
from apis.app_api.admin.services.managed_models import list_all_managed_models
from apis.app_api.admin.services.model_access import (
    ModelAccessService,
    get_model_access_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ManagedModelsListResponse)
async def list_models_for_user(
    current_user: User = Depends(get_current_user),
    model_access_service: ModelAccessService = Depends(get_model_access_service),
):
    """
    List models available to the current user.

    This endpoint returns models filtered by the user's permissions. Only models
    that are:
    1. Enabled
    2. Accessible via AppRole permissions (allowedAppRoles) OR
    3. Available via legacy JWT role matching (availableToRoles)

    will be returned.

    Access Control:
    - AppRole-based access is checked first (via allowedAppRoles field)
    - Legacy JWT role-based access is checked as fallback (via availableToRoles field)
    - During the transition period, access is granted if EITHER method matches

    Args:
        current_user: Authenticated user (injected by dependency)
        model_access_service: Service for checking model access (injected)

    Returns:
        ManagedModelsListResponse with list of available models

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    logger.info(
        f"User {current_user.email} requesting available models "
        f"(roles: {current_user.roles})"
    )

    try:
        # Get all models, then filter by access
        all_models = await list_all_managed_models()

        # Filter models based on hybrid AppRole + JWT role access
        accessible_models = await model_access_service.filter_accessible_models(
            current_user, all_models
        )

        logger.info(
            f"✅ Found {len(accessible_models)} models available to user "
            f"{current_user.email} (out of {len(all_models)} total)"
        )

        # Convert ManagedModel instances to dicts for Pydantic v2 validation
        models_dict = [model.model_dump(by_alias=True) for model in accessible_models]

        return ManagedModelsListResponse(
            models=models_dict,
            total_count=len(accessible_models),
        )

    except Exception as e:
        logger.error(f"Unexpected error listing models for user: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing models: {str(e)}"
        )
