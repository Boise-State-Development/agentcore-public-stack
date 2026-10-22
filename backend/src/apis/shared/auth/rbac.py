"""Role-based access control via the AppRole system.

All authorization checks resolve through the AppRoleService, which maps
JWT ``cognito:groups`` claims to DynamoDB-backed AppRoles.  This gives a
single source of truth for permissions — no hardcoded group names.
"""

from typing import Callable
from fastapi import Depends, HTTPException, status
import logging

from .dependencies import get_current_user_from_session
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
    async def checker(user: User = Depends(get_current_user_from_session)) -> User:
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


async def has_admin_scope(user: User, scope: str) -> bool:
    """Whether ``user`` holds ``scope`` — the predicate behind ``require_admin_scope``.

    Exists because one caller needs the *answer* rather than a route guard: the invocation
    path's reviewer preview (``inference_api.chat.routes``) is a field on an existing
    request, not a route, so it cannot take a FastAPI dependency. Sharing the predicate is
    what keeps "who is a marketplace admin" from being answered twice, differently.

    Fails closed, exactly as the dependency does: a permission lookup that raises is a
    denial, never a default-allow.
    """
    from apis.shared.rbac.service import get_app_role_service

    try:
        permissions = await get_app_role_service().resolve_user_permissions(user)
    except Exception:
        logger.exception(
            f"Failed to resolve admin scope {scope} for {user.name}, denying access"
        )
        return False
    # ``system_admin`` satisfies every scope implicitly — the superuser rule the dependency
    # applies, restated here rather than reimplemented differently.
    return "system_admin" in permissions.app_roles or scope in permissions.admin_scopes


def require_admin_scope(scope: str) -> Callable:
    """
    Create a dependency guarding one delegated admin surface.

    A user passes if they hold the ``system_admin`` AppRole (the superuser
    satisfies every scope implicitly, so this is a no-op change for existing
    admins) *or* if any of their roles grants ``scope``.

    Takes a **single** scope, deliberately — ``require_app_roles(*roles)`` is OR
    logic, but an OR across admin scopes has no legitimate use here and would
    defeat the route-coverage test in ``tests/architecture/test_admin_scope_coverage.py``,
    which reads one scope per admin route.

    Fails closed: if permission resolution raises, access is denied.

    Args:
        scope: An id from ``apis.shared.rbac.admin_scopes.ADMIN_SCOPES``.

    Returns:
        A FastAPI dependency that validates the scope and returns the User.

    Raises:
        HTTPException: 403 if the user holds neither system_admin nor the scope.
    """
    async def checker(user: User = Depends(get_current_user_from_session)) -> User:
        if await has_admin_scope(user, scope):
            logger.debug(f"User {user.name} authorized for admin scope {scope}")
            return user

        logger.warning(
            f"User {user.name} (jwt_roles: {user.roles}) denied access — "
            f"required admin scope: {scope}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    return checker


# ---------------------------------------------------------------------------
# Predefined checkers
# ---------------------------------------------------------------------------

# Full admin access — any JWT group mapped to the "system_admin" AppRole.
#
# Reserved for the two surfaces that can never be delegated: role administration
# and auth-provider configuration (see `rbac/admin_scopes.py` for why the latter
# belongs in that set). Every other admin surface uses `require_admin_scope`.
require_admin = require_app_roles("system_admin")
