"""Admin API routes for platform-wide settings.

Currently hosts the chat-mode policy: which agent mode (skills vs. tools)
new conversations get by default, and whether users may switch between
modes. The user-facing read of the same policy lives at
``GET /system/chat-settings``.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth import User, require_admin
from apis.shared.platform_settings.models import (
    ChatModeSettingsResponse,
    ChatModeSettingsUpdate,
)
from apis.shared.platform_settings.service import get_chat_mode_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["admin-settings"])


@router.get(
    "/chat",
    response_model=ChatModeSettingsResponse,
    summary="Get the chat-mode policy",
)
async def get_chat_mode_settings(
    admin_user: User = Depends(require_admin),
) -> ChatModeSettingsResponse:
    """Get the current chat-mode policy (defaults if never configured)."""
    service = get_chat_mode_settings_service()
    settings = await service.get_settings()
    return ChatModeSettingsResponse.from_settings(settings)


@router.put(
    "/chat",
    response_model=ChatModeSettingsResponse,
    summary="Update the chat-mode policy",
)
async def update_chat_mode_settings(
    update: ChatModeSettingsUpdate,
    admin_user: User = Depends(require_admin),
) -> ChatModeSettingsResponse:
    """Set the default agent mode and whether users may toggle between modes."""
    service = get_chat_mode_settings_service()
    try:
        settings = await service.update_settings(update, updated_by=admin_user.email)
    except Exception:
        logger.exception("Failed to update chat-mode settings")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update chat-mode settings",
        )
    logger.info(
        f"Admin {admin_user.email} updated chat-mode settings: "
        f"default_mode={settings.default_mode}, allow_mode_toggle={settings.allow_mode_toggle}"
    )
    return ChatModeSettingsResponse.from_settings(settings)
