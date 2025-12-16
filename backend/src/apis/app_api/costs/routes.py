"""Cost API routes

Provides endpoints for retrieving user cost summaries and detailed reports.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from datetime import datetime
from typing import Optional
import logging

from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from .models import UserCostSummary
from .aggregator import CostAggregator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("/summary", response_model=UserCostSummary)
async def get_cost_summary(
    period: Optional[str] = Query(None, description="Period (YYYY-MM), defaults to current month"),
    current_user: User = Depends(get_current_user)
):
    """
    Get cost summary for the authenticated user (fast path)

    Uses pre-aggregated UserCostSummary table for <10ms response time.

    Args:
        period: Optional period (YYYY-MM), defaults to current month
        current_user: Authenticated user from JWT

    Returns:
        UserCostSummary with pre-aggregated costs

    Example:
        GET /costs/summary?period=2025-01

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id

    # Default to current month
    if not period:
        period = datetime.utcnow().strftime("%Y-%m")

    logger.info(f"GET /costs/summary - User: {user_id}, Period: {period}")

    try:
        # Get pre-aggregated summary (O(1) lookup)
        aggregator = CostAggregator()
        summary = await aggregator.get_user_cost_summary(
            user_id=user_id,
            period=period
        )

        logger.info(f"Successfully retrieved cost summary for user {user_id}, period {period}")

        return summary

    except Exception as e:
        logger.error(f"Error retrieving cost summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve cost summary: {str(e)}"
        )


@router.get("/detailed-report", response_model=UserCostSummary)
async def get_detailed_report(
    start_date: str = Query(..., description="ISO 8601 start date (YYYY-MM-DD)"),
    end_date: str = Query(..., description="ISO 8601 end date (YYYY-MM-DD)"),
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed cost report for custom date range

    Queries MessageMetadata table for detailed breakdown.
    Use this for custom date ranges or when detailed per-message data is needed.

    Args:
        start_date: Start date (ISO 8601)
        end_date: End date (ISO 8601)
        current_user: Authenticated user from JWT

    Returns:
        UserCostSummary with detailed aggregations

    Example:
        GET /costs/detailed-report?start_date=2025-01-01&end_date=2025-01-15

    Raises:
        HTTPException:
            - 400 if date range invalid or exceeds 90 days
            - 401 if not authenticated
            - 500 if server error
    """
    user_id = current_user.user_id

    logger.info(f"GET /costs/detailed-report - User: {user_id}, Start: {start_date}, End: {end_date}")

    try:
        # Parse dates
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)

        # Validate date range (max 90 days for performance)
        if (end - start).days > 90:
            raise HTTPException(
                status_code=400,
                detail="Date range cannot exceed 90 days"
            )

        if start > end:
            raise HTTPException(
                status_code=400,
                detail="Start date must be before end date"
            )

        # Get detailed report (queries message-level data)
        aggregator = CostAggregator()
        summary = await aggregator.get_detailed_cost_report(
            user_id=user_id,
            start_date=start,
            end_date=end
        )

        logger.info(f"Successfully retrieved detailed report for user {user_id}")

        return summary

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Invalid date format: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format. Use YYYY-MM-DD: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Error retrieving detailed report: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve detailed report: {str(e)}"
        )
