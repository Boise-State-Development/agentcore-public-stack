"""AppRoleService for resolving and checking AppRole-based permissions."""

import logging
from typing import List, Set, Optional
from datetime import datetime

from apis.shared.auth.models import User

from .models import AppRole, UserEffectivePermissions
from .repository import AppRoleRepository
from .cache import AppRoleCache, get_app_role_cache

logger = logging.getLogger(__name__)


class AppRoleService:
    """
    Service for resolving and checking AppRole-based permissions.

    This is the main entry point for authorization checks.
    """

    def __init__(
        self,
        repository: Optional[AppRoleRepository] = None,
        cache: Optional[AppRoleCache] = None,
    ):
        """Initialize service with repository and cache."""
        self.repository = repository or AppRoleRepository()
        self.cache = cache or get_app_role_cache()

    async def resolve_user_permissions(
        self, user: User
    ) -> UserEffectivePermissions:
        """
        Resolve effective permissions for a user based on their JWT roles.

        This is the main entry point for authorization checks.

        Algorithm:
        1. Check user cache
        2. For each JWT role, find matching AppRoles
        3. Merge permissions (union for tools/models, highest priority for quota)
        4. Cache and return

        Args:
            user: Authenticated user with JWT roles

        Returns:
            UserEffectivePermissions with merged permissions
        """
        # Step 1: Check cache
        cached = await self.cache.get_user_permissions(user.user_id)
        if cached:
            logger.debug(f"Cache hit for user permissions: {user.user_id}")
            return cached

        # Step 2: Get all AppRoles that match user's JWT roles
        matching_roles: List[AppRole] = []
        jwt_roles = user.roles or []

        for jwt_role in jwt_roles:
            # Check JWT mapping cache
            role_ids = await self.cache.get_jwt_mapping(jwt_role)

            if role_ids is None:
                # Cache miss - query database
                role_ids = await self.repository.get_roles_for_jwt_role(jwt_role)
                await self.cache.set_jwt_mapping(jwt_role, role_ids)
                logger.debug(
                    f"JWT mapping cache miss for {jwt_role}, found {len(role_ids)} roles"
                )

            # Get full role objects
            for role_id in role_ids:
                role = await self._get_role_with_cache(role_id)
                if role and role.enabled:
                    matching_roles.append(role)

        # Step 3: If no roles matched, use default role
        if not matching_roles:
            default_role = await self._get_role_with_cache("default")
            if default_role and default_role.enabled:
                matching_roles = [default_role]
                logger.debug(
                    f"No matching roles for user {user.email}, using default role"
                )

        # Step 4: Merge permissions
        permissions = self._merge_permissions(user.user_id, matching_roles)

        # Step 5: Cache and return
        await self.cache.set_user_permissions(user.user_id, permissions)

        logger.debug(
            f"Resolved permissions for {user.email}: "
            f"roles={permissions.app_roles}, "
            f"tools={len(permissions.tools)}, "
            f"models={len(permissions.models)}"
        )

        return permissions

    async def _get_role_with_cache(self, role_id: str) -> Optional[AppRole]:
        """Get role from cache or database."""
        cached = await self.cache.get_role(role_id)
        if cached:
            return cached

        role = await self.repository.get_role(role_id)
        if role:
            await self.cache.set_role(role)
        return role

    def _merge_permissions(
        self, user_id: str, roles: List[AppRole]
    ) -> UserEffectivePermissions:
        """
        Merge permissions from multiple AppRoles.

        Merge rules:
        - Tools: Union (user gets access to all tools from all roles)
        - Models: Union (user gets access to all models from all roles)
        - Quota Tier: Highest priority role's tier wins
        """
        if not roles:
            return UserEffectivePermissions(
                user_id=user_id,
                app_roles=[],
                tools=[],
                models=[],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )

        # Collect all tools and models (union)
        all_tools: Set[str] = set()
        all_models: Set[str] = set()

        for role in roles:
            if role.effective_permissions:
                # Handle wildcard
                if "*" in role.effective_permissions.tools:
                    all_tools.add("*")
                else:
                    all_tools.update(role.effective_permissions.tools)

                if "*" in role.effective_permissions.models:
                    all_models.add("*")
                else:
                    all_models.update(role.effective_permissions.models)

        # Determine quota tier (highest priority wins)
        sorted_roles = sorted(roles, key=lambda r: r.priority, reverse=True)
        quota_tier = None
        for role in sorted_roles:
            if (
                role.effective_permissions
                and role.effective_permissions.quota_tier
            ):
                quota_tier = role.effective_permissions.quota_tier
                break

        return UserEffectivePermissions(
            user_id=user_id,
            app_roles=[r.role_id for r in roles],
            tools=list(all_tools),
            models=list(all_models),
            quota_tier=quota_tier,
            resolved_at=datetime.utcnow().isoformat() + "Z",
        )

    async def can_access_tool(self, user: User, tool_id: str) -> bool:
        """Check if user can access a specific tool."""
        permissions = await self.resolve_user_permissions(user)

        # Wildcard grants access to all
        if "*" in permissions.tools:
            return True

        return tool_id in permissions.tools

    async def can_access_model(self, user: User, model_id: str) -> bool:
        """Check if user can access a specific model."""
        permissions = await self.resolve_user_permissions(user)

        # Wildcard grants access to all
        if "*" in permissions.models:
            return True

        return model_id in permissions.models

    async def get_accessible_tools(self, user: User) -> List[str]:
        """Get list of tool IDs user can access."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.tools

    async def get_accessible_models(self, user: User) -> List[str]:
        """Get list of model IDs user can access."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.models

    async def get_user_quota_tier(self, user: User) -> Optional[str]:
        """Get the quota tier for a user based on their roles."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.quota_tier


# Global service instance (singleton)
_service_instance: Optional[AppRoleService] = None


def get_app_role_service() -> AppRoleService:
    """Get or create the global AppRoleService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = AppRoleService()
    return _service_instance
