"""
Tool Access Service

Handles tool authorization based on AppRoles.
Similar to ModelAccessService but for tools.
"""
import logging
from typing import List, Optional, Set

from apis.shared.auth.models import User
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from agents.strands_agent.tools import get_tool_catalog_service

logger = logging.getLogger(__name__)


class ToolAccessService:
    """Service for checking tool access based on AppRoles."""

    def __init__(self, app_role_service: Optional[AppRoleService] = None):
        """Initialize with optional AppRoleService."""
        self._app_role_service = app_role_service
        self._tool_catalog = get_tool_catalog_service()

    @property
    def app_role_service(self) -> AppRoleService:
        """Lazy-load AppRoleService."""
        if self._app_role_service is None:
            self._app_role_service = get_app_role_service()
        return self._app_role_service

    async def get_user_allowed_tools(self, user: User) -> Set[str]:
        """
        Get the set of tool IDs the user is allowed to use.

        Returns:
            Set of tool IDs. Contains "*" if user has wildcard access.
        """
        permissions = await self.app_role_service.resolve_user_permissions(user)
        return set(permissions.tools)

    async def can_access_tool(self, user: User, tool_id: str) -> bool:
        """
        Check if a user can access a specific tool.

        Args:
            user: The user to check
            tool_id: Tool identifier

        Returns:
            True if user has access to the tool
        """
        allowed_tools = await self.get_user_allowed_tools(user)

        # Wildcard grants access to all tools
        if "*" in allowed_tools:
            return True

        # Check if specific tool is in allowed set
        return tool_id in allowed_tools

    async def filter_allowed_tools(
        self,
        user: User,
        requested_tools: Optional[List[str]] = None
    ) -> List[str]:
        """
        Filter a list of requested tools to only those the user can access.

        If no tools are requested, returns all tools the user can access.

        Args:
            user: The user to check
            requested_tools: Optional list of tool IDs the user wants to use.
                           If None, returns all allowed tools.

        Returns:
            List of tool IDs the user is allowed to use from the requested set.
        """
        allowed_tools = await self.get_user_allowed_tools(user)
        has_wildcard = "*" in allowed_tools

        # Get all available tool IDs from catalog
        all_tool_ids = set(self._tool_catalog.get_tool_ids())

        if requested_tools is None:
            # No specific tools requested - return all allowed
            if has_wildcard:
                return list(all_tool_ids)
            else:
                # Only return allowed tools that exist in the catalog
                return list(allowed_tools & all_tool_ids)

        # Filter requested tools to only allowed ones
        requested_set = set(requested_tools)

        if has_wildcard:
            # Wildcard: allow all requested tools that exist
            # (allow gateway tools even if not in catalog)
            return [
                t for t in requested_tools
                if t in all_tool_ids or t.startswith("gateway_")
            ]
        else:
            # Only return intersection of requested and allowed
            return [
                t for t in requested_tools
                if t in allowed_tools
            ]

    async def check_access_and_filter(
        self,
        user: User,
        requested_tools: Optional[List[str]] = None,
        strict: bool = False
    ) -> tuple[List[str], List[str]]:
        """
        Check tool access and return both allowed and denied tools.

        Args:
            user: The user to check
            requested_tools: Optional list of tool IDs the user wants to use
            strict: If True, raise ValueError if any requested tool is denied

        Returns:
            Tuple of (allowed_tools, denied_tools)

        Raises:
            ValueError: If strict=True and any tools are denied
        """
        if requested_tools is None:
            allowed = await self.filter_allowed_tools(user, None)
            return allowed, []

        allowed = await self.filter_allowed_tools(user, requested_tools)
        allowed_set = set(allowed)
        denied = [t for t in requested_tools if t not in allowed_set]

        if strict and denied:
            raise ValueError(
                f"User {user.email} is not authorized to use tools: {', '.join(denied)}"
            )

        if denied:
            logger.warning(
                f"User {user.email} requested unauthorized tools: {denied}"
            )

        return allowed, denied


# Singleton instance
_tool_access_service: Optional[ToolAccessService] = None


def get_tool_access_service() -> ToolAccessService:
    """Get the singleton ToolAccessService instance."""
    global _tool_access_service
    if _tool_access_service is None:
        _tool_access_service = ToolAccessService()
    return _tool_access_service
