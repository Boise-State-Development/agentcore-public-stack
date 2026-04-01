"""Admin cost dashboard API routes.

Provides endpoints for viewing system-wide usage metrics, top users by cost,
model usage breakdowns, and cost trends for administrators.

All endpoints require admin authentication via JWT token with Admin or SuperAdmin role.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional, List
import logging
import csv
import io
import json

from apis.shared.auth import User, require_admin
from .models import (
    TopUserCost,
    SystemCostSummary,
    ModelUsageSummary,
    TierUsageSummary,
    CostTrend,
    AdminCostDashboard,
)
from .service import AdminCostService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs", tags=["admin-costs"])


# ========== Dependencies ==========

def get_cost_service() -> AdminCostService:
    """Get admin cost service instance."""
    return AdminCostService()


# ========== Dashboard Endpoints ==========

@router.get("/dashboard", response_model=AdminCostDashboard)
async def get_cost_dashboard(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM), defaults to current month",
        pattern=r"^\d{4}-\d{2}$"
    ),
    top_users_limit: int = Query(
        100,
        ge=1,
        le=1000,
        alias="topUsersLimit",
        description="Number of top users to return"
    ),
    include_trends: bool = Query(
        True,
        alias="includeTrends",
        description="Include daily trends for the period"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get comprehensive admin cost dashboard.

    Returns:
    - System-wide cost summary for the period
    - Top N users by cost (sorted descending)
    - Model usage breakdown
    - Tier usage breakdown (if quota system enabled)
    - Daily trends (optional)

    Performance: <500ms for 10,000+ users (no table scans)

    Args:
        period: Billing period in YYYY-MM format (defaults to current month)
        top_users_limit: Number of top users to return (1-1000, default 100)
        include_trends: Whether to include daily cost trends
        admin_user: Authenticated admin user (injected)
        service: Admin cost service (injected)

    Returns:
        Complete AdminCostDashboard with all metrics

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin requesting cost dashboard"
    )

    try:
        return await service.get_dashboard(
            period=period,
            top_users_limit=top_users_limit,
            include_trends=include_trends
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting cost dashboard: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve cost dashboard"
        )


@router.get("/top-users", response_model=List[TopUserCost])
async def get_top_users(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM), defaults to current month",
        pattern=r"^\d{4}-\d{2}$"
    ),
    limit: int = Query(100, ge=1, le=1000, description="Maximum users to return"),
    min_cost: Optional[float] = Query(
        None,
        alias="minCost",
        ge=0,
        description="Minimum cost threshold in dollars"
    ),
    tier_id: Optional[str] = Query(
        None,
        alias="tierId",
        description="Filter by quota tier (not yet implemented)"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get top users by cost for a period.

    Uses GSI query for efficient sorted retrieval.
    Performance: <200ms via PeriodCostIndex GSI.

    Args:
        period: Billing period in YYYY-MM format
        limit: Maximum number of users to return (1-1000)
        min_cost: Optional minimum cost threshold
        tier_id: Optional tier ID filter (placeholder)
        admin_user: Authenticated admin user
        service: Admin cost service

    Returns:
        List of TopUserCost sorted by cost descending

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin requesting top users"
    )

    try:
        return await service.get_top_users(
            period=period,
            limit=limit,
            min_cost=min_cost,
            tier_id=tier_id
        )
    except Exception as e:
        logger.error(f"Error getting top users: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve top users"
        )


@router.get("/system-summary", response_model=SystemCostSummary)
async def get_system_summary(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM for monthly, YYYY-MM-DD for daily)"
    ),
    period_type: str = Query(
        "monthly",
        alias="periodType",
        pattern=r"^(daily|monthly)$",
        description="Period type: 'daily' or 'monthly'"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get system-wide cost summary.

    Uses pre-aggregated rollups for <50ms response.

    Args:
        period: Period string (YYYY-MM for monthly, YYYY-MM-DD for daily)
        period_type: Either "daily" or "monthly"
        admin_user: Authenticated admin user
        service: Admin cost service

    Returns:
        SystemCostSummary with aggregated metrics

    Raises:
        HTTPException:
            - 400 if invalid period format
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin requesting system summary"
    )

    try:
        return await service.get_system_summary(
            period=period,
            period_type=period_type
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting system summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve system summary"
        )


@router.get("/by-model", response_model=List[ModelUsageSummary])
async def get_usage_by_model(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM), defaults to current month",
        pattern=r"^\d{4}-\d{2}$"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get cost breakdown by model.

    Returns all models with usage in the period, sorted by cost descending.

    Args:
        period: Billing period in YYYY-MM format
        admin_user: Authenticated admin user
        service: Admin cost service

    Returns:
        List of ModelUsageSummary sorted by cost descending

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin requesting model usage"
    )

    try:
        return await service.get_usage_by_model(period=period)
    except Exception as e:
        logger.error(f"Error getting model usage: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve model usage"
        )


@router.get("/by-tier", response_model=List[TierUsageSummary])
async def get_usage_by_tier(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM), defaults to current month",
        pattern=r"^\d{4}-\d{2}$"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get cost breakdown by quota tier.

    Returns usage statistics per tier, including users at limit.
    Note: Currently returns empty list - tier usage tracking not yet implemented.

    Args:
        period: Billing period in YYYY-MM format
        admin_user: Authenticated admin user
        service: Admin cost service

    Returns:
        List of TierUsageSummary (currently empty placeholder)

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin requesting tier usage"
    )

    try:
        return await service.get_usage_by_tier(period=period)
    except Exception as e:
        logger.error(f"Error getting tier usage: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve tier usage"
        )


@router.get("/trends", response_model=List[CostTrend])
async def get_cost_trends(
    start_date: str = Query(
        ...,
        alias="startDate",
        description="Start date (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    ),
    end_date: str = Query(
        ...,
        alias="endDate",
        description="End date (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get daily cost trends for a date range.

    Returns daily aggregates for charting.
    Max range: 90 days.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        admin_user: Authenticated admin user
        service: Admin cost service

    Returns:
        List of CostTrend sorted by date ascending

    Raises:
        HTTPException:
            - 400 if invalid date format or range > 90 days
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin requesting cost trends"
    )

    try:
        return await service.get_daily_trends(
            start_date=start_date,
            end_date=end_date
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting cost trends: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve cost trends"
        )


@router.get("/export")
async def export_cost_data(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM), defaults to current month",
        pattern=r"^\d{4}-\d{2}$"
    ),
    format: str = Query(
        "csv",
        pattern=r"^(csv|json)$",
        description="Export format: 'csv' or 'json'"
    ),
    admin_user: User = Depends(require_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Export cost data for a period.

    Returns all user costs for the period as CSV or JSON.
    Uses streaming to handle large datasets efficiently.

    Args:
        period: Billing period in YYYY-MM format
        format: Export format ('csv' or 'json')
        admin_user: Authenticated admin user
        service: Admin cost service

    Returns:
        StreamingResponse with CSV or JSON data

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(
        "Admin exporting cost data"
    )

    try:
        # Get all users (up to 1000 for now)
        users = await service.get_top_users(period=period, limit=1000)

        if format == "csv":
            # Generate CSV
            output = io.StringIO()
            writer = csv.writer(output)

            # Write header
            writer.writerow([
                "User ID",
                "Email",
                "Total Cost ($)",
                "Total Requests",
                "Tier",
                "Quota %",
                "Last Updated"
            ])

            # Write data rows
            for user in users:
                writer.writerow([
                    user.user_id,
                    user.email or "",
                    f"{user.total_cost:.2f}",
                    user.total_requests,
                    user.tier_name or "",
                    f"{user.quota_percentage:.1f}" if user.quota_percentage else "",
                    user.last_updated
                ])

            output.seek(0)

            return StreamingResponse(
                iter([output.getvalue()]),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=cost_report_{period or 'current'}.csv"
                }
            )

        else:  # JSON format
            # Serialize users to JSON
            users_data = [user.model_dump(by_alias=True) for user in users]
            json_output = json.dumps(users_data, indent=2)

            return StreamingResponse(
                iter([json_output]),
                media_type="application/json",
                headers={
                    "Content-Disposition": f"attachment; filename=cost_report_{period or 'current'}.json"
                }
            )

    except Exception as e:
        logger.error(f"Error exporting cost data: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to export cost data"
        )
