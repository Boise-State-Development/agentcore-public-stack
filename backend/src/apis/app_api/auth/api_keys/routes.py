"""API Key management routes.

Endpoints:
    POST   /auth/api-keys       — Create a new API key
    GET    /auth/api-keys       — List the caller's API keys
    DELETE /auth/api-keys/{id}  — Revoke an API key
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

from .models import (
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    DeleteApiKeyResponse,
    GetApiKeyResponse,
)
from .service import get_api_key_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/api-keys", tags=["api-keys"])


@router.post(
    "",
    response_model=CreateApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
)
async def create_api_key(
    request: CreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
) -> CreateApiKeyResponse:
    """Generate a new API key for the authenticated user.

    The raw key value is returned only in this response — it cannot be
    retrieved later. If the user already has a key, it is replaced.
    """
    service = get_api_key_service()
    try:
        return await service.create_key(current_user.user_id, request)
    except Exception as e:
        logger.error(f"Failed to create API key for user {current_user.user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create API key",
        )


@router.get(
    "",
    response_model=GetApiKeyResponse,
    summary="Get your API key",
)
async def get_api_key(
    current_user: User = Depends(get_current_user),
) -> GetApiKeyResponse:
    """Return metadata for the authenticated user's API key, if one exists."""
    service = get_api_key_service()
    try:
        key = await service.get_key(current_user.user_id)
        return GetApiKeyResponse(key=key)
    except Exception as e:
        logger.error(f"Failed to get API key for user {current_user.user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get API key",
        )


@router.delete(
    "/{key_id}",
    response_model=DeleteApiKeyResponse,
    summary="Delete an API key",
)
async def delete_api_key(
    key_id: str,
    current_user: User = Depends(get_current_user),
) -> DeleteApiKeyResponse:
    """Revoke an API key. Only the key owner can delete it."""
    service = get_api_key_service()
    try:
        deleted = await service.delete_key(current_user.user_id, key_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found or not owned by you",
            )
        return DeleteApiKeyResponse(key_id=key_id, deleted=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete API key", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete API key",
        )
