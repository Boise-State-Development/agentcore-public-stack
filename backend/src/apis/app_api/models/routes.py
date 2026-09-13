"""Models API routes

Provides endpoints for users to list available models based on their roles.
Supports both AppRole-based access (preferred) and legacy JWT role-based access.
"""

from fastapi import APIRouter, HTTPException, Depends, Request, Response, status
import logging

from apis.app_api.admin.models import ManagedModelsListResponse
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.models.managed_models import list_all_managed_models
from apis.app_api.admin.services.model_icons import ModelIconError, read_model_icon
from apis.app_api.admin.services.model_access import (
    ModelAccessService,
    get_model_access_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ManagedModelsListResponse)
async def list_models_for_user(
    current_user: User = Depends(get_current_user_from_session),
    model_access_service: ModelAccessService = Depends(get_model_access_service),
):
    """
    List models available to the current user.

    This endpoint returns models filtered by the user's permissions. Only models
    that are:
    1. Enabled, AND
    2. Granted by one of the user's AppRoles — the role lists the model in its
       grantedModels, or grants the '*' wildcard — OR
    3. Available via legacy JWT role matching (availableToRoles)

    will be returned.

    Access Control:
    - The AppRole record is the source of truth. The model's own allowedAppRoles
      field is DERIVED from those roles for display and is not consulted here.
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
        f"User {current_user.name} requesting available models "
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
            f"{current_user.name} (out of {len(all_models)} total)"
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


@router.get("/{model_id}/icon")
async def get_model_icon(
    model_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_from_session),
):
    """Serve a model's uploaded icon bytes.

    User-facing, not admin-facing: this renders in the chat model picker for
    everyone. Signed-in is the whole check — the catalog already hands every user
    this model's name, provider and pricing, so its logo discloses nothing new.

    The object is immutable — its key *is* its content digest — so this answers
    with a one-year ``immutable`` directive and the digest as the ETag. A
    replacement changes ``iconUrl``'s ``?v=``, which is what busts the cache; the
    ``If-None-Match`` 304 below is for the same URL being asked for twice.

    A missing icon is a 404, so the SPA falls through to the model's ``iconSlug``
    (or its provider-name match) rather than rendering a broken tile.
    """
    try:
        data, content_type, version = await read_model_icon(model_id)
    except ModelIconError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error reading model icon: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read model icon: {str(e)}")

    etag = f'"{version}"'
    # `immutable` is only true of the VERSIONED url. `?v=<digest>` names one
    # specific object and can never mean anything else, so a year is right. The
    # bare path tracks whatever the record points at now — promising a year for
    # that pins a replaced or removed icon in every cache that saw it, and the
    # removal simply never becomes visible. Revalidating costs a 304 against the
    # ETag below, which is the same round trip the versioned url avoids anyway.
    if request.query_params.get("v") == version:
        cache_control = "public, max-age=31536000, immutable"
    else:
        cache_control = "no-cache"
    headers = {"Cache-Control": cache_control, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=content_type, headers=headers)
