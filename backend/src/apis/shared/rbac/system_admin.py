"""System administrator configuration and access control."""

import logging
from typing import Callable

from fastapi import Depends, HTTPException, status

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)


async def require_system_admin(
    user: User = Depends(get_current_user),
) -> User:
    """
    Require system administrator access.

    Resolves user permissions via the AppRole system and checks for
    the ``system_admin`` app role.  Fails closed: if the permission
    lookup raises an exception, access is denied.

    Usage:
        @router.post("/admin/roles")
        async def create_role(
            admin: User = Depends(require_system_admin)
        ):
            pass

    Args:
        user: Current authenticated user (injected)

    Returns:
        User object if authorized

    Raises:
        HTTPException: 403 if user lacks system admin access
    """
    from .service import get_app_role_service

    try:
        app_role_service = get_app_role_service()
        permissions = await app_role_service.resolve_user_permissions(user)
        if "system_admin" in permissions.app_roles:
            logger.debug(f"User {user.name} authorized as system admin")
            return user
    except Exception:
        logger.exception(
            f"Failed to resolve permissions for {user.name}, denying admin access"
        )

    logger.warning(
        f"User {user.name} (roles: {user.roles}) denied system admin access"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="System administrator access required",
    )


def require_tool_access(tool_id: str) -> Callable:
    """
    FastAPI dependency that checks if user can access a specific tool.

    Usage:
        @router.post("/tools/code-interpreter/execute")
        async def execute_code(
            user: User = Depends(require_tool_access("code_interpreter"))
        ):
            # User has been verified to have access
            pass
    """
    from .service import get_app_role_service

    async def checker(
        user: User = Depends(get_current_user),
    ) -> User:
        app_role_service = get_app_role_service()
        if not await app_role_service.can_access_tool(user, tool_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to tool: {tool_id}",
            )
        return user

    return checker


def require_model_access(model_id: str) -> Callable:
    """
    FastAPI dependency that checks if user can access a specific model.

    Usage:
        @router.post("/chat")
        async def chat(
            user: User = Depends(require_model_access("claude-opus"))
        ):
            # User has been verified to have access
            pass
    """
    from .service import get_app_role_service

    async def checker(
        user: User = Depends(get_current_user),
    ) -> User:
        app_role_service = get_app_role_service()
        if not await app_role_service.can_access_model(user, model_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to model: {model_id}",
            )
        return user

    return checker
