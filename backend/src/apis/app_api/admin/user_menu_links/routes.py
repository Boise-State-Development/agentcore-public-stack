"""Admin API routes for user-menu link management.

All endpoints require admin role. Non-admin users hit the public
``GET /user-menu-links`` endpoint (registered under ``app_api.user_menu_links``)
which returns only enabled links and strips admin-only metadata.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User, require_admin
from apis.shared.user_menu_links.models import (
    UserMenuLinkCreate,
    UserMenuLinkListResponse,
    UserMenuLinkResponse,
    UserMenuLinkUpdate,
)
from apis.shared.user_menu_links.service import get_user_menu_links_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user-menu-links", tags=["admin-user-menu-links"])


@router.get(
    "/",
    response_model=UserMenuLinkListResponse,
    summary="List all user-menu links",
)
async def list_user_menu_links(
    enabled_only: bool = Query(False, description="Filter to enabled links only"),
    admin_user: User = Depends(require_admin),
) -> UserMenuLinkListResponse:
    """List all user-menu links (admin sees disabled ones too)."""
    service = get_user_menu_links_service()
    links = await service.list_links(enabled_only=enabled_only)
    return UserMenuLinkListResponse(
        links=[UserMenuLinkResponse.from_link(link) for link in links],
        total=len(links),
    )


@router.get(
    "/{link_id}",
    response_model=UserMenuLinkResponse,
    summary="Get a user-menu link",
)
async def get_user_menu_link(
    link_id: str,
    admin_user: User = Depends(require_admin),
) -> UserMenuLinkResponse:
    service = get_user_menu_links_service()
    link = await service.get_link(link_id)
    if not link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User-menu link '{link_id}' not found",
        )
    return UserMenuLinkResponse.from_link(link)


@router.post(
    "/",
    response_model=UserMenuLinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user-menu link",
)
async def create_user_menu_link(
    data: UserMenuLinkCreate,
    admin_user: User = Depends(require_admin),
) -> UserMenuLinkResponse:
    try:
        service = get_user_menu_links_service()
        link = await service.create_link(data, created_by=admin_user.email)
        return UserMenuLinkResponse.from_link(link)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.patch(
    "/{link_id}",
    response_model=UserMenuLinkResponse,
    summary="Update a user-menu link",
)
async def update_user_menu_link(
    link_id: str,
    updates: UserMenuLinkUpdate,
    admin_user: User = Depends(require_admin),
) -> UserMenuLinkResponse:
    try:
        service = get_user_menu_links_service()
        link = await service.update_link(link_id, updates)
        if not link:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User-menu link '{link_id}' not found",
            )
        return UserMenuLinkResponse.from_link(link)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete(
    "/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user-menu link",
)
async def delete_user_menu_link(
    link_id: str,
    admin_user: User = Depends(require_admin),
) -> None:
    service = get_user_menu_links_service()
    deleted = await service.delete_link(link_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User-menu link '{link_id}' not found",
        )
