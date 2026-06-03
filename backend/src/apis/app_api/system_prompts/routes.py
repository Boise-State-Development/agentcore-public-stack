"""User-facing read endpoint for system prompts.

Signed-in users fetch the list of enabled prompts to display in the
conversation settings panel. The prompt_text is never returned here —
only the name and description. Admin writes go through /admin/system-prompts.
"""

import logging

from fastapi import APIRouter, Depends

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.system_prompts.models import (
    SystemPromptUserListResponse,
    SystemPromptUserResponse,
)
from apis.shared.system_prompts.service import get_system_prompts_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system-prompts", tags=["system-prompts"])


@router.get(
    "/",
    response_model=SystemPromptUserListResponse,
    summary="List available system prompts",
)
async def list_enabled_system_prompts(
    current_user: User = Depends(get_current_user_from_session),
) -> SystemPromptUserListResponse:
    """Return all enabled system prompts (name and description only)."""
    service = get_system_prompts_service()
    prompts = await service.list_prompts(enabled_only=True)
    return SystemPromptUserListResponse(
        prompts=[SystemPromptUserResponse.from_prompt(p) for p in prompts],
        total=len(prompts),
    )
