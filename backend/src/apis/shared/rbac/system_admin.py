"""System administrator configuration and access control."""

import os
import json
import logging
from typing import List, Callable

from fastapi import Depends, HTTPException, status

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)


class SystemAdminConfig:
    """
    Configuration for system administrator access.

    System admins have full access to all RBAC features and cannot be
    locked out by misconfigured roles.
    """

    @staticmethod
    def get_admin_jwt_roles() -> List[str]:
        """
        Get JWT roles that grant system admin access.

        Configured via ADMIN_JWT_ROLES environment variable.
        Defaults to ["DotNetDevelopers"] for backwards compatibility.
        """
        roles_json = os.getenv("ADMIN_JWT_ROLES", '["DotNetDevelopers"]')
        try:
            roles = json.loads(roles_json)
            if isinstance(roles, list):
                return roles
        except json.JSONDecodeError:
            logger.warning(
                f"Invalid ADMIN_JWT_ROLES format: {roles_json}, using default"
            )
        return ["DotNetDevelopers"]

    @staticmethod
    def is_system_admin(user_roles: List[str]) -> bool:
        """Check if user has system admin access via JWT roles."""
        if not user_roles:
            return False
        admin_roles = SystemAdminConfig.get_admin_jwt_roles()
        return any(role in user_roles for role in admin_roles)


async def require_system_admin(
    user: User = Depends(get_current_user),
) -> User:
    """
    Require system administrator access.

    This uses the hardcoded admin check, NOT the AppRole system,
    to prevent lockout scenarios.

    Usage:
        @router.post("/admin/roles")
        async def create_role(
            admin: User = Depends(require_system_admin)
        ):
            # User has been verified to have system admin access
            pass

    Args:
        user: Current authenticated user (injected)

    Returns:
        User object if authorized

    Raises:
        HTTPException: 403 if user lacks system admin access
    """
    if not SystemAdminConfig.is_system_admin(user.roles or []):
        logger.warning(
            f"User {user.email} (roles: {user.roles}) "
            f"denied system admin access"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System administrator access required",
        )

    logger.debug(f"User {user.email} authorized as system admin")
    return user


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
