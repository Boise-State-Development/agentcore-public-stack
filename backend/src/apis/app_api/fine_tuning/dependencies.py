"""FastAPI dependencies for fine-tuning access control."""

import logging
from fastapi import Depends, HTTPException, status
from apis.shared.auth import User
from apis.shared.auth.dependencies import get_current_user
from .repository import FineTuningAccessRepository, get_fine_tuning_access_repository

logger = logging.getLogger(__name__)


async def require_fine_tuning_access(
    user: User = Depends(get_current_user),
    repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
) -> dict:
    """FastAPI dependency that enforces fine-tuning access.

    Checks the user's email against the fine-tuning-access table.
    Also performs lazy quota period reset if a new month has started.

    Returns the access grant dict if the user has access.
    Raises HTTPException 403 if the user is not whitelisted.
    """
    grant = repo.check_and_reset_quota(user.email)

    if grant is None:
        logger.warning(f"Fine-tuning access denied for {user.email}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to fine-tuning features. Contact an administrator to request access.",
        )

    return grant
