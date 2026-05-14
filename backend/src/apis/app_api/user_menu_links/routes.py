"""Public read endpoint for user-menu links.

Signed-in users (any role) fetch enabled links to render in the user menu.
Admin writes go through ``/admin/user-menu-links``.
"""

import logging

from fastapi import APIRouter, Depends

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.user_menu_links.models import (
    UserMenuLinkListResponse,
    UserMenuLinkResponse,
)
from apis.shared.user_menu_links.service import get_user_menu_links_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user-menu-links", tags=["user-menu-links"])


@router.get(
    "/",
    response_model=UserMenuLinkListResponse,
    summary="List enabled user-menu links",
)
async def list_enabled_user_menu_links(
    current_user: User = Depends(get_current_user_from_session),
) -> UserMenuLinkListResponse:
    """Return all enabled links for rendering in the SPA user menu."""
    service = get_user_menu_links_service()
    links = await service.list_links(enabled_only=True)
    return UserMenuLinkListResponse(
        links=[UserMenuLinkResponse.from_link(link) for link in links],
        total=len(links),
    )
