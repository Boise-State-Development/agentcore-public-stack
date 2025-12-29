"""Admin API routes for AppRole management."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query

from apis.shared.auth import User, require_admin
from apis.shared.rbac import (
    AppRoleService,
    AppRoleAdminService,
    AppRoleCache,
)
from apis.shared.rbac.models import (
    AppRoleCreate,
    AppRoleUpdate,
    AppRoleResponse,
    AppRoleListResponse,
    CacheStatsResponse,
)
from apis.shared.rbac.system_admin import require_system_admin
from apis.shared.rbac.service import get_app_role_service
from apis.shared.rbac.admin_service import get_app_role_admin_service
from apis.shared.rbac.cache import get_app_role_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/roles", tags=["admin-roles"])


@router.get("/", response_model=AppRoleListResponse)
async def list_roles(
    enabled_only: bool = Query(
        False, description="Only return enabled roles"
    ),
    admin: User = Depends(require_admin),
):
    """
    List all application roles.

    Requires admin access (Admin, SuperAdmin, or DotNetDevelopers role).

    Args:
        enabled_only: If True, only return enabled roles
        admin: Authenticated admin user (injected)

    Returns:
        AppRoleListResponse with list of all roles
    """
    logger.info(f"Admin {admin.email} listing roles")

    service = get_app_role_admin_service()
    roles = await service.list_roles(enabled_only=enabled_only)

    return AppRoleListResponse(
        roles=[AppRoleResponse.from_app_role(r) for r in roles],
        total=len(roles),
    )


@router.get("/{role_id}", response_model=AppRoleResponse)
async def get_role(
    role_id: str,
    admin: User = Depends(require_admin),
):
    """
    Get a role by ID.

    Requires admin access.

    Args:
        role_id: Role identifier
        admin: Authenticated admin user (injected)

    Returns:
        AppRoleResponse with role details

    Raises:
        HTTPException: 404 if role not found
    """
    logger.info(f"Admin {admin.email} getting role: {role_id}")

    service = get_app_role_admin_service()
    role = await service.get_role(role_id)

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role '{role_id}' not found",
        )

    return AppRoleResponse.from_app_role(role)


@router.post("/", response_model=AppRoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    role_data: AppRoleCreate,
    admin: User = Depends(require_system_admin),
):
    """
    Create a new application role.

    Requires system administrator access.

    Args:
        role_data: Role creation data
        admin: Authenticated system admin user (injected)

    Returns:
        Created AppRoleResponse

    Raises:
        HTTPException: 400 if role already exists or validation fails
    """
    logger.info(f"Admin {admin.email} creating role: {role_data.role_id}")

    try:
        service = get_app_role_admin_service()
        role = await service.create_role(role_data, admin)
        return AppRoleResponse.from_app_role(role)

    except ValueError as e:
        logger.warning(f"Role creation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.patch("/{role_id}", response_model=AppRoleResponse)
async def update_role(
    role_id: str,
    updates: AppRoleUpdate,
    admin: User = Depends(require_system_admin),
):
    """
    Update an application role.

    Requires system administrator access.
    System roles have limited editability.

    Args:
        role_id: Role identifier
        updates: Fields to update
        admin: Authenticated system admin user (injected)

    Returns:
        Updated AppRoleResponse

    Raises:
        HTTPException:
            - 400 if validation fails
            - 404 if role not found
    """
    logger.info(f"Admin {admin.email} updating role: {role_id}")

    try:
        service = get_app_role_admin_service()
        role = await service.update_role(role_id, updates, admin)

        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role '{role_id}' not found",
            )

        return AppRoleResponse.from_app_role(role)

    except ValueError as e:
        logger.warning(f"Role update failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    admin: User = Depends(require_system_admin),
):
    """
    Delete an application role.

    Requires system administrator access.
    System roles cannot be deleted.

    Args:
        role_id: Role identifier
        admin: Authenticated system admin user (injected)

    Raises:
        HTTPException:
            - 400 if trying to delete a system role
            - 404 if role not found
    """
    logger.info(f"Admin {admin.email} deleting role: {role_id}")

    try:
        service = get_app_role_admin_service()
        success = await service.delete_role(role_id, admin)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role '{role_id}' not found",
            )

    except ValueError as e:
        logger.warning(f"Role deletion failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/{role_id}/sync", response_model=AppRoleResponse)
async def sync_role_permissions(
    role_id: str,
    admin: User = Depends(require_system_admin),
):
    """
    Force recomputation of effective permissions for a role.

    Useful after inheritance changes or to fix data inconsistencies.

    Requires system administrator access.

    Args:
        role_id: Role identifier
        admin: Authenticated system admin user (injected)

    Returns:
        Updated AppRoleResponse

    Raises:
        HTTPException: 404 if role not found
    """
    logger.info(f"Admin {admin.email} syncing permissions for role: {role_id}")

    service = get_app_role_admin_service()
    role = await service.sync_effective_permissions(role_id, admin)

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role '{role_id}' not found",
        )

    return AppRoleResponse.from_app_role(role)


@router.get("/cache/stats", response_model=CacheStatsResponse)
async def get_cache_stats(
    admin: User = Depends(require_system_admin),
):
    """
    Get cache statistics.

    Requires system administrator access.

    Args:
        admin: Authenticated system admin user (injected)

    Returns:
        CacheStatsResponse with cache statistics
    """
    logger.info(f"Admin {admin.email} getting cache stats")

    cache = get_app_role_cache()
    stats = cache.get_stats()

    return CacheStatsResponse(**stats)


@router.post("/cache/invalidate", status_code=status.HTTP_204_NO_CONTENT)
async def invalidate_cache(
    admin: User = Depends(require_system_admin),
):
    """
    Force invalidation of all role caches.

    Requires system administrator access.

    Args:
        admin: Authenticated system admin user (injected)
    """
    logger.info(f"Admin {admin.email} invalidating all caches")

    cache = get_app_role_cache()
    await cache.invalidate_all()
