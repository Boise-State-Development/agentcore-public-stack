"""Admin API routes for user management."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import status as status_codes  # list/search take a ``status`` query param that shadows the module
from typing import Optional, List
import logging

from apis.shared.auth import User, require_admin_scope
from apis.shared.costs.aggregator import CostAggregator
from agents.main_agent.quota.repository import QuotaRepository
from agents.main_agent.quota.resolver import QuotaResolver
from apis.shared.users.repository import UserRepository

from .service import UserAdminService
from .models import (
    UserListResponse,
    UserDetailResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["admin-users"])

# Every route in this package is guarded by this one scope, so the
# permission boundary is the package boundary. Enforced by
# tests/architecture/test_admin_scope_coverage.py.
require_users_admin = require_admin_scope("admin.users")


# ========== Dependencies ==========

def get_user_repository() -> UserRepository:
    """Get user repository instance."""
    return UserRepository()


def get_quota_repository() -> QuotaRepository:
    """Get quota repository instance."""
    return QuotaRepository()


def get_quota_resolver(
    repo: QuotaRepository = Depends(get_quota_repository)
) -> QuotaResolver:
    """Get quota resolver instance."""
    return QuotaResolver(repository=repo)


def get_cost_aggregator() -> CostAggregator:
    """Get cost aggregator instance."""
    return CostAggregator()


def get_user_admin_service(
    user_repo: UserRepository = Depends(get_user_repository),
    cost_aggregator: CostAggregator = Depends(get_cost_aggregator),
    quota_resolver: QuotaResolver = Depends(get_quota_resolver),
    quota_repo: QuotaRepository = Depends(get_quota_repository)
) -> UserAdminService:
    """Get user admin service instance."""
    return UserAdminService(
        user_repository=user_repo,
        cost_aggregator=cost_aggregator,
        quota_resolver=quota_resolver,
        quota_repository=quota_repo
    )


# ========== Routes ==========

_STATUS_PATTERN = "^(all|active|inactive|suspended)$"


@router.get("", response_model=UserListResponse)
async def list_users(
    status: str = Query("active", pattern=_STATUS_PATTERN, description="active, inactive, suspended, or all"),
    domain: Optional[str] = Query(None, max_length=253, description="Filter by email domain"),
    limit: int = Query(25, ge=1, le=100),
    cursor: Optional[str] = Query(None, max_length=2048, description="Pagination cursor"),
    admin_user: User = Depends(require_users_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """
    List users with optional filters.

    - **status**: Filter by user status (active, inactive, suspended, all)
    - **domain**: Filter by email domain (e.g., "example.com")
    - **limit**: Number of results per page (1-100)
    - **cursor**: Pagination cursor from previous response

    Returns:
        UserListResponse with paginated list of users
    """
    logger.info("Admin listing users")

    if not service.enabled:
        logger.warning("User admin service is disabled - no table configured")
        return UserListResponse(users=[], next_cursor=None)

    try:
        return await service.list_users(status=status, domain=domain, limit=limit, cursor=cursor)
    except Exception:
        logger.exception("Admin user list failed")
        raise HTTPException(status_code=status_codes.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list users")


@router.get("/search", response_model=UserListResponse)
async def search_users(
    q: str = Query(..., min_length=1, max_length=254, description="Part of a name, email or user id"),
    status: str = Query("all", pattern=_STATUS_PATTERN, description="active, inactive, suspended, or all"),
    domain: Optional[str] = Query(None, max_length=253, description="Filter by email domain"),
    limit: int = Query(25, ge=1, le=100),
    cursor: Optional[str] = Query(None, max_length=2048, description="Pagination cursor"),
    admin_user: User = Depends(require_users_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """
    Search users by name, email or user id: case-insensitive, contains, typo tolerant.

    Results are ranked exact, prefix, contains, all words, then near misses,
    with the most recent sign-in first within a rank. Every profile for an
    exact email comes first, the live one (most recent login) on top; more
    than one means legacy duplicates.
    """
    logger.info("Admin searching users")

    if not service.enabled:
        logger.warning("User admin service is disabled - no table configured")
        return UserListResponse(users=[], next_cursor=None)

    try:
        return await service.search_users(q, status=status, domain=domain, limit=limit, cursor=cursor)
    except Exception:
        logger.exception("Admin user search failed")
        raise HTTPException(status_code=status_codes.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to search users")


@router.get("/domains/list", response_model=List[str])
async def list_email_domains(
    limit: int = Query(50, ge=1, le=200),
    admin_user: User = Depends(require_users_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """Distinct email domains, the most users first, for the domain filter."""
    logger.info("Admin listing email domains")

    try:
        return await service.list_domains(limit=limit)
    except Exception:
        logger.exception("Admin domain list failed")
        raise HTTPException(status_code=status_codes.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list domains")


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    admin_user: User = Depends(require_users_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """
    Get comprehensive user detail including:
    - Profile information
    - Current month cost summary
    - Quota status
    - Recent quota events

    Args:
        user_id: User identifier

    Returns:
        UserDetailResponse with comprehensive user data

    Raises:
        HTTPException: 404 if user not found
    """
    logger.info("Admin requesting user detail")

    if not service.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User admin service is disabled - no table configured"
        )

    detail = await service.get_user_detail(user_id)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )
    return detail
