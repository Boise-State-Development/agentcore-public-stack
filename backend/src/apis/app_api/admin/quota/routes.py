"""Admin API routes for quota management."""

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
import logging
from apis.shared.auth import User, require_admin
from apis.app_api.costs.aggregator import CostAggregator
from agents.main_agent.quota.repository import QuotaRepository
from agents.main_agent.quota.resolver import QuotaResolver
from agents.main_agent.quota.models import QuotaTier, QuotaAssignment, QuotaOverride, QuotaEvent
from .service import QuotaAdminService
from .models import (
    QuotaTierCreate,
    QuotaTierUpdate,
    QuotaAssignmentCreate,
    QuotaAssignmentUpdate,
    UserQuotaInfo,
    QuotaOverrideCreate,
    QuotaOverrideUpdate
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
    logger.info("Admin creating tier")

    try:
        tier = await service.create_tier(tier_data, admin_user)
        return tier
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error creating tier")
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
    logger.info("Admin listing tiers")

    try:
        tiers = await service.list_tiers(enabled_only=enabled_only)
        return tiers
    except Exception as e:
        logger.error("Error listing tiers")
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
    logger.info("Admin getting tier")

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
    logger.info("Admin updating tier")

    try:
        tier = await service.update_tier(tier_id, updates, admin_user)
        if not tier:
            raise HTTPException(status_code=404, detail=f"Tier '{tier_id}' not found")
        return tier
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error updating tier")
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
    logger.info("Admin deleting tier")

    try:
        success = await service.delete_tier(tier_id, admin_user)
        if not success:
            raise HTTPException(status_code=404, detail=f"Tier '{tier_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error deleting tier")
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
    logger.info("Admin creating assignment")

    try:
        assignment = await service.create_assignment(assignment_data, admin_user)
        return assignment
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error creating assignment")
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
    logger.info("Admin listing assignments")

    try:
        assignments = await service.list_assignments(
            assignment_type=assignment_type,
            enabled_only=enabled_only
        )
        return assignments
    except Exception as e:
        logger.error("Error listing assignments")
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
    logger.info("Admin getting assignment")

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
    logger.info("Admin updating assignment")

    try:
        assignment = await service.update_assignment(assignment_id, updates, admin_user)
        if not assignment:
            raise HTTPException(status_code=404, detail=f"Assignment '{assignment_id}' not found")
        return assignment
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error updating assignment")
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
    logger.info("Admin deleting assignment")

    try:
        success = await service.delete_assignment(assignment_id, admin_user)
        if not success:
            raise HTTPException(status_code=404, detail=f"Assignment '{assignment_id}' not found")
    except Exception as e:
        logger.error("Error deleting assignment")
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
    logger.info("Admin inspecting user quota")

    try:
        roles_list = [r.strip() for r in roles.split(",")] if roles else []

        info = await service.get_user_quota_info(
            user_id=user_id,
            email=email,
            roles=roles_list
        )
        return info
    except Exception as e:
        logger.error("Error getting user quota info")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== Quota Overrides ==========

@router.post("/overrides", response_model=QuotaOverride, status_code=status.HTTP_201_CREATED)
async def create_override(
    override_data: QuotaOverrideCreate,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Create a new quota override (admin only).

    Overrides provide temporary quota exceptions for specific users.
    They take priority over all other quota assignments.

    Args:
        override_data: Override configuration
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Created quota override

    Raises:
        HTTPException:
            - 400 if validation fails
            - 401 if not authenticated
            - 403 if user lacks admin role
    """
    logger.info("Admin creating override")

    try:
        override = await service.create_override(override_data, admin_user)
        return override
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error creating override")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/overrides", response_model=List[QuotaOverride])
async def list_overrides(
    user_id: Optional[str] = None,
    active_only: bool = False,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    List quota overrides (admin only).

    Args:
        user_id: Filter by user ID (optional)
        active_only: Only return currently active overrides
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        List of quota overrides
    """
    logger.info("Admin listing overrides")

    try:
        overrides = await service.list_overrides(
            user_id=user_id,
            active_only=active_only
        )
        return overrides
    except Exception as e:
        logger.error("Error listing overrides")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/overrides/{override_id}", response_model=QuotaOverride)
async def get_override(
    override_id: str,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Get quota override by ID (admin only).

    Args:
        override_id: Override identifier
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Quota override

    Raises:
        HTTPException: 404 if override not found
    """
    logger.info("Admin getting override")

    try:
        override = await service.get_override(override_id)
        if not override:
            raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
        return override
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting override")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/overrides/{override_id}", response_model=QuotaOverride)
async def update_override(
    override_id: str,
    updates: QuotaOverrideUpdate,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Update quota override (admin only).

    Args:
        override_id: Override identifier
        updates: Fields to update
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        Updated quota override

    Raises:
        HTTPException: 404 if override not found
    """
    logger.info("Admin updating override")

    try:
        override = await service.update_override(override_id, updates)
        if not override:
            raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
        return override
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error updating override")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/overrides/{override_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_override(
    override_id: str,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Delete quota override (admin only).

    Args:
        override_id: Override identifier
        admin_user: Authenticated admin user
        service: Quota admin service

    Raises:
        HTTPException: 404 if override not found
    """
    logger.info("Admin deleting override")

    try:
        success = await service.delete_override(override_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting override")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== Quota Events ==========

@router.get("/events", response_model=List[QuotaEvent])
async def get_events(
    user_id: Optional[str] = None,
    tier_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
    admin_user: User = Depends(require_admin),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """
    Get quota events with filters (admin only).

    Args:
        user_id: Filter by user ID (optional)
        tier_id: Filter by tier ID (optional)
        event_type: Filter by event type (warning, block, reset, override_applied)
        limit: Maximum number of events to return (default: 50)
        admin_user: Authenticated admin user
        service: Quota admin service

    Returns:
        List of quota events
    """
    logger.info("Admin getting events")

    try:
        events = await service.get_events(
            user_id=user_id,
            tier_id=tier_id,
            event_type=event_type,
            limit=limit
        )
        return events
    except Exception as e:
        logger.error("Error getting events")
        raise HTTPException(status_code=500, detail="Internal server error")
