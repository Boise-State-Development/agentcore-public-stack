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

from apis.shared.auth import User, require_admin_scope
from .models import (
    FleetFeedbackSummary,
    TopUserCost,
    TopSessionsResponse,
    SystemCostSummary,
    ModelUsageSummary,
    TierUsageSummary,
    CostTrend,
    AdminCostDashboard,
    SessionCostAnatomy,
    SessionProfile,
    UserSessionsResponse,
)
from .service import AdminCostService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs", tags=["admin-costs"])

# Every route in this package is guarded by this one scope, so the
# permission boundary is the package boundary. Enforced by
# tests/architecture/test_admin_scope_coverage.py.
require_costs_admin = require_admin_scope("admin.costs")


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
    admin_user: User = Depends(require_costs_admin),
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


@router.get("/top-sessions", response_model=TopSessionsResponse)
async def get_top_sessions(
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM), defaults to current month",
        pattern=r"^\d{4}-\d{2}$"
    ),
    limit: int = Query(25, ge=1, le=200, description="Maximum sessions to return"),
    users_to_scan: int = Query(
        50,
        ge=1,
        le=500,
        alias="usersToScan",
        description="How many of the period's top-cost users to fan out over"
    ),
    min_cost: Optional[float] = Query(
        None,
        alias="minCost",
        ge=0,
        description="Only include sessions whose lifetime cost is at least this"
    ),
    admin_user: User = Depends(require_costs_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get the most expensive conversations for a period, cost-sorted.

    The support-side counterpart to the per-session quota notice: a runaway
    conversation is visible here before the user calls about a spent quota
    (#833 PR-5). Each row links to the session's cost anatomy
    (`/costs/sessions/{id}/calls`), and carries the session's
    `partialMissCount` / `partialMissUsd` so a *platform* problem is
    distinguishable from a heavy user at a glance.

    ⚠️ **`totalCost` is the session's lifetime cost, not its cost within
    `period`.** `period` selects which sessions are listed (those active in
    it) and which users are scanned; the dollars are the whole conversation,
    which is the number that matters for a thread that spans months. The
    response's `usersScanned` / `truncated` say how deep the scan went — a
    session whose owner sits outside the scanned users is not listed.

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info("Admin requesting top sessions by cost")

    try:
        return await service.get_top_sessions(
            period=period,
            limit=limit,
            users_to_scan=users_to_scan,
            min_cost=min_cost,
        )
    except Exception as e:
        logger.error(f"Error getting top sessions: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve top sessions"
        )


@router.get("/sessions/{session_id}/calls", response_model=SessionCostAnatomy)
async def get_session_cost_anatomy(
    session_id: str,
    admin_user: User = Depends(require_costs_admin),
    service: AdminCostService = Depends(get_cost_service)
):
    """
    Get the per-model-call "cost anatomy" of a session.

    Returns one row per model call — timestamp, input/cacheRead/cacheWrite/
    output tokens, cost, derived cacheStatus (first_write | hit |
    partial_miss | miss_ttl_expired | miss_avoidable | uncached), and the
    prompt-cache prefix fingerprint hashes (toolConfig / system prompt /
    history) — plus session-level rollups (cache efficiency, avoidable-miss
    and partial-miss counts, wastedUsd).

    This is the forensic view for diagnosing avoidable prompt-cache
    re-writes: on a miss_avoidable row, diff its fingerprint hashes against
    the previous row's to see which prefix component changed.

    ⚠️ **A `partial_miss` row costs like a miss even though it read from
    cache.** It means a leading segment (typically tools + system) hit while
    the rest of the prefix was re-written against a live entry — the shape
    that spent 90% of one user's monthly quota while every row read `hit`.
    Its dollars are in `wastedUsd`; `partialMissUsd` / `partialMissCount`
    name the subset.

    ⚠️ **Check `agentSwitched` before treating a miss as a regression.** An
    `@`-mention hands one turn to a different Agent, which genuinely re-writes
    the prefix — it presents exactly like the nondeterministic-ordering bug the
    fingerprints exist to catch (`toolConfigHash` and `systemPromptHash` flipping
    together), so the row says which it was, and `turnAgentId` says which Agent
    ran it. The session rollup splits the same way: `agentSwitchMissCount` /
    `agentSwitchUsd` are a *subset* of `avoidableMissCount` / `wastedUsd`, never
    deducted from them. Subtract to get unexplained waste — that difference is
    what a prefix-stability regression moves.

    Args:
        session_id: Session identifier (any user's session — admin scope)
        admin_user: Authenticated admin user (injected)
        service: Admin cost service (injected)

    Returns:
        SessionCostAnatomy with chronological per-call rows

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 404 if the session has no cost records
            - 500 if server error
    """
    logger.info("Admin requesting session cost anatomy")

    try:
        anatomy = await service.get_session_cost_anatomy(session_id)
    except Exception as e:
        logger.error(f"Error getting session cost anatomy: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve session cost anatomy"
        )

    if not anatomy.calls:
        raise HTTPException(
            status_code=404,
            detail=f"No cost records found for session {session_id}"
        )

    return anatomy


@router.get("/users/{user_id}/sessions", response_model=UserSessionsResponse)
async def get_user_sessions(
    user_id: str,
    period: Optional[str] = Query(
        None,
        description="Period (YYYY-MM) to scope the list to; defaults to the current month",
        pattern=r"^\d{4}-\d{2}$",
    ),
    all_time: bool = Query(
        False,
        alias="allTime",
        description="List every conversation regardless of period (period is then ignored)",
    ),
    sort: str = Query(
        "cost",
        pattern=r"^(cost|recent|context|messages)$",
        description="cost (desc), recent (lastMessageAt desc), context (desc), messages (desc)",
    ),
    limit: int = Query(100, ge=1, le=500, description="Maximum rows to return"),
    admin_user: User = Depends(require_costs_admin),
    service: AdminCostService = Depends(get_cost_service),
):
    """
    One user's conversations, content-free, for the admin user page.

    The drill-down that was missing between "top users by cost" and the
    per-session cost anatomy: it starts from a *user* — the person support is
    looking at because they hit their quota — and lists what they were doing
    without reading any of it. Each row is counts, tokens, dollars, timestamps,
    a model id, a tool count, an agent-binding flag and the diagnoses that
    fired on the row's own numbers; nothing on it carries user text or
    model-generated prose (no title, no summary).

    ⚠️ **`costKnown=false` rows are unrecorded, not free.** 20% of session rows
    fleet-wide have never had a cost aggregate written. They are listed, they
    trail under cost-sort, and `unknownCostCount` says how many there are so a
    total over this list is understood as a floor.

    Period semantics match `/top-sessions`: `period` selects which sessions
    are listed (active in it) and supplies the denominator for
    `shareOfUserPeriod`; a row's `totalCost` is the conversation's **lifetime**
    cost, because a runaway thread usually spans period boundaries.

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks the admin.costs scope
            - 500 if server error
    """
    logger.info("Admin requesting a user's conversation list")

    try:
        return await service.get_user_sessions(
            user_id=user_id,
            period=None if all_time else period,
            all_time=all_time,
            sort=sort,
            limit=limit,
        )
    except Exception as e:
        logger.error(f"Error getting user sessions: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve user sessions"
        )


@router.get("/sessions/{session_id}/profile", response_model=SessionProfile)
async def get_session_profile(
    session_id: str,
    admin_user: User = Depends(require_costs_admin),
    service: AdminCostService = Depends(get_cost_service),
):
    """
    The content-free diagnostic profile of one conversation.

    Sits beside `/sessions/{id}/calls` (the cost anatomy) and answers the
    question that view cannot: *what was the user doing?* Attachments (count
    and bytes, never names), the context-token trajectory per model call with
    the compaction threshold to read it against, the model mix, how often each
    cached-prefix component changed, the tool census when recorded, and the
    diagnoses that fired — each a named, computable classification with its
    evidence and the fix a prior investigation landed on.

    `dataCoverage` says which optional signals this session actually has; a
    session written before a counter shipped reads "not tracked", never "0".

    The whole payload is safe to paste into a model for a second opinion — it
    is content-free by construction (`apis.shared.observability.content_policy`).

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks the admin.costs scope
            - 404 if the session has no metadata row
            - 500 if server error
    """
    logger.info("Admin requesting a session profile")

    try:
        profile = await service.get_session_profile(session_id)
    except Exception as e:
        logger.error(f"Error getting session profile: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve session profile"
        )

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"No session found for {session_id}"
        )

    return profile


@router.get("/feedback", response_model=FleetFeedbackSummary)
async def get_fleet_feedback(
    period: Optional[str] = Query(None, description="Billing period YYYY-MM (default: current)"),
    users_to_scan: int = Query(50, ge=1, le=200, description="Top-cost users to fan out over"),
    sessions_to_scan: int = Query(200, ge=1, le=1000, description="Cap on feedback-bearing sessions joined"),
    admin_user: User = Depends(require_costs_admin),
    service: AdminCostService = Depends(get_cost_service),
):
    """
    The fleet's outcome signal for a billing period — thumbs joined to the
    configuration that produced each answer.

    A thumb on its own says nothing; joined to its `C#` cost row it becomes a
    *labelled example* of "this configuration produced a bad answer". That is
    what this endpoint returns, aggregated:

    - `byTurnClass` — down-thumb rate for **full** (document inline) vs
      **digestOnly** vs **retrieved** turns. The document-offload spec's
      quality question, answerable from stored rows: if `digestOnly` is
      thumbed down materially more than `full`, the digest is too thin and the
      answer is to widen pinning or raise the digest budget.
    - `reasons` — the closed-set split of *why* (`wrong`, `instructions`,
      `length`, `tool_failed`, `outdated`, `other`). "40% are tool_failed"
      points at a fix in a way a bare count never does.
    - `retried` / `reworkUsd` — what bad answers actually cost, in the same
      units as the rest of the cost surface.

    **This is not a quality score, by design.** Every rate carries its `n` and
    is withheld below a floor; `coverage` reports what share of assistant
    calls were ever thumbed (1-5% is the expected band). Thumbs are
    self-selected and skew negative, so the absolute rate is noise — the value
    is *comparison between arms*, and the sampling of turns worth judging.
    `tracked: false` means no session in the period carried a feedback
    rollup — "not tracked", never "nobody complained".

    Assembled from the period's top-cost users (`PeriodCostIndex`), filtered
    to sessions whose `S#` rollups show thumbs, then joined per session.
    `sessionsScanned` / `truncated` say how much of the fleet this covered, so
    a truncated sweep never reads as the whole picture.

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks the admin.costs scope
            - 500 if server error
    """
    logger.info("Admin requesting the fleet feedback summary")

    try:
        return await service.get_fleet_feedback(
            period=period,
            users_to_scan=users_to_scan,
            sessions_to_scan=sessions_to_scan,
        )
    except Exception as e:
        logger.error(f"Error getting fleet feedback summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve fleet feedback summary"
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
    admin_user: User = Depends(require_costs_admin),
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
    admin_user: User = Depends(require_costs_admin),
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
    admin_user: User = Depends(require_costs_admin),
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
    admin_user: User = Depends(require_costs_admin),
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
    admin_user: User = Depends(require_costs_admin),
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
    admin_user: User = Depends(require_costs_admin),
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
