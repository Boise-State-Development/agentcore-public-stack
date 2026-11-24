"""``/users/me/arrangements/{surface}`` — how a user arranges a list page.

``docs/specs/agent-project-organization.md`` §7. Mounted always; every handler
404s while ``ORGANIZE_LISTS_ENABLED`` is off, after authentication, so a
signed-out caller gets 401 whether or not the flag is on.
"""

import logging

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.arrangements.models import Arrangement, ArrangementSurface, ArrangementUpdate
from apis.shared.arrangements.repository import ArrangementRepository, ArrangementStorageUnavailable
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import organize_lists_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/me/arrangements", tags=["arrangements"])

_repository: ArrangementRepository | None = None


def get_arrangement_repository() -> ArrangementRepository:
    """The process-wide repository; overridden in tests."""
    global _repository
    if _repository is None:
        _repository = ArrangementRepository()
    return _repository


async def require_organize_lists_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """The signed-in user, or 404 while the flag is off. Authentication runs first."""
    if not organize_lists_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


@router.get("/{surface}", response_model=Arrangement, response_model_by_alias=True)
async def get_arrangement(
    surface: ArrangementSurface,
    user: User = Depends(require_organize_lists_user),
    repo: ArrangementRepository = Depends(get_arrangement_repository),
) -> Arrangement:
    """The caller's arrangement of ``surface``; the defaults when they have none.

    A read failure answers with the defaults: an arrangement is an enhancement,
    and a page that cannot learn it still lists everything in its usual order.
    """
    try:
        return await repo.get(user.user_id, surface)
    except ClientError:
        logger.warning("Arrangement read failed for %s", surface, exc_info=True)
        return Arrangement()


@router.put("/{surface}", response_model=Arrangement, response_model_by_alias=True)
async def put_arrangement(
    surface: ArrangementSurface,
    body: ArrangementUpdate,
    user: User = Depends(require_organize_lists_user),
    repo: ArrangementRepository = Depends(get_arrangement_repository),
) -> Arrangement:
    """Replace the caller's arrangement of ``surface`` with ``body``."""
    try:
        return await repo.put(user.user_id, surface, body)
    except ArrangementStorageUnavailable:
        logger.error("Arrangement write rejected: DYNAMODB_USER_SETTINGS_TABLE_NAME is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrangement storage is not configured on this server.",
        )
    except ClientError:
        logger.error("Arrangement write failed for %s", surface, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not save your arrangement. Please try again.",
        )
