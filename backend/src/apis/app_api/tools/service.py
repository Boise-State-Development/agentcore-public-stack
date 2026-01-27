"""
Tool Catalog Service

Service for tool catalog operations with AppRole integration.
Provides CRUD operations, user access computation, and bidirectional sync.
"""

import logging
from typing import Dict, List, Optional, Set
from datetime import datetime

from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.rbac.admin_service import AppRoleAdminService, get_app_role_admin_service

from .models import (
    ToolDefinition,
    UserToolAccess,
    UserToolPreference,
    ToolCategory,
    ToolProtocol,
    ToolStatus,
    ToolRoleAssignment,
    AdminToolResponse,
    SyncResult,
)
from .repository import ToolCatalogRepository, get_tool_catalog_repository

# Import the existing in-memory catalog for fallback
from agents.main_agent.tools.tool_catalog import (
    TOOL_CATALOG,
    ToolMetadata,
    ToolCategory as LegacyToolCategory,
)

logger = logging.getLogger(__name__)


class ToolCatalogService:
    """
    Service for tool catalog operations.

    Tool access is determined by AppRoles. This service provides:
    - Catalog management (CRUD for tool metadata)
    - User preference management
    - Access computation using AppRoleService
    - Bidirectional sync between tools and AppRoles
    - Fallback to in-memory catalog during migration
    """

    def __init__(
        self,
        repository: Optional[ToolCatalogRepository] = None,
        app_role_service: Optional[AppRoleService] = None,
        app_role_admin_service: Optional[AppRoleAdminService] = None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_tool_catalog_repository()
        self.app_role_service = app_role_service or get_app_role_service()
        self.app_role_admin_service = app_role_admin_service or get_app_role_admin_service()
        self._use_fallback = True  # Use in-memory catalog as fallback

    # =========================================================================
    # User-Facing Methods
    # =========================================================================

    async def get_user_accessible_tools(self, user: User) -> List[UserToolAccess]:
        """
        Get tools accessible to a user based on their AppRole permissions.

        This is the main entry point for the GET /tools endpoint.

        Args:
            user: Authenticated user

        Returns:
            List of UserToolAccess objects with user's accessible tools
        """
        # Get effective permissions from AppRoleService
        permissions = await self.app_role_service.resolve_user_permissions(user)

        # Get all active tools from catalog
        all_tools = await self._get_all_active_tools()

        # Get user preferences
        prefs = await self.repository.get_user_preferences(user.user_id)

        accessible = []
        for tool in all_tools:
            granted_by = self._compute_granted_by(tool, permissions)

            if not granted_by:
                continue

            user_enabled = prefs.tool_preferences.get(tool.tool_id)
            is_enabled = user_enabled if user_enabled is not None else tool.enabled_by_default

            accessible.append(
                UserToolAccess(
                    tool_id=tool.tool_id,
                    display_name=tool.display_name,
                    description=tool.description,
                    category=tool.category,
                    icon=tool.icon,
                    protocol=tool.protocol,
                    status=tool.status,
                    granted_by=granted_by,
                    enabled_by_default=tool.enabled_by_default,
                    user_enabled=user_enabled,
                    is_enabled=is_enabled,
                )
            )

        return sorted(
            accessible,
            key=lambda t: (t.category if isinstance(t.category, str) else t.category.value, t.display_name),
        )

    def _compute_granted_by(
        self, tool: ToolDefinition, permissions: UserEffectivePermissions
    ) -> List[str]:
        """Compute which sources grant access to this tool."""
        granted_by = []

        if tool.is_public:
            granted_by.append("public")

        if "*" in permissions.tools or tool.tool_id in permissions.tools:
            granted_by.extend(permissions.app_roles)

        return list(set(granted_by))

    async def get_categories(self, user: User) -> List[str]:
        """Get unique categories for user's accessible tools."""
        tools = await self.get_user_accessible_tools(user)
        categories = set()
        for tool in tools:
            cat = tool.category
            if isinstance(cat, ToolCategory):
                categories.add(cat.value)
            else:
                categories.add(cat)
        return sorted(categories)

    async def save_user_preferences(
        self, user: User, preferences: Dict[str, bool]
    ) -> UserToolPreference:
        """
        Save user's tool preferences.

        Validates that user has access to the tools being configured.

        Args:
            user: Authenticated user
            preferences: Map of tool_id -> enabled state

        Returns:
            Updated UserToolPreference

        Raises:
            ValueError: If user tries to configure tools they don't have access to
        """
        # Get accessible tools
        accessible = await self.get_user_accessible_tools(user)
        accessible_ids = {t.tool_id for t in accessible}

        # Validate preferences
        invalid_tools = set(preferences.keys()) - accessible_ids
        if invalid_tools:
            raise ValueError(
                f"Cannot configure tools user doesn't have access to: {invalid_tools}"
            )

        # Save preferences
        return await self.repository.save_user_preferences(user.user_id, preferences)

    # =========================================================================
    # Admin Methods - Tool CRUD
    # =========================================================================

    async def get_all_tools(
        self, status: Optional[str] = None, include_roles: bool = True
    ) -> List[ToolDefinition]:
        """
        Get all tools in the catalog.

        Args:
            status: Optional status filter
            include_roles: If True, populate allowed_app_roles field

        Returns:
            List of ToolDefinition objects
        """
        tools = await self._get_all_active_tools(status=status)

        if include_roles:
            for tool in tools:
                roles = await self.get_roles_for_tool(tool.tool_id)
                tool.allowed_app_roles = [r.role_id for r in roles if r.grant_type == "direct"]

        return tools

    async def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """Get a specific tool by ID."""
        tool = await self.repository.get_tool(tool_id)

        # Fallback to in-memory catalog if not found in DB
        if not tool and self._use_fallback:
            legacy = TOOL_CATALOG.get(tool_id)
            if legacy:
                tool = self._legacy_to_definition(legacy)

        return tool

    async def create_tool(
        self, tool: ToolDefinition, admin: User
    ) -> ToolDefinition:
        """
        Create a new tool catalog entry.

        Args:
            tool: Tool definition to create
            admin: Admin user performing the action

        Returns:
            Created ToolDefinition
        """
        tool.created_by = admin.user_id
        tool.updated_by = admin.user_id

        created = await self.repository.create_tool(tool)

        logger.info(
            f"Admin {admin.email} created tool: {tool.tool_id}",
            extra={
                "event": "tool_created",
                "tool_id": tool.tool_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
            },
        )

        return created

    async def update_tool(
        self, tool_id: str, updates: Dict, admin: User
    ) -> Optional[ToolDefinition]:
        """
        Update a tool's metadata.

        Args:
            tool_id: Tool identifier
            updates: Fields to update
            admin: Admin user performing the action

        Returns:
            Updated ToolDefinition or None if not found
        """
        updated = await self.repository.update_tool(
            tool_id, updates, admin_user_id=admin.user_id
        )

        if updated:
            logger.info(
                f"Admin {admin.email} updated tool: {tool_id}",
                extra={
                    "event": "tool_updated",
                    "tool_id": tool_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "changes": list(updates.keys()),
                },
            )

        return updated

    async def delete_tool(self, tool_id: str, admin: User, soft: bool = True) -> bool:
        """
        Delete a tool from the catalog.

        Args:
            tool_id: Tool identifier
            admin: Admin user performing the action
            soft: If True, mark as disabled instead of deleting

        Returns:
            True if deleted/disabled, False if not found
        """
        if soft:
            result = await self.repository.soft_delete_tool(tool_id, admin.user_id)
            deleted = result is not None
        else:
            deleted = await self.repository.delete_tool(tool_id)

        if deleted:
            logger.info(
                f"Admin {admin.email} deleted tool: {tool_id}",
                extra={
                    "event": "tool_deleted",
                    "tool_id": tool_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "soft_delete": soft,
                },
            )

        return deleted

    # =========================================================================
    # Admin Methods - Role Sync
    # =========================================================================

    async def get_roles_for_tool(self, tool_id: str) -> List[ToolRoleAssignment]:
        """
        Get all AppRoles that grant access to a tool.

        Uses the ToolRoleMappingIndex GSI on AppRoles table.

        Args:
            tool_id: Tool identifier

        Returns:
            List of ToolRoleAssignment objects
        """
        # Query roles from repository
        role_infos = await self.app_role_admin_service.repository.get_roles_for_tool(tool_id)

        assignments = []
        for info in role_infos:
            role_id = info.get("roleId")
            if not role_id:
                continue

            # Get full role to check inheritance
            role = await self.app_role_admin_service.get_role(role_id)
            if not role:
                continue

            # Determine grant type
            grant_type = "direct" if tool_id in role.granted_tools else "inherited"
            inherited_from = None

            if grant_type == "inherited":
                # Find which parent role provides this tool
                for parent_id in role.inherits_from:
                    parent = await self.app_role_admin_service.get_role(parent_id)
                    if parent and tool_id in parent.effective_permissions.tools:
                        inherited_from = parent_id
                        break

            assignments.append(
                ToolRoleAssignment(
                    role_id=role_id,
                    display_name=role.display_name,
                    grant_type=grant_type,
                    inherited_from=inherited_from,
                    enabled=role.enabled,
                )
            )

        return assignments

    async def set_roles_for_tool(
        self, tool_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """
        Set which AppRoles grant access to a tool (bidirectional sync).

        This updates the grantedTools field on each affected AppRole.

        Args:
            tool_id: Tool identifier
            app_role_ids: List of AppRole IDs that should grant this tool
            admin: Admin user performing the action
        """
        # Verify tool exists
        tool = await self.get_tool(tool_id)
        if not tool:
            raise ValueError(f"Tool '{tool_id}' not found")

        # Get current roles that grant this tool (direct only)
        current_roles = await self.get_roles_for_tool(tool_id)
        current_role_ids = {r.role_id for r in current_roles if r.grant_type == "direct"}

        new_role_ids = set(app_role_ids)

        # Roles to add tool to
        to_add = new_role_ids - current_role_ids

        # Roles to remove tool from
        to_remove = current_role_ids - new_role_ids

        # Update each role
        for role_id in to_add:
            await self._add_tool_to_role(role_id, tool_id, admin)

        for role_id in to_remove:
            await self._remove_tool_from_role(role_id, tool_id, admin)

        logger.info(
            f"Admin {admin.email} set roles for tool {tool_id}",
            extra={
                "event": "tool_roles_updated",
                "tool_id": tool_id,
                "admin_user_id": admin.user_id,
                "roles_added": list(to_add),
                "roles_removed": list(to_remove),
            },
        )

    async def add_roles_to_tool(
        self, tool_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Add AppRoles to tool access (preserves existing)."""
        for role_id in app_role_ids:
            await self._add_tool_to_role(role_id, tool_id, admin)

    async def remove_roles_from_tool(
        self, tool_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Remove AppRoles from tool access."""
        for role_id in app_role_ids:
            await self._remove_tool_from_role(role_id, tool_id, admin)

    async def _add_tool_to_role(
        self, role_id: str, tool_id: str, admin: User
    ) -> None:
        """Add a tool to a role's grantedTools."""
        role = await self.app_role_admin_service.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if tool_id not in role.granted_tools:
            from apis.shared.rbac.models import AppRoleUpdate
            updates = AppRoleUpdate(granted_tools=role.granted_tools + [tool_id])
            await self.app_role_admin_service.update_role(role_id, updates, admin)

    async def _remove_tool_from_role(
        self, role_id: str, tool_id: str, admin: User
    ) -> None:
        """Remove a tool from a role's grantedTools."""
        role = await self.app_role_admin_service.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if tool_id in role.granted_tools:
            from apis.shared.rbac.models import AppRoleUpdate
            new_tools = [t for t in role.granted_tools if t != tool_id]
            updates = AppRoleUpdate(granted_tools=new_tools)
            await self.app_role_admin_service.update_role(role_id, updates, admin)

    # =========================================================================
    # Registry Sync
    # =========================================================================

    async def sync_catalog_from_registry(
        self, admin: User, dry_run: bool = True
    ) -> SyncResult:
        """
        Discover tools from backend registry and sync to catalog.

        Args:
            admin: Admin user performing the action
            dry_run: If True, only report what would happen

        Returns:
            SyncResult with discovered, orphaned, and unchanged tools
        """
        # Get registered tools from in-memory catalog
        registered_tools = TOOL_CATALOG
        registered_ids = set(registered_tools.keys())

        # Get catalog tools from DynamoDB
        catalog_tools = await self.repository.list_tools()
        catalog_ids = {t.tool_id for t in catalog_tools}

        discovered = []
        for tool_id, legacy in registered_tools.items():
            if tool_id not in catalog_ids:
                discovered.append({
                    "tool_id": tool_id,
                    "display_name": legacy.name,
                    "description": legacy.description,
                    "category": self._map_legacy_category(legacy.category),
                    "protocol": ToolProtocol.LOCAL,
                    "action": "create",
                })

        orphaned = []
        for tool in catalog_tools:
            if tool.tool_id not in registered_ids:
                orphaned.append({
                    "tool_id": tool.tool_id,
                    "action": "mark_deprecated",
                })

        unchanged = list(catalog_ids & registered_ids)

        if not dry_run:
            # Create discovered tools
            for item in discovered:
                tool = ToolDefinition(
                    tool_id=item["tool_id"],
                    display_name=item["display_name"],
                    description=item["description"],
                    category=item["category"],
                    protocol=item["protocol"],
                    status=ToolStatus.ACTIVE,
                    is_public=self._is_public_tool(item["tool_id"]),
                    enabled_by_default=self._get_default_enabled(item["tool_id"]),
                )
                await self.create_tool(tool, admin)

            # Mark orphaned as deprecated
            for item in orphaned:
                await self.update_tool(
                    item["tool_id"],
                    {"status": ToolStatus.DEPRECATED},
                    admin,
                )

            logger.info(
                f"Admin {admin.email} synced tool catalog",
                extra={
                    "event": "tool_catalog_synced",
                    "admin_user_id": admin.user_id,
                    "discovered": len(discovered),
                    "orphaned": len(orphaned),
                },
            )

        return SyncResult(
            discovered=discovered,
            orphaned=orphaned,
            unchanged=unchanged,
            dry_run=dry_run,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    async def _get_all_active_tools(
        self, status: Optional[str] = None
    ) -> List[ToolDefinition]:
        """
        Get all tools, using DynamoDB with fallback to in-memory catalog.
        """
        # Try to get from DynamoDB first
        tools = await self.repository.list_tools(status=status)

        # If no tools in DB and fallback enabled, use in-memory catalog
        if not tools and self._use_fallback:
            tools = [
                self._legacy_to_definition(legacy)
                for legacy in TOOL_CATALOG.values()
            ]
            # Apply status filter
            if status:
                tools = [t for t in tools if t.status == status]

        return tools

    def _legacy_to_definition(self, legacy: ToolMetadata) -> ToolDefinition:
        """Convert legacy ToolMetadata to ToolDefinition."""
        return ToolDefinition(
            tool_id=legacy.tool_id,
            display_name=legacy.name,
            description=legacy.description,
            category=self._map_legacy_category(legacy.category),
            icon=legacy.icon,
            protocol=ToolProtocol.MCP_GATEWAY if legacy.is_gateway_tool else ToolProtocol.LOCAL,
            status=ToolStatus.ACTIVE,
            requires_oauth_provider=legacy.requires_oauth_provider,
            is_public=self._is_public_tool(legacy.tool_id),
            enabled_by_default=self._get_default_enabled(legacy.tool_id),
        )

    def _map_legacy_category(self, legacy_cat: LegacyToolCategory) -> ToolCategory:
        """Map legacy category to new category enum."""
        mapping = {
            LegacyToolCategory.SEARCH: ToolCategory.SEARCH,
            LegacyToolCategory.BROWSER: ToolCategory.BROWSER,
            LegacyToolCategory.DATA: ToolCategory.DATA,
            LegacyToolCategory.UTILITIES: ToolCategory.UTILITY,
            LegacyToolCategory.CODE: ToolCategory.CODE,
            LegacyToolCategory.GATEWAY: ToolCategory.GATEWAY,
        }
        return mapping.get(legacy_cat, ToolCategory.UTILITY)

    def _is_public_tool(self, tool_id: str) -> bool:
        """Determine if a tool should be public (available to all users)."""
        public_tools = {
            "calculator",
            "get_current_weather",
            "ddg_web_search",
        }
        return tool_id in public_tools

    def _get_default_enabled(self, tool_id: str) -> bool:
        """Determine default enabled state for a tool."""
        # Based on current frontend defaults
        enabled_by_default = {
            "fetch_url_content",
            "search_boise_state",
            "calculator",
        }
        return tool_id in enabled_by_default


# Global service instance
_service_instance: Optional[ToolCatalogService] = None


def get_tool_catalog_service() -> ToolCatalogService:
    """Get or create the global ToolCatalogService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = ToolCatalogService()
    return _service_instance
