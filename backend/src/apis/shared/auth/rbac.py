"""Role-based access control utilities."""

from typing import List, Callable
from fastapi import Depends, HTTPException, status
import logging

from .dependencies import get_current_user
from .models import User

logger = logging.getLogger(__name__)


def require_roles(*required_roles: str) -> Callable:
    """
    Create a dependency that requires the user to have at least one of the specified roles.

    This creates a FastAPI dependency that checks if the authenticated user has any of the
    specified roles. If the user doesn't have any of the required roles, a 403 Forbidden
    response is returned.

    Usage:
        @router.post("/admin/users")
        async def admin_only_endpoint(user: User = Depends(require_roles("Admin", "SuperAdmin"))):
            return {"message": "Admin access granted"}

    Args:
        *required_roles: One or more role names that grant access (OR logic)

    Returns:
        A FastAPI dependency function that validates roles and returns the User object

    Raises:
        HTTPException: 403 Forbidden if user doesn't have any of the required roles
    """
    async def role_checker(user: User = Depends(get_current_user)) -> User:
        if not user.roles:
            logger.warning(f"User {user.email} has no assigned roles, denying access")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User has no assigned roles."
            )

        has_required_role = any(role in user.roles for role in required_roles)
        if not has_required_role:
            logger.warning(
                f"User {user.email} (roles: {user.roles}) lacks required roles: {required_roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {', '.join(required_roles)}"
            )

        logger.debug(f"User {user.email} authorized with roles: {user.roles}")
        return user

    return role_checker


def require_all_roles(*required_roles: str) -> Callable:
    """
    Create a dependency that requires the user to have ALL of the specified roles.

    This creates a FastAPI dependency that checks if the authenticated user has all of the
    specified roles. If the user is missing any required role, a 403 Forbidden response
    is returned.

    Usage:
        @router.post("/admin/critical")
        async def critical_endpoint(user: User = Depends(require_all_roles("Admin", "Security"))):
            return {"message": "Full access granted"}

    Args:
        *required_roles: All role names that must be present (AND logic)

    Returns:
        A FastAPI dependency function that validates roles and returns the User object

    Raises:
        HTTPException: 403 Forbidden if user doesn't have all required roles
    """
    async def role_checker(user: User = Depends(get_current_user)) -> User:
        if not user.roles:
            logger.warning(f"User {user.email} has no assigned roles, denying access")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User has no assigned roles."
            )

        has_all_roles = all(role in user.roles for role in required_roles)
        if not has_all_roles:
            missing_roles = [role for role in required_roles if role not in user.roles]
            logger.warning(
                f"User {user.email} (roles: {user.roles}) missing required roles: {missing_roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Missing required roles: {', '.join(missing_roles)}"
            )

        logger.debug(f"User {user.email} authorized with all required roles: {required_roles}")
        return user

    return role_checker


def has_any_role(user: User, *roles: str) -> bool:
    """
    Helper function to check if a user has any of the specified roles.

    Useful for conditional logic within route handlers without raising exceptions.

    Usage:
        async def my_endpoint(user: User = Depends(get_current_user)):
            if has_any_role(user, "Admin", "SuperAdmin"):
                # Show additional admin data
                pass

    Args:
        user: User object to check
        *roles: Role names to check for

    Returns:
        True if user has any of the specified roles, False otherwise
    """
    if not user.roles:
        return False
    return any(role in user.roles for role in roles)


def has_all_roles(user: User, *roles: str) -> bool:
    """
    Helper function to check if a user has all of the specified roles.

    Useful for conditional logic within route handlers without raising exceptions.

    Usage:
        async def my_endpoint(user: User = Depends(get_current_user)):
            if has_all_roles(user, "Admin", "Security"):
                # Perform security-sensitive operation
                pass

    Args:
        user: User object to check
        *roles: Role names to check for

    Returns:
        True if user has all of the specified roles, False otherwise
    """
    if not user.roles:
        return False
    return all(role in user.roles for role in roles)


# Predefined role checkers for common use cases
# These can be used directly as dependencies: async def endpoint(user: User = Depends(require_admin))

# Admin access - requires either Admin or SuperAdmin role
require_admin = require_roles("Admin", "SuperAdmin", "DotNetDevelopers")

# Faculty access
require_faculty = require_roles("Faculty")

# Staff access
require_staff = require_roles("Staff")

# Developer access
require_developer = require_roles("DotNetDevelopers")

# AWS AI access
require_aws_ai_access = require_roles("AWS-BoiseStateAI")
