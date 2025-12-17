"""Admin API routes for quota management."""

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
import logging
from apis.shared.auth import User, require_admin
from apis.app_api.costs.aggregator import CostAggregator
from agentcore.quota.repository import QuotaRepository
from agentcore.quota.resolver import QuotaResolver
from agentcore.quota.models import QuotaTier, QuotaAssignment
from .service import QuotaAdminService
from .models import (
    QuotaTierCreate,
    QuotaTierUpdate,
    QuotaAssignmentCreate,
    QuotaAssignmentUpdate,
    UserQuotaInfo
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quota", tags=["admin-quota"])


# ========== Dependencies ==========

def get_quota_repository() -> QuotaRepository:
    """Get quota repository instance"""
    return QuotaRepository()


def get_quota_resolver(
    repo: QuotaRepository = Depends(get_quota_repository)
) -> QuotaResolver:
    """Get quota resolver instance"""
    return QuotaResolver(repository=repo)


def get_cost_aggregator() -> CostAggregator:
    """Get cost aggregator instance"""
    return CostAggregator()


def get_quota_service(
    repo: QuotaRepository = Depends(get_quota_repository),
    resolver: QuotaResolver = Depends(get_quota_resolver),
    cost_aggregator: CostAggregator = Depends(get_cost_aggregator)
) -> QuotaAdminService:
    """Get quota admin service instance"""
    return QuotaAdminService(
        repository=repo,
        resolver=resolver,
        cost_aggregator=cost_aggregator
    )


# ========== Quota Tiers ==========

@router.post("/tiers", response_model=QuotaTier, status_code=status.HTTP_201_CREATED)
async def create_tier(
    tier_data: QuotaTierCreate,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Create a new quota tier (admin only).

    Args:
        tier_data: Tier configuration
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Created quota tier

    Raises:
        HTTPException:
            - 400 if tier_id already exists or validation fails
            - 401 if not authenticated
            - 403 if user lacks admin role
    """
    logger.info(f"Admin {admin_user.email} creating tier {tier_data.tier_id}")

    try:
        tier = await service.create_tier(tier_data, admin_user)
        return tier
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating tier: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/tiers", response_model=List[QuotaTier])
async def list_tiers(
    enabled_only: bool = False,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    List all quota tiers (admin only).

    Args:
        enabled_only: If True, only return enabled tiers
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        List of quota tiers
    """
    logger.info(f"Admin {admin_user.email} listing tiers (enabled_only={enabled_only})")

    try:
        tiers = await service.list_tiers(enabled_only=enabled_only)
        return tiers
    except Exception as e:
        logger.error(f"Error listing tiers: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/tiers/{tier_id}", response_model=QuotaTier)
async def get_tier(
    tier_id: str,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Get quota tier by ID (admin only).

    Args:
        tier_id: Tier identifier
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Quota tier

    Raises:
        HTTPException:
            - 404 if tier not found
    """
    logger.info(f"Admin {admin_user.email} getting tier {tier_id}")

    tier = await service.get_tier(tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail=f"Tier '{tier_id}' not found")

    return tier


@router.patch("/tiers/{tier_id}", response_model=QuotaTier)
async def update_tier(
    tier_id: str,
    updates: QuotaTierUpdate,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Update quota tier (admin only).

    Args:
        tier_id: Tier identifier
        updates: Partial tier updates
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Updated quota tier

    Raises:
        HTTPException:
            - 404 if tier not found
    """
    logger.info(f"Admin {admin_user.email} updating tier {tier_id}")

    try:
        tier = await service.update_tier(tier_id, updates, admin_user)
        if not tier:
            raise HTTPException(status_code=404, detail=f"Tier '{tier_id}' not found")
        return tier
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating tier: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/tiers/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tier(
    tier_id: str,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Delete quota tier (admin only).

    Validates that tier is not in use by any assignments before deletion.

    Args:
        tier_id: Tier identifier
        admin_user: Authenticated admin user
        service: Quota admin service

    Raises:
        HTTPException:
            - 400 if tier is in use
            - 404 if tier not found
    """
    logger.info(f"Admin {admin_user.email} deleting tier {tier_id}")

    try:
        success = await service.delete_tier(tier_id, admin_user)
        if not success:
            raise HTTPException(status_code=404, detail=f"Tier '{tier_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting tier: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== Quota Assignments ==========

@router.post("/assignments", response_model=QuotaAssignment, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    assignment_data: QuotaAssignmentCreate,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Create a new quota assignment (admin only).

    Args:
        assignment_data: Assignment configuration
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Created quota assignment

    Raises:
        HTTPException:
            - 400 if validation fails or tier not found
            - 401 if not authenticated
            - 403 if user lacks admin role
    """
    logger.info(
        f"Admin {admin_user.email} creating {assignment_data.assignment_type.value} "
        f"assignment for tier {assignment_data.tier_id}"
    )

    try:
        assignment = await service.create_assignment(assignment_data, admin_user)
        return assignment
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating assignment: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/assignments", response_model=List[QuotaAssignment])
async def list_assignments(
    assignment_type: Optional[str] = None,
    enabled_only: bool = False,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    List all quota assignments (admin only).

    Args:
        assignment_type: Optional filter by assignment type
        enabled_only: If True, only return enabled assignments
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        List of quota assignments
    """
    logger.info(
        f"Admin {admin_user.email} listing assignments "
        f"(type={assignment_type}, enabled_only={enabled_only})"
    )

    try:
        assignments = await service.list_assignments(
            assignment_type=assignment_type,
            enabled_only=enabled_only
        )
        return assignments
    except Exception as e:
        logger.error(f"Error listing assignments: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/assignments/{assignment_id}", response_model=QuotaAssignment)
async def get_assignment(
    assignment_id: str,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Get quota assignment by ID (admin only).

    Args:
        assignment_id: Assignment identifier
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Quota assignment

    Raises:
        HTTPException:
            - 404 if assignment not found
    """
    logger.info(f"Admin {admin_user.email} getting assignment {assignment_id}")

    assignment = await service.get_assignment(assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Assignment '{assignment_id}' not found")

    return assignment


@router.patch("/assignments/{assignment_id}", response_model=QuotaAssignment)
async def update_assignment(
    assignment_id: str,
    updates: QuotaAssignmentUpdate,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Update quota assignment (admin only).

    Args:
        assignment_id: Assignment identifier
        updates: Partial assignment updates
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Updated quota assignment

    Raises:
        HTTPException:
            - 400 if validation fails
            - 404 if assignment not found
    """
    logger.info(f"Admin {admin_user.email} updating assignment {assignment_id}")

    try:
        assignment = await service.update_assignment(assignment_id, updates, admin_user)
        if not assignment:
            raise HTTPException(status_code=404, detail=f"Assignment '{assignment_id}' not found")
        return assignment
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating assignment: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assignment(
    assignment_id: str,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Delete quota assignment (admin only).

    Args:
        assignment_id: Assignment identifier
        admin_user: Authenticated admin user
        service: Quota admin service

    Raises:
        HTTPException:
            - 404 if assignment not found
    """
    logger.info(f"Admin {admin_user.email} deleting assignment {assignment_id}")

    try:
        success = await service.delete_assignment(assignment_id, admin_user)
        if not success:
            raise HTTPException(status_code=404, detail=f"Assignment '{assignment_id}' not found")
    except Exception as e:
        logger.error(f"Error deleting assignment: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== User Quota Info (Inspector) ==========

@router.get("/users/{user_id}", response_model=UserQuotaInfo)
async def get_user_quota_info(
    user_id: str,
    email: str = "",
    roles: str = "",
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Get comprehensive quota information for a user (admin only).

    This endpoint provides a complete view of a user's quota status,
    including resolved tier, current usage, and recent block events.

    Args:
        user_id: User identifier
        email: User email (optional, for resolution)
        roles: Comma-separated list of roles (optional, for resolution)
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Comprehensive user quota information
    """
    logger.info(f"Admin {admin_user.email} inspecting quota for user {user_id}")

    try:
        roles_list = [r.strip() for r in roles.split(",")] if roles else []

        info = await service.get_user_quota_info(
            user_id=user_id,
            email=email,
            roles=roles_list
        )
        return info
    except Exception as e:
        logger.error(f"Error getting user quota info: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
