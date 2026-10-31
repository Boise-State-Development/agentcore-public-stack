"""Cost API routes

Provides endpoints for retrieving user cost summaries and detailed reports.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from datetime import datetime, timezone
from typing import Optional
import logging

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.costs.models import UserCostSummary
from apis.shared.costs.aggregator import CostAggregator
from apis.app_api.costs.models import (
    QuotaStatusCharged,
    QuotaStatusExtension,
    QuotaStatusOpenRequest,
    UserQuotaStatusResponse,
)
from apis.shared.feature_flags import quota_requests_enabled
from apis.shared.timestamps import utc_now_iso
from agents.main_agent.quota.extensions import (
    active_extensions,
    build_funding_notice,
    effective_limit,
    funding_label,
    is_unlimited,
    period_base_limit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs", tags=["costs"])


def _reset_info(period_type: str) -> str:
    """Human-readable note about when the quota resets (mirrors the SSE path)."""
    now = datetime.now(timezone.utc)
    if period_type == "daily":
        return "Quota resets at midnight UTC"
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    days_remaining = (next_month - now).days
    return f"Quota resets in {days_remaining} day(s)"


@router.get("/quota-status", response_model=UserQuotaStatusResponse)
async def get_quota_status(
    current_user: User = Depends(get_current_user_from_session),
) -> UserQuotaStatusResponse:
    """Get the authenticated user's own quota status for the current period.

    Read-only: resolves the caller's tier and current-period spend and returns
    the denominator (limit) behind the usage/spend numbers already shown on the
    Usage page. Unlike the chat path's ``QuotaChecker.check_quota``, this does
    NOT record any warning/block events — a status read must have no side
    effects.

    Never 500s on a resolution/aggregation hiccup: it degrades to
    ``configured=false`` so the UI simply omits the bar rather than erroring.
    """
    # Imported lazily so this read path doesn't pull the quota singletons into
    # every costs import.
    from apis.shared.quota import get_quota_resolver, get_cost_aggregator

    logger.info("GET /costs/quota-status")

    try:
        resolved = await get_quota_resolver().resolve_user_quota(current_user)
    except Exception as e:  # noqa: BLE001 - status read must not surface 500s
        logger.warning(f"Quota resolution failed for quota-status: {e}")
        resolved = None

    if not resolved or not resolved.tier:
        # No tier assigned — the UI shows "no quota configured" rather than a
        # bar against a zero denominator.
        return UserQuotaStatusResponse(configured=False)

    tier = resolved.tier
    period_type = tier.period_type or "monthly"
    has_override = resolved.override is not None

    # Unlimited tier/override: usage without a denominator.
    if is_unlimited(tier):
        return UserQuotaStatusResponse(
            configured=True,
            unlimited=True,
            tier_name=tier.tier_name,
            matched_by=resolved.matched_by,
            period_type=period_type,
            has_active_override=has_override,
        )

    # Determine the base limit + the period to aggregate spend over.
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m-%d") if period_type == "daily" else now.strftime("%Y-%m")
    base_limit = period_base_limit(tier)
    extensions = active_extensions(resolved.extensions, utc_now_iso())
    limit = effective_limit(base_limit, extensions)

    try:
        summary = await get_cost_aggregator().get_user_cost_summary(
            user_id=current_user.user_id,
            period=period,
        )
        current_usage = float(summary.total_cost)
    except Exception as e:  # noqa: BLE001 - degrade gracefully
        logger.warning(f"Cost aggregation failed for quota-status: {e}")
        current_usage = 0.0

    # The bar runs on the effective limit (base + extensions), so a granted
    # extension shows up in the existing UI before it learns the split.
    denominator = limit if limit is not None else base_limit
    usage_pct = (current_usage / denominator * 100) if denominator > 0 else 0.0
    notice = build_funding_notice(current_usage, base_limit, extensions)

    return UserQuotaStatusResponse(
        configured=True,
        unlimited=False,
        tier_name=tier.tier_name,
        matched_by=resolved.matched_by,
        monthly_limit=denominator,
        current_usage=current_usage,
        remaining=max(0.0, limit - current_usage) if limit is not None else None,
        usage_percentage=round(usage_pct, 1),
        period_type=period_type,
        reset_info=_reset_info(period_type),
        has_active_override=has_override,
        base_limit=base_limit,
        effective_limit=limit,
        extensions=[
            QuotaStatusExtension(
                amount=float(e.amount) if e.amount is not None else None,
                valid_until=e.valid_until,
                funding_mode=e.funding_mode,
                standing=e.standing,
                funding_label=funding_label(e.funding_source) if e.funding_source else None,
            )
            for e in extensions
        ],
        charged=(
            QuotaStatusCharged(
                label=notice.label,
                this_period=float(notice.charged_this_period),
                cap=float(notice.cap) if notice.cap is not None else None,
            )
            if notice
            else None
        ),
        open_request=await _open_request(current_user),
    )


async def _open_request(user: User) -> Optional[QuotaStatusOpenRequest]:
    """The caller's open quota request, while requests are switched on. Never raises."""
    if not quota_requests_enabled():
        return None
    try:
        from apis.shared.quota_requests.service import get_quota_request_service

        request = await get_quota_request_service().open_request_for(user.user_id)
    except Exception as e:  # noqa: BLE001 - a status read must not surface 500s
        logger.warning(f"Open quota request read failed for quota-status: {e}")
        return None
    if request is None:
        return None
    return QuotaStatusOpenRequest(
        request_id=request.request_id,
        status=request.status,
        requested_amount=float(request.requested_amount),
        created_at=request.created_at,
        admin_note=request.admin_note,
    )


@router.get("/summary", response_model=UserCostSummary)
async def get_cost_summary(
    period: Optional[str] = Query(None, description="Period (YYYY-MM), defaults to current month"),
    current_user: User = Depends(get_current_user_from_session)
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
        period = datetime.now(timezone.utc).strftime("%Y-%m")

    logger.info("GET /costs/summary")

    try:
        # Get pre-aggregated summary (O(1) lookup)
        aggregator = CostAggregator()
        summary = await aggregator.get_user_cost_summary(
            user_id=user_id,
            period=period
        )

        logger.info("Successfully retrieved cost summary")

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
    current_user: User = Depends(get_current_user_from_session)
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

    logger.info("GET /costs/detailed-report")

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
