"""Admin API routes for fine-tuning access management."""

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
from .models import GrantAccessRequest, UpdateQuotaRequest, AccessListResponse

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
    logger.info(f"Admin {admin_user.email} listing fine-tuning access grants")

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
    logger.info(f"Admin {admin_user.email} granting fine-tuning access to {request.email}")

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
    logger.info(f"Admin {admin_user.email} getting fine-tuning access for {email}")

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
    logger.info(
        f"Admin {admin_user.email} updating quota for {email} "
        f"to {request.monthly_quota_hours} hours"
    )

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
    logger.info(f"Admin {admin_user.email} revoking fine-tuning access for {email}")

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
    logger.info(f"Admin {admin_user.email} listing all fine-tuning jobs (status={status_filter})")

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
    logger.info(f"Admin {admin_user.email} listing all inference jobs (status={status_filter})")

    try:
        jobs = inf_repo.list_all_inference_jobs(status_filter=status_filter)
        return InferenceJobListResponse(
            jobs=[InferenceJobResponse(**j) for j in jobs],
            total_count=len(jobs),
        )
    except Exception as e:
        logger.error(f"Error listing all inference jobs: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
