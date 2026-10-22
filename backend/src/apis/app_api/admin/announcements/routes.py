"""Admin API routes for feature announcements.

Authoring, scheduling, and lifecycle for the notices users see, plus the
reach numbers for one. The user-facing counterpart is ``GET /announcements``
and its ack endpoint in ``apis/app_api/announcements/``.

See ``docs/specs/feature-announcements.md`` §6.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.announcements.models import (
    AnnouncementCreate,
    AnnouncementState,
    AnnouncementListResponse,
    AnnouncementResponse,
    AnnouncementStatsResponse,
    AnnouncementUpdate,
)
from apis.shared.announcements.service import get_announcements_service
from apis.shared.auth import User, require_admin_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/announcements", tags=["admin-announcements"])

# Every route in this package is guarded by this one scope, so the
# permission boundary is the package boundary. Enforced by
# tests/architecture/test_admin_scope_coverage.py.
require_announcements_admin = require_admin_scope("admin.announcements")


def _not_found(announcement_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Announcement '{announcement_id}' not found",
    )


@router.get(
    "/",
    response_model=AnnouncementListResponse,
    summary="List announcements",
)
async def list_announcements(
    state: Optional[List[AnnouncementState]] = Query(
        None, description="Filter to these states (repeatable)"
    ),
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementListResponse:
    """List every announcement, in every state — drafts included."""
    service = get_announcements_service()
    announcements = await service.list_announcements(states=state)
    return AnnouncementListResponse(
        announcements=[
            AnnouncementResponse.from_announcement(a) for a in announcements
        ],
        total=len(announcements),
    )


@router.get(
    "/{announcement_id}",
    response_model=AnnouncementResponse,
    summary="Get an announcement",
)
async def get_announcement(
    announcement_id: str,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementResponse:
    service = get_announcements_service()
    announcement = await service.get_announcement(announcement_id)
    if not announcement:
        raise _not_found(announcement_id)
    return AnnouncementResponse.from_announcement(announcement)


@router.post(
    "/",
    response_model=AnnouncementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an announcement",
)
async def create_announcement(
    data: AnnouncementCreate,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementResponse:
    """Create an announcement. Defaults to ``draft`` — publishing is its own
    action, so an in-progress edit is never live."""
    try:
        service = get_announcements_service()
        announcement = await service.create_announcement(
            data, created_by=admin_user.email
        )
        return AnnouncementResponse.from_announcement(announcement)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.patch(
    "/{announcement_id}",
    response_model=AnnouncementResponse,
    summary="Update an announcement",
)
async def update_announcement(
    announcement_id: str,
    updates: AnnouncementUpdate,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementResponse:
    """Edit body/title/targeting. ``revision`` is untouched, so an edit does
    **not** re-show the announcement to anyone who already dismissed it — that
    is what ``/revise`` is for (§D4)."""
    try:
        service = get_announcements_service()
        announcement = await service.update_announcement(announcement_id, updates)
        if not announcement:
            raise _not_found(announcement_id)
        return AnnouncementResponse.from_announcement(announcement)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/{announcement_id}/publish",
    response_model=AnnouncementResponse,
    summary="Publish an announcement",
)
async def publish_announcement(
    announcement_id: str,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementResponse:
    try:
        service = get_announcements_service()
        announcement = await service.publish(announcement_id)
        if not announcement:
            raise _not_found(announcement_id)
        return AnnouncementResponse.from_announcement(announcement)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/{announcement_id}/archive",
    response_model=AnnouncementResponse,
    summary="Archive an announcement",
)
async def archive_announcement(
    announcement_id: str,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementResponse:
    """Stop showing it. Acknowledgements are kept."""
    service = get_announcements_service()
    announcement = await service.archive(announcement_id)
    if not announcement:
        raise _not_found(announcement_id)
    return AnnouncementResponse.from_announcement(announcement)


@router.post(
    "/{announcement_id}/revise",
    response_model=AnnouncementResponse,
    summary='Bump the revision ("show this again")',
)
async def revise_announcement(
    announcement_id: str,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementResponse:
    """Increment ``revision``, so everyone's suppression lapses at once (§D4).

    The old acks stay readable under their own revision, which is what lets the
    panel mark the entry *Updated* rather than plain unread.
    """
    service = get_announcements_service()
    announcement = await service.revise(announcement_id)
    if not announcement:
        raise _not_found(announcement_id)
    return AnnouncementResponse.from_announcement(announcement)


@router.delete(
    "/{announcement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an announcement",
)
async def delete_announcement(
    announcement_id: str,
    admin_user: User = Depends(require_announcements_admin),
) -> None:
    service = get_announcements_service()
    deleted = await service.delete_announcement(announcement_id)
    if not deleted:
        raise _not_found(announcement_id)


@router.get(
    "/{announcement_id}/stats",
    response_model=AnnouncementStatsResponse,
    summary="Reach for one announcement",
)
async def get_announcement_stats(
    announcement_id: str,
    admin_user: User = Depends(require_announcements_admin),
) -> AnnouncementStatsResponse:
    """Funnel counts for the announcement's **current** revision.

    ``seen``/``dismissed``/``acknowledged`` are cumulative, not disjoint — a
    user who acknowledged is counted in all three, because the stored rank
    only ever rises through them (§D2).

    Every number is approximate and the UI must say so (§11): the counters are
    incremented on a second write after the ack lands, and ``targeted`` is a
    denominator that moves as people join and roles change. ``targeted`` is
    null when the audience is role-scoped rather than everyone — that means
    "not estimated", not zero.
    """
    service = get_announcements_service()
    stats = await service.get_stats(announcement_id)
    if stats is None:
        raise _not_found(announcement_id)
    return stats
