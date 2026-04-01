"""FastAPI dependencies for fine-tuning access control and services."""

import os
import logging
from fastapi import Depends, HTTPException, status
from apis.shared.auth import User
from apis.shared.auth.dependencies import get_current_user
from .repository import FineTuningAccessRepository, get_fine_tuning_access_repository

logger = logging.getLogger(__name__)

# Default monthly GPU-hour quota for users without an explicit grant.
# Set to 0 to revert to whitelist-only mode (original behaviour).
DEFAULT_MONTHLY_QUOTA_HOURS = float(
    os.environ.get("FINE_TUNING_DEFAULT_QUOTA_HOURS", "0")
)


async def require_fine_tuning_access(
    user: User = Depends(get_current_user),
    repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
) -> dict:
    """FastAPI dependency that enforces fine-tuning access.

    Behaviour depends on ``FINE_TUNING_DEFAULT_QUOTA_HOURS``:

    * **0 (default / whitelist mode):** Only users with an explicit grant
      in the ``fine-tuning-access`` table are allowed.  Anyone else
      receives a 403.
    * **> 0 (open-access mode):** All authenticated users are allowed.
      On first use, a grant is auto-created with the configured default
      quota so that the existing quota-tracking machinery keeps working.

    Also performs lazy quota period reset if a new month has started.

    Returns the access grant dict.
    """
    grant = repo.check_and_reset_quota(user.email)

    if grant is not None:
        return grant

    # No explicit grant exists for this user.
    if DEFAULT_MONTHLY_QUOTA_HOURS > 0:
        # Open-access mode: auto-provision a grant with the default quota.
        logger.info(
            f"Auto-provisioning fine-tuning access for {user.email} "
            f"with {DEFAULT_MONTHLY_QUOTA_HOURS}h default quota"
        )
        try:
            new_grant = repo.grant_access(
                email=user.email,
                granted_by="system-default",
                monthly_quota_hours=DEFAULT_MONTHLY_QUOTA_HOURS,
            )
            return new_grant
        except ValueError:
            # Race condition: another request created the grant first.
            grant = repo.check_and_reset_quota(user.email)
            if grant is not None:
                return grant

    logger.warning(f"Fine-tuning access denied for {user.email}")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to fine-tuning features. Contact an administrator to request access.",
    )
