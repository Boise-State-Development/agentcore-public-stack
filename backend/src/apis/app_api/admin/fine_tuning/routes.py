"""Admin API routes for fine-tuning access management."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
import logging

from apis.shared.auth import User, require_admin
from apis.app_api.fine_tuning.repository import (
    FineTuningAccessRepository,
    get_fine_tuning_access_repository,
)
from apis.app_api.fine_tuning.models import FineTuningAccessGrant
from apis.app_api.fine_tuning.job_repository import (
    FineTuningJobsRepository,
    get_fine_tuning_jobs_repository,
)
from apis.app_api.fine_tuning.job_models import JobResponse, JobListResponse
from apis.app_api.fine_tuning.inference_repository import (
    InferenceRepository,
    get_inference_repository,
)
from apis.app_api.fine_tuning.inference_models import (
    InferenceJobResponse,
    InferenceJobListResponse,
)
from .models import (
    GrantAccessRequest,
    UpdateQuotaRequest,
    AccessListResponse,
    UserCostBreakdown,
    FineTuningCostDashboard,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fine-tuning", tags=["admin-fine-tuning"])


# ========== Dependencies ==========

def get_repository() -> FineTuningAccessRepository:
    return get_fine_tuning_access_repository()


def get_jobs_repository() -> FineTuningJobsRepository:
    return get_fine_tuning_jobs_repository()


def get_inf_repository() -> InferenceRepository:
    return get_inference_repository()


# ========== Access Management ==========

@router.get("/access", response_model=AccessListResponse)
async def list_access(
    admin_user: User = Depends(require_admin),
    repo: FineTuningAccessRepository = Depends(get_repository),
):
    """List all users with fine-tuning access (admin only)."""
    logger.info("Admin listing fine-tuning access grants")

    try:
        grants = repo.list_access()
        return AccessListResponse(
            grants=[FineTuningAccessGrant(**g) for g in grants],
            total_count=len(grants),
        )
    except Exception as e:
        logger.error(f"Error listing fine-tuning access: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/access", response_model=FineTuningAccessGrant, status_code=status.HTTP_201_CREATED)
async def grant_access(
    request: GrantAccessRequest,
    admin_user: User = Depends(require_admin),
    repo: FineTuningAccessRepository = Depends(get_repository),
):
    """Grant fine-tuning access to a user by email (admin only)."""
    logger.info("Admin granting fine-tuning access")

    try:
        grant = repo.grant_access(
            email=request.email,
            granted_by=admin_user.email,
            monthly_quota_hours=request.monthly_quota_hours,
        )
        return FineTuningAccessGrant(**grant)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error granting fine-tuning access: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/access/{email}", response_model=FineTuningAccessGrant)
async def get_access(
    email: str,
    admin_user: User = Depends(require_admin),
    repo: FineTuningAccessRepository = Depends(get_repository),
):
    """Get fine-tuning access info for a specific user (admin only)."""
    logger.info("Admin getting fine-tuning access")

    grant = repo.get_access(email)
    if not grant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No fine-tuning access found for {email}",
        )
    return FineTuningAccessGrant(**grant)


@router.put("/access/{email}", response_model=FineTuningAccessGrant)
async def update_quota(
    email: str,
    request: UpdateQuotaRequest,
    admin_user: User = Depends(require_admin),
    repo: FineTuningAccessRepository = Depends(get_repository),
):
    """Update GPU-hour quota for a user (admin only)."""
    logger.info("Admin updating fine-tuning quota")

    try:
        result = repo.update_quota(email, request.monthly_quota_hours)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No fine-tuning access found for {email}",
            )
        return FineTuningAccessGrant(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating fine-tuning quota: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/access/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_access(
    email: str,
    admin_user: User = Depends(require_admin),
    repo: FineTuningAccessRepository = Depends(get_repository),
):
    """Revoke fine-tuning access for a user (admin only)."""
    logger.info("Admin revoking fine-tuning access")

    try:
        success = repo.revoke_access(email)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No fine-tuning access found for {email}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking fine-tuning access: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== Job Management ==========

@router.get("/jobs", response_model=JobListResponse)
async def list_all_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    admin_user: User = Depends(require_admin),
    jobs_repo: FineTuningJobsRepository = Depends(get_jobs_repository),
):
    """List all fine-tuning jobs across all users (admin only)."""
    logger.info("Admin listing all fine-tuning jobs")

    try:
        jobs = jobs_repo.list_all_jobs(status_filter=status_filter)
        return JobListResponse(
            jobs=[JobResponse(**j) for j in jobs],
            total_count=len(jobs),
        )
    except Exception as e:
        logger.error(f"Error listing all fine-tuning jobs: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== Inference Job Management ==========

@router.get("/inference-jobs", response_model=InferenceJobListResponse)
async def list_all_inference_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    admin_user: User = Depends(require_admin),
    inf_repo: InferenceRepository = Depends(get_inf_repository),
):
    """List all inference jobs across all users (admin only)."""
    logger.info("Admin listing all inference jobs")

    try:
        jobs = inf_repo.list_all_inference_jobs(status_filter=status_filter)
        return InferenceJobListResponse(
            jobs=[InferenceJobResponse(**j) for j in jobs],
            total_count=len(jobs),
        )
    except Exception as e:
        logger.error(f"Error listing all inference jobs: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ========== Cost Dashboard ==========

def _date_range_for_period(period: str) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a YYYY-MM period string."""
    year, month = int(period[:4]), int(period[5:7])
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


