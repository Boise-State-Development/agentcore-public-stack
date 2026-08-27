"""HTTP surface for the owner-facing knowledge base upgrade (Requirements 21, 23).

Four endpoints, all under the assistant that owns the knowledge base:

* ``GET  /assistants/{id}/knowledge-base/upgrade``          — what to render
* ``POST /assistants/{id}/knowledge-base/upgrade``          — opt in
* ``POST /assistants/{id}/knowledge-base/upgrade/retry``    — after a failure
* ``POST /assistants/{id}/knowledge-base/upgrade/notice``   — dismiss the notice

Permission is resolved the same way the documents surface resolves it, through
``resolve_assistant_permission``. The read is allowed for any resolvable
permission and reports ``canUpgrade: false`` to a viewer; the three writes
require owner or editor. Requirement 23.7's "viewers never see the control" is
therefore enforced on the server, with the client's hiding as presentation only.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from apis.app_api.kb_upgrade.models import EnrollResponse, UpgradeStatusResponse
from apis.app_api.kb_upgrade.service import (
    UpgradeUnavailable,
    dismiss_notice,
    enroll,
    get_upgrade_status,
    retry,
)
from apis.shared.assistants.service import resolve_assistant_permission
from apis.shared.auth import User, get_current_user_from_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/assistants/{assistant_id}/knowledge-base/upgrade",
    tags=["knowledge-base-upgrade"],
)

_EDIT_PERMISSIONS = ("owner", "editor")


async def _resolve(assistant_id: str, current_user: User):
    """Return ``(assistant, permission)`` or raise 404.

    A permission of ``None`` from a resolvable assistant means the user has no
    access at all, which is reported as 404 rather than 403 so the endpoint does
    not confirm the existence of an assistant the caller cannot see.
    """
    assistant, permission = await resolve_assistant_permission(
        assistant_id=assistant_id,
        user_id=current_user.user_id,
        user_email=current_user.email,
    )
    if not assistant or not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant not found: {assistant_id}",
        )
    return assistant, permission


async def _require_edit_permission(assistant_id: str, current_user: User):
    """Resolve and require owner|editor. Returns ``(assistant, permission)``."""
    assistant, permission = await _resolve(assistant_id, current_user)
    if permission not in _EDIT_PERMISSIONS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to upgrade this knowledge base",
        )
    return assistant, permission


@router.get("", response_model=UpgradeStatusResponse)
async def read_upgrade_status(
    assistant_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> UpgradeStatusResponse:
    """What the upgrade card should render, or ``phase: "none"`` for nothing."""
    _, permission = await _resolve(assistant_id, current_user)
    try:
        return await get_upgrade_status(
            assistant_id, can_edit=permission in _EDIT_PERMISSIONS
        )
    except Exception as exc:  # noqa: BLE001 — see below
        # Fail to "nothing to show" rather than 500. This endpoint is decoration
        # on a working page: a knowledge base that cannot be described still
        # serves retrieval, and taking the whole documents section down over an
        # unavailable upgrade card would be a strictly worse outcome. Logged at
        # error so the failure is not silent.
        logger.error(
            f"kb {assistant_id}: could not derive upgrade status: {exc}",
            exc_info=True,
        )
        return UpgradeStatusResponse(phase="none", canUpgrade=False)


@router.post("", response_model=EnrollResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_upgrade(
    assistant_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> EnrollResponse:
    """Opt this knowledge base into the upgrade (Requirement 23.2, 23.8).

    202 rather than 200: enrolment queues work for the migration worker and
    returns before any of it has happened.
    """
    assistant, _ = await _require_edit_permission(assistant_id, current_user)
    try:
        return await enroll(
            assistant_id,
            owner_user_id=assistant.owner_id,
            visibility=str(getattr(assistant, "visibility", "PRIVATE") or "PRIVATE"),
        )
    except UpgradeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


@router.post(
    "/retry", response_model=EnrollResponse, status_code=status.HTTP_202_ACCEPTED
)
async def retry_upgrade(
    assistant_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> EnrollResponse:
    """Restart a failed upgrade on a fresh generation (Requirement 23.5)."""
    assistant, _ = await _require_edit_permission(assistant_id, current_user)
    try:
        return await retry(assistant_id, owner_user_id=assistant.owner_id)
    except UpgradeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


@router.post("/notice", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_upgrade_notice(
    assistant_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> None:
    """Dismiss the one-time post-upgrade notice (Requirement 23.4)."""
    await _require_edit_permission(assistant_id, current_user)
    await dismiss_notice(assistant_id)
