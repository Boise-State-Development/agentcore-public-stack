"""Admin API routes for user management."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Optional, List
import logging

from apis.shared.auth import User, require_admin
from apis.app_api.costs.aggregator import CostAggregator
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

@router.get("", response_model=UserListResponse)
async def list_users(
    status: str = Query("active", description="Filter by status"),
    domain: Optional[str] = Query(None, description="Filter by email domain"),
    limit: int = Query(25, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """
    List users with optional filters.

    - **status**: Filter by user status (active, inactive, suspended)
    - **domain**: Filter by email domain (e.g., "example.com")
    - **limit**: Number of results per page (1-100)
    - **cursor**: Pagination cursor from previous response

    Returns:
        UserListResponse with paginated list of users
    """
    logger.info(f"Admin {admin_user.email} listing users (status={status}, domain={domain})")

    if not service.enabled:
        logger.warning("User admin service is disabled - no table configured")
        return UserListResponse(users=[], next_cursor=None)

    return await service.list_users(
        status=status,
        domain=domain,
        limit=limit,
        cursor=cursor
    )


@router.get("/search", response_model=UserListResponse)
async def search_users(
    email: str = Query(..., description="Email to search (exact match)"),
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """
    Search for a user by exact email match.

    Args:
        email: Email address to search for (case-insensitive)

    Returns:
        UserListResponse with matching user (or empty if not found)
    """
    logger.info(f"Admin {admin_user.email} searching for user by email: {email}")

    if not service.enabled:
        logger.warning("User admin service is disabled - no table configured")
        return UserListResponse(users=[], next_cursor=None)

    user = await service.search_by_email(email)
    if not user:
        return UserListResponse(users=[], next_cursor=None)
    return UserListResponse(users=[user], next_cursor=None)


@router.get("/domains/list", response_model=List[str])
async def list_email_domains(
    limit: int = Query(50, ge=1, le=200),
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_admin_service)
):
    """
    List distinct email domains with user counts.
    Useful for domain filter dropdown.

    Note: Currently returns empty list. Full implementation requires
    maintaining a separate domain aggregation.
    """
    logger.info(f"Admin {admin_user.email} listing email domains")

    return await service.list_domains(limit=limit)


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    admin_user: User = Depends(require_admin),
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
    logger.info(f"Admin {admin_user.email} requesting user detail: {user_id}")

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
