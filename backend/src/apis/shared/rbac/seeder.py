"""Seed default system roles on startup."""

import logging
from datetime import datetime, timezone

from .models import AppRole, EffectivePermissions
from .repository import AppRoleRepository

logger = logging.getLogger(__name__)


# System Admin Role - cannot be deleted, has full access
SYSTEM_ADMIN_ROLE = AppRole(
    role_id="system_admin",
    display_name="System Administrator",
    description="Full access to all system features. This role cannot be deleted.",
    jwt_role_mappings=[],  # Populated by bootstrap seed script
    inherits_from=[],
    granted_tools=["*"],
    granted_models=["*"],
    granted_skills=["*"],
    effective_permissions=EffectivePermissions(
        tools=["*"],
        models=["*"],
        skills=["*"],
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
    granted_skills=[],
    effective_permissions=EffectivePermissions(
        tools=[],
        models=[],
        skills=[],
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
    now = datetime.now(timezone.utc).isoformat() + "Z"

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
            # JUSTIFICATION: Role seeding is a startup initialization task that should be
            # resilient to individual role failures. If one role fails to seed (e.g., due to
            # transient DynamoDB issues), we continue seeding other roles to maximize system
            # availability. The application can still start and function with partial roles.
            # Critical: system_admin role failure is logged and monitored for alerting.
            logger.error(f"Failed to seed role '{role.role_id}' (non-critical): {e}", exc_info=True)


async def ensure_system_roles():
    """
    Ensure system roles exist.

    Convenience wrapper for seed_system_roles() that creates its own repository.
    """
    await seed_system_roles()
