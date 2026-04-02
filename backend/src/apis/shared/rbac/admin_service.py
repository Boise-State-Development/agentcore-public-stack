"""Admin service for AppRole management operations."""

import logging
from typing import List, Optional, Set

from apis.shared.auth.models import User

from .models import AppRole, EffectivePermissions, AppRoleCreate, AppRoleUpdate
from .repository import AppRoleRepository
from .cache import AppRoleCache, get_app_role_cache

logger = logging.getLogger(__name__)


class AppRoleAdminService:
    """
    Service for administrative operations on AppRoles.

    Handles:
    - CRUD operations for roles
    - Permission computation (inheritance resolution)
    - Cache invalidation on updates
    - System role protection
    """

    def __init__(
        self,
        repository: Optional[AppRoleRepository] = None,
        cache: Optional[AppRoleCache] = None,
    ):
        """Initialize admin service with repository and cache."""
        self.repository = repository or AppRoleRepository()
        self.cache = cache or get_app_role_cache()

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def list_roles(self, enabled_only: bool = False) -> List[AppRole]:
        """List all roles."""
        return await self.repository.list_roles(enabled_only=enabled_only)

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        """Get a role by ID."""
        return await self.repository.get_role(role_id)

    async def create_role(
        self, role_data: AppRoleCreate, admin: User
    ) -> AppRole:
        """
        Create a new AppRole.

        Args:
            role_data: Role creation data
            admin: Admin user performing the action

        Returns:
            Created AppRole

        Raises:
            ValueError: If role already exists or validation fails
        """
        # Build the AppRole object
        role = AppRole(
            role_id=role_data.role_id,
            display_name=role_data.display_name,
            description=role_data.description,
            jwt_role_mappings=role_data.jwt_role_mappings,
            inherits_from=role_data.inherits_from,
            granted_tools=role_data.granted_tools,
            granted_models=role_data.granted_models,
            priority=role_data.priority,
            enabled=role_data.enabled,
            is_system_role=False,
            created_by=admin.user_id,
        )

        # Validate inheritance (check that parent roles exist)
        await self._validate_inheritance(role.inherits_from)

        # Compute effective permissions
        role.effective_permissions = await self._compute_effective_permissions(
            role
        )

        # Create in database
        created_role = await self.repository.create_role(role)

        # Invalidate caches
        await self._invalidate_caches_for_role(role)

        logger.info(
            f"Admin {admin.email} created role: {role.role_id}",
            extra={
                "event": "app_role_created",
                "role_id": role.role_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
            },
        )

        return created_role

    async def update_role(
        self, role_id: str, updates: AppRoleUpdate, admin: User
    ) -> Optional[AppRole]:
        """
        Update an AppRole.

        Args:
            role_id: Role identifier
            updates: Fields to update
            admin: Admin user performing the action

        Returns:
            Updated AppRole or None if not found

        Raises:
            ValueError: If validation fails or trying to modify protected fields
        """
        existing = await self.repository.get_role(role_id)
        if not existing:
            return None

        # System role protection
        if existing.is_system_role and role_id == "system_admin":
            # For system_admin, only allow updating display_name and description
            allowed_fields = {"display_name", "description"}
            update_dict = updates.model_dump(exclude_unset=True)
            invalid_fields = set(update_dict.keys()) - allowed_fields
            if invalid_fields:
                raise ValueError(
                    f"Cannot modify protected fields on system_admin role: {invalid_fields}"
                )

        # Apply updates
        update_dict = updates.model_dump(exclude_unset=True, by_alias=False)
        for field, value in update_dict.items():
            if hasattr(existing, field):
                setattr(existing, field, value)

        # Validate inheritance if changed
        if updates.inherits_from is not None:
            await self._validate_inheritance(existing.inherits_from)

        # Recompute effective permissions
        existing.effective_permissions = await self._compute_effective_permissions(
            existing
        )

        # Update in database
        updated_role = await self.repository.update_role(existing)

        # Invalidate caches
        await self._invalidate_caches_for_role(existing)

        logger.info(
            f"Admin {admin.email} updated role: {role_id}",
            extra={
                "event": "app_role_updated",
                "role_id": role_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
                "changes": list(update_dict.keys()),
            },
        )

        return updated_role

    async def delete_role(self, role_id: str, admin: User) -> bool:
        """
        Delete an AppRole.

        Args:
            role_id: Role identifier
            admin: Admin user performing the action

        Returns:
            True if deleted, False if not found

        Raises:
            ValueError: If trying to delete a system role
        """
        existing = await self.repository.get_role(role_id)
        if not existing:
            return False

        if existing.is_system_role:
            raise ValueError(f"Cannot delete system role: {role_id}")

        # Delete from database
        deleted = await self.repository.delete_role(role_id)

        if deleted:
            # Invalidate caches
            await self.cache.invalidate_role(role_id)
            for jwt_role in existing.jwt_role_mappings:
                await self.cache.invalidate_jwt_mapping(jwt_role)

            logger.info(
                f"Admin {admin.email} deleted role: {role_id}",
                extra={
                    "event": "app_role_deleted",
                    "role_id": role_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                },
            )

        return deleted

    async def sync_effective_permissions(
        self, role_id: str, admin: User
    ) -> Optional[AppRole]:
        """
        Force recomputation of effective permissions for a role.

        Useful after inheritance changes or to fix data inconsistencies.

        Args:
            role_id: Role identifier
            admin: Admin user performing the action

        Returns:
            Updated AppRole or None if not found
        """
        existing = await self.repository.get_role(role_id)
        if not existing:
            return None

        # Recompute effective permissions
        existing.effective_permissions = await self._compute_effective_permissions(
            existing
        )

        # Update in database
        updated_role = await self.repository.update_role(existing)

        # Invalidate caches
        await self._invalidate_caches_for_role(existing)

        logger.info(
            f"Admin {admin.email} synced permissions for role: {role_id}",
            extra={
                "event": "app_role_synced",
                "role_id": role_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
            },
        )

        return updated_role

    # =========================================================================
    # Permission Computation
    # =========================================================================

    async def _compute_effective_permissions(
        self, role: AppRole
    ) -> EffectivePermissions:
        """
        Compute effective permissions for a role, including inheritance.

        This resolves single-level inheritance and merges permissions.
        """
        all_tools: Set[str] = set(role.granted_tools)
        all_models: Set[str] = set(role.granted_models)

        # Process inherited roles (single level only)
        for parent_role_id in role.inherits_from:
            parent = await self.repository.get_role(parent_role_id)
            if parent and parent.enabled:
                all_tools.update(parent.granted_tools)
                all_models.update(parent.granted_models)

        return EffectivePermissions(
            tools=list(all_tools),
            models=list(all_models),
            quota_tier=None,  # Quota tier comes from direct configuration
        )

    async def _validate_inheritance(self, inherits_from: List[str]):
        """Validate that all parent roles exist."""
        for parent_role_id in inherits_from:
            parent = await self.repository.get_role(parent_role_id)
            if not parent:
                raise ValueError(
                    f"Inherited role '{parent_role_id}' does not exist"
                )

    async def _invalidate_caches_for_role(self, role: AppRole):
        """Invalidate all relevant caches after role update."""
        await self.cache.invalidate_role(role.role_id)
        for jwt_role in role.jwt_role_mappings:
            await self.cache.invalidate_jwt_mapping(jwt_role)

    # =========================================================================
    # Tool Management Extensions
    # =========================================================================

    async def get_roles_granting_tool(self, tool_id: str) -> List[dict]:
        """
        Query which AppRoles grant access to a specific tool.
        Uses GSI2 (ToolRoleMappingIndex) for efficient lookup.

        Args:
            tool_id: The tool identifier

        Returns:
            List of role info dicts with roleId, displayName, grantType, etc.
        """
        # Query GSI2: GSI2PK=TOOL#{tool_id}
        results = await self.repository.get_roles_for_tool(tool_id)

        roles = []
        for item in results:
            role_id = item.get("roleId")
            if not role_id:
                continue

            role = await self.get_role(role_id)
            if not role:
                continue

            # Determine if grant is direct or inherited
            grant_type = "direct" if tool_id in role.granted_tools else "inherited"
            inherited_from = None

            if grant_type == "inherited":
                # Find which parent role provides this tool
                for parent_id in role.inherits_from:
                    parent = await self.get_role(parent_id)
                    if parent and tool_id in parent.effective_permissions.tools:
                        inherited_from = parent_id
                        break

            roles.append({
                "roleId": role.role_id,
                "displayName": role.display_name,
                "grantType": grant_type,
                "inheritedFrom": inherited_from,
                "enabled": role.enabled,
            })

        return roles

    async def add_tool_to_role(
        self, role_id: str, tool_id: str, admin: User
    ) -> AppRole:
        """
        Add a tool to a role's grantedTools.
        Triggers permission recomputation.

        Args:
            role_id: Role identifier
            tool_id: Tool identifier
            admin: Admin user performing the action

        Returns:
            Updated AppRole

        Raises:
            ValueError: If role not found
        """
        role = await self.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if tool_id not in role.granted_tools:
            new_tools = role.granted_tools + [tool_id]
            updates = AppRoleUpdate(granted_tools=new_tools)
            updated = await self.update_role(role_id, updates, admin)
            if updated:
                logger.info(
                    f"Admin {admin.email} added tool {tool_id} to role {role_id}",
                    extra={
                        "event": "tool_added_to_role",
                        "role_id": role_id,
                        "tool_id": tool_id,
                        "admin_user_id": admin.user_id,
                    },
                )
                return updated

        return role

    async def remove_tool_from_role(
        self, role_id: str, tool_id: str, admin: User
    ) -> AppRole:
        """
        Remove a tool from a role's grantedTools.
        Triggers permission recomputation.

        Args:
            role_id: Role identifier
            tool_id: Tool identifier
            admin: Admin user performing the action

        Returns:
            Updated AppRole

        Raises:
            ValueError: If role not found
        """
        role = await self.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if tool_id in role.granted_tools:
            new_tools = [t for t in role.granted_tools if t != tool_id]
            updates = AppRoleUpdate(granted_tools=new_tools)
            updated = await self.update_role(role_id, updates, admin)
            if updated:
                logger.info(
                    f"Admin {admin.email} removed tool {tool_id} from role {role_id}",
                    extra={
                        "event": "tool_removed_from_role",
                        "role_id": role_id,
                        "tool_id": tool_id,
                        "admin_user_id": admin.user_id,
                    },
                )
                return updated

        return role


# Global service instance
_admin_service_instance: Optional[AppRoleAdminService] = None


def get_app_role_admin_service() -> AppRoleAdminService:
    """Get or create the global AppRoleAdminService instance."""
    global _admin_service_instance
    if _admin_service_instance is None:
        _admin_service_instance = AppRoleAdminService()
    return _admin_service_instance
