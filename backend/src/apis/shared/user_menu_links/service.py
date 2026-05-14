"""Service layer for user-menu links."""

from typing import List, Optional

from .models import UserMenuLink, UserMenuLinkCreate, UserMenuLinkUpdate
from .repository import UserMenuLinksRepository, get_user_menu_links_repository


class UserMenuLinksService:
    def __init__(self, repository: UserMenuLinksRepository):
        self._repo = repository

    async def list_links(self, enabled_only: bool = False) -> List[UserMenuLink]:
        return await self._repo.list_links(enabled_only=enabled_only)

    async def get_link(self, link_id: str) -> Optional[UserMenuLink]:
        return await self._repo.get_link(link_id)

    async def create_link(
        self, data: UserMenuLinkCreate, created_by: Optional[str] = None
    ) -> UserMenuLink:
        return await self._repo.create_link(data, created_by=created_by)

    async def update_link(
        self, link_id: str, updates: UserMenuLinkUpdate
    ) -> Optional[UserMenuLink]:
        return await self._repo.update_link(link_id, updates)

    async def delete_link(self, link_id: str) -> bool:
        return await self._repo.delete_link(link_id)


_service: Optional[UserMenuLinksService] = None


def get_user_menu_links_service() -> UserMenuLinksService:
    global _service
    if _service is None:
        _service = UserMenuLinksService(get_user_menu_links_repository())
    return _service