@router.get("/costs", response_model=FineTuningCostDashboard)
async def get_cost_dashboard(
    month: Optional[str] = Query(
        None,
        description="Billing period in YYYY-MM format. Defaults to current month.",
        regex=r"^\d{4}-\d{2}$",
    ),
    admin_user: User = Depends(require_admin),
    jobs_repo: FineTuningJobsRepository = Depends(get_jobs_repository),
    inf_repo: InferenceRepository = Depends(get_inf_repository),
):
    """Get aggregated fine-tuning cost dashboard for a billing period.

    Queries the StatusIndex GSI for Completed and Stopped jobs within
    the requested month, then aggregates costs by user in application code.
    """
    period = month or datetime.now(timezone.utc).strftime("%Y-%m")
    safe_period = period.replace("\n", "").replace("\r", "")
    logger.info("Admin requesting fine-tuning cost dashboard for %s", safe_period)

    try:
        start_iso, end_iso = _date_range_for_period(period)

        # Query training jobs (Completed + Stopped) via StatusIndex GSI
        training_completed = jobs_repo.query_jobs_by_status_and_date("Completed", start_iso, end_iso)
        training_stopped = jobs_repo.query_jobs_by_status_and_date("Stopped", start_iso, end_iso)
        all_training = training_completed + training_stopped

        # Query inference jobs (Completed + Stopped) via StatusIndex GSI
        inf_completed = inf_repo.query_jobs_by_status_and_date("Completed", start_iso, end_iso)
        inf_stopped = inf_repo.query_jobs_by_status_and_date("Stopped", start_iso, end_iso)
        all_inference = inf_completed + inf_stopped

        # Aggregate by user email
        user_data: dict[str, dict] = defaultdict(
            lambda: {
                "total_cost_usd": 0.0,
                "total_gpu_hours": 0.0,
                "training_job_count": 0,
                "inference_job_count": 0,
            }
        )

        for job in all_training:
            email = job.get("email", "unknown")
            cost = job.get("estimated_cost_usd") or 0.0
            billable = job.get("billable_seconds") or 0
            user_data[email]["total_cost_usd"] += cost
            user_data[email]["total_gpu_hours"] += billable / 3600
            user_data[email]["training_job_count"] += 1

        for job in all_inference:
            email = job.get("email", "unknown")
            cost = job.get("estimated_cost_usd") or 0.0
            billable = job.get("billable_seconds") or 0
            user_data[email]["total_cost_usd"] += cost
            user_data[email]["total_gpu_hours"] += billable / 3600
            user_data[email]["inference_job_count"] += 1

        # Build per-user breakdowns sorted by cost descending
        users = sorted(
            [
                UserCostBreakdown(
                    email=email,
                    total_cost_usd=round(data["total_cost_usd"], 4),
                    total_gpu_hours=round(data["total_gpu_hours"], 2),
                    training_job_count=data["training_job_count"],
                    inference_job_count=data["inference_job_count"],
                )
                for email, data in user_data.items()
            ],
            key=lambda u: u.total_cost_usd,
            reverse=True,
        )

        total_cost = sum(u.total_cost_usd for u in users)
        total_hours = sum(u.total_gpu_hours for u in users)
        total_training = sum(u.training_job_count for u in users)
        total_inference = sum(u.inference_job_count for u in users)

        return FineTuningCostDashboard(
            period=period,
            total_cost_usd=round(total_cost, 4),
            total_gpu_hours=round(total_hours, 2),
            active_user_count=len(users),
            training_job_count=total_training,
            inference_job_count=total_inference,
            users=users,
        )
    except Exception as e:
        logger.error(f"Error building fine-tuning cost dashboard: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
