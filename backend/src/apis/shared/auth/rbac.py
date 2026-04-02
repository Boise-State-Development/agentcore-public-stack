"""Role-based access control via the AppRole system.

All authorization checks resolve through the AppRoleService, which maps
JWT ``cognito:groups`` claims to DynamoDB-backed AppRoles.  This gives a
single source of truth for permissions — no hardcoded group names.
"""

from typing import Callable
from fastapi import Depends, HTTPException, status
import logging

from .dependencies import get_current_user
from .models import User

logger = logging.getLogger(__name__)


def require_app_roles(*required_app_roles: str) -> Callable:
    """
    Create a dependency that checks the AppRole system for authorization.

    Resolves the user's effective AppRoles via the AppRoleService
    (JWT role → DynamoDB AppRole mapping) and checks if any of the
    required AppRoles are present.  Fails closed: if the permission
    lookup raises, access is denied.

    Usage:
        @router.get("/admin/users")
        async def list_users(user: User = Depends(require_app_roles("system_admin"))):
            ...

    Args:
        *required_app_roles: One or more AppRole IDs that grant access (OR logic)

    Returns:
        A FastAPI dependency function that validates AppRoles and returns the User

    Raises:
        HTTPException: 403 if user lacks all required AppRoles
    """
    async def checker(user: User = Depends(get_current_user)) -> User:
        from apis.shared.rbac.service import get_app_role_service

        try:
            service = get_app_role_service()
            permissions = await service.resolve_user_permissions(user)
            if any(role in permissions.app_roles for role in required_app_roles):
                logger.debug(
                    f"User {user.name} authorized via AppRoles: "
                    f"{set(permissions.app_roles) & set(required_app_roles)}"
                )
                return user
        except Exception:
            logger.exception(
                f"Failed to resolve AppRole permissions for {user.name}, denying access"
            )

        logger.warning(
            f"User {user.name} (jwt_roles: {user.roles}) denied access — "
            f"required AppRoles: {required_app_roles}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required AppRoles: {', '.join(required_app_roles)}",
        )

    return checker


# ---------------------------------------------------------------------------
# Predefined checkers
# ---------------------------------------------------------------------------

# Admin access — any JWT group mapped to the "system_admin" AppRole.
require_admin = require_app_roles("system_admin")
