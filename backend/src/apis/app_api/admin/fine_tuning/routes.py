"""Admin API routes for fine-tuning access management."""

from fastapi import APIRouter, Depends, HTTPException, status
import logging

from apis.shared.auth import User, require_admin
from apis.app_api.fine_tuning.repository import (
    FineTuningAccessRepository,
    get_fine_tuning_access_repository,
)
from apis.app_api.fine_tuning.models import FineTuningAccessGrant
from .models import GrantAccessRequest, UpdateQuotaRequest, AccessListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fine-tuning", tags=["admin-fine-tuning"])


# ========== Dependencies ==========

def get_repository() -> FineTuningAccessRepository:
    return get_fine_tuning_access_repository()


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
