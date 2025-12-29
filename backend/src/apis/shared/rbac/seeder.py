"""Seed default system roles on startup."""

import logging
from datetime import datetime

from .models import AppRole, EffectivePermissions
from .repository import AppRoleRepository

logger = logging.getLogger(__name__)


# System Admin Role - cannot be deleted, has full access
SYSTEM_ADMIN_ROLE = AppRole(
    role_id="system_admin",
    display_name="System Administrator",
    description="Full access to all system features. This role cannot be deleted.",
    jwt_role_mappings=[],  # Configured via ADMIN_JWT_ROLES env var
    inherits_from=[],
    granted_tools=["*"],
    granted_models=["*"],
    effective_permissions=EffectivePermissions(
        tools=["*"],
        models=["*"],
        quota_tier=None,  # No quota limits
    ),
    priority=1000,
    is_system_role=True,
    enabled=True,
    created_by="system",
)

# Default Role - fallback for users without mapped JWT roles
DEFAULT_ROLE = AppRole(
    role_id="default",
    display_name="Default User",
    description="Minimal access for users without specific role mappings. Can be modified but not deleted.",
    jwt_role_mappings=[],  # Special: applies when no other roles match
    inherits_from=[],
    granted_tools=[],
    granted_models=[],
    effective_permissions=EffectivePermissions(
        tools=[],
        models=[],
        quota_tier="tier_basic",
    ),
    priority=0,
    is_system_role=True,  # Cannot be deleted, but can be modified
    enabled=True,
    created_by="system",
)


async def seed_system_roles(repository: AppRoleRepository = None):
    """
    Seed system roles if they don't exist.

    This should be called on application startup.

    Args:
        repository: Optional repository instance (creates new one if not provided)
    """
    if repository is None:
        repository = AppRoleRepository()

    roles_to_seed = [SYSTEM_ADMIN_ROLE, DEFAULT_ROLE]
    now = datetime.utcnow().isoformat() + "Z"

    for role in roles_to_seed:
        try:
            existing = await repository.get_role(role.role_id)
            if existing:
                logger.debug(f"System role '{role.role_id}' already exists")
                continue

            # Set timestamps
            role.created_at = now
            role.updated_at = now

            # Create the role
            await repository.create_role(role)
            logger.info(f"Seeded system role: {role.role_id}")

        except Exception as e:
            logger.error(f"Failed to seed role '{role.role_id}': {e}")
            # Don't raise - continue seeding other roles


async def ensure_system_roles():
    """
    Ensure system roles exist.

    Convenience wrapper for seed_system_roles() that creates its own repository.
    """
    await seed_system_roles()
