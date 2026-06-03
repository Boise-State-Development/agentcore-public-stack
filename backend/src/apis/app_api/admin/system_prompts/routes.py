"""Admin API routes for system prompt management.

All endpoints require admin role. Non-admin users use the public
``GET /system-prompts`` endpoint which returns only enabled prompts
and strips the prompt_text field.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User, require_admin
from apis.shared.system_prompts.models import (
    SystemPromptAdminListResponse,
    SystemPromptAdminResponse,
    SystemPromptCreate,
    SystemPromptUpdate,
)
from apis.shared.system_prompts.service import get_system_prompts_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system-prompts", tags=["admin-system-prompts"])


@router.get(
    "/",
    response_model=SystemPromptAdminListResponse,
    summary="List all system prompts",
)
async def list_system_prompts(
    enabled_only: bool = Query(False, description="Filter to enabled prompts only"),
    admin_user: User = Depends(require_admin),
) -> SystemPromptAdminListResponse:
    """List all system prompts. Admin sees both enabled and disabled."""
    service = get_system_prompts_service()
    prompts = await service.list_prompts(enabled_only=enabled_only)
    return SystemPromptAdminListResponse(
        prompts=[SystemPromptAdminResponse.from_prompt(p) for p in prompts],
        total=len(prompts),
    )


@router.get(
    "/{prompt_id}",
    response_model=SystemPromptAdminResponse,
    summary="Get a system prompt",
)
async def get_system_prompt(
    prompt_id: str,
    admin_user: User = Depends(require_admin),
) -> SystemPromptAdminResponse:
    service = get_system_prompts_service()
    prompt = await service.get_prompt(prompt_id)
    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt '{prompt_id}' not found",
        )
    return SystemPromptAdminResponse.from_prompt(prompt)


@router.post(
    "/",
    response_model=SystemPromptAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a system prompt",
)
async def create_system_prompt(
    data: SystemPromptCreate,
    admin_user: User = Depends(require_admin),
) -> SystemPromptAdminResponse:
    try:
        service = get_system_prompts_service()
        prompt = await service.create_prompt(data, created_by=admin_user.email)
        return SystemPromptAdminResponse.from_prompt(prompt)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.patch(
    "/{prompt_id}",
    response_model=SystemPromptAdminResponse,
    summary="Update a system prompt",
)
async def update_system_prompt(
    prompt_id: str,
    updates: SystemPromptUpdate,
    admin_user: User = Depends(require_admin),
) -> SystemPromptAdminResponse:
    try:
        service = get_system_prompts_service()
        prompt = await service.update_prompt(prompt_id, updates)
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"System prompt '{prompt_id}' not found",
            )
        return SystemPromptAdminResponse.from_prompt(prompt)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete(
    "/{prompt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a system prompt",
)
async def delete_system_prompt(
    prompt_id: str,
    admin_user: User = Depends(require_admin),
) -> None:
    service = get_system_prompts_service()
    deleted = await service.delete_prompt(prompt_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt '{prompt_id}' not found",
        )
