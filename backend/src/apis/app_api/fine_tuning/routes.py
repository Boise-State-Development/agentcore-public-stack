"""User-facing routes for fine-tuning."""

from fastapi import APIRouter, Depends
import logging

from apis.shared.auth import User
from apis.shared.auth.dependencies import get_current_user
from .models import FineTuningAccessResponse
from .repository import (
    FineTuningAccessRepository,
    get_fine_tuning_access_repository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fine-tuning", tags=["fine-tuning"])


@router.get("/access", response_model=FineTuningAccessResponse)
async def check_access(
    user: User = Depends(get_current_user),
    repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
):
    """Check if the current user has fine-tuning access and return quota info.

    This endpoint does NOT require fine-tuning access — it is used by
    the frontend to decide whether to show the fine-tuning UI.
    """
    grant = repo.check_and_reset_quota(user.email)

    if grant is None:
        return FineTuningAccessResponse(has_access=False)

    return FineTuningAccessResponse(
        has_access=True,
        monthly_quota_hours=grant["monthly_quota_hours"],
        current_month_usage_hours=grant["current_month_usage_hours"],
        quota_period=grant["quota_period"],
    )
