"""User-facing announcement surface: read the feed, record an acknowledgement.

Cookie session auth on every route (CLAUDE.md's rule — the SPA sends an
httpOnly session cookie, and a Bearer-only dependency here would 401 into the
centralized redirect loop).

Admin authoring lives at ``/admin/announcements``. See
``docs/specs/feature-announcements.md`` §6.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status

from apis.shared.announcements.models import (
    AnnouncementAckRequest,
    AnnouncementFeedResponse,
    UserAnnouncement,
)
from apis.shared.announcements.service import get_announcements_service
from apis.shared.announcements.visibility import VisibleAnnouncement
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import announcements_enabled
from apis.shared.users.repository import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/announcements", tags=["announcements"])


async def require_announcements_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth plus the environment kill switch.

    404 while ``ANNOUNCEMENTS_ENABLED`` is off, so the surface behaves as if it
    were never mounted (the ``memory_spaces`` / ``schedules`` pattern). Checked
    per request rather than at import so a test can flip the flag without a
    module reload.
    """
    if not announcements_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


async def _user_created_at(user_id: str) -> Optional[str]:
    """This user's signup timestamp, for the new-user suppression rule (§D6).

    Returns None if the profile is missing or the lookup fails, which
    ``compute_feed`` reads as "treat them as an existing user and show the
    announcement". Failing toward showing a message is recoverable; failing
    toward silence is not — and a directory blip must not decide what a user
    reads.
    """
    try:
        repo = UserRepository()
        if not repo.enabled:
            return None
        profile = await repo.get_user(user_id)
        return profile.created_at if profile else None
    except Exception:
        logger.warning(
            "Could not read created_at for %s; treating as an existing user",
            user_id,
            exc_info=True,
        )
        return None


def _to_response(visible: Optional[VisibleAnnouncement]) -> Optional[UserAnnouncement]:
    if visible is None:
        return None
    return UserAnnouncement.from_announcement(
        visible.announcement,
        is_unread=visible.is_unread,
        is_updated=visible.is_updated,
    )


@router.get(
    "/",
    response_model=AnnouncementFeedResponse,
    summary="Announcements visible to the current user",
)
async def get_announcements(
    current_user: User = Depends(require_announcements_user),
) -> AnnouncementFeedResponse:
    """Return only what this user should see, already filtered and capped.

    The client renders this; it does not evaluate targeting, dates, or ack
    state (§D5).
    """
    service = get_announcements_service()
    feed = await service.build_feed(
        user_id=current_user.user_id,
        user_roles=current_user.roles or [],
        user_created_at=await _user_created_at(current_user.user_id),
    )
    return AnnouncementFeedResponse(
        panel=[_to_response(v) for v in feed.panel],
        banner=_to_response(feed.banner),
        modal=_to_response(feed.modal),
        unread_count=feed.unread_count,
    )


@router.post(
    "/{announcement_id}/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record an acknowledgement",
)
async def acknowledge_announcement(
    announcement_id: str,
    body: AnnouncementAckRequest,
    current_user: User = Depends(require_announcements_user),
) -> Response:
    """Record ``seen`` / ``dismissed`` / ``acknowledged`` for this user.

    Idempotent and monotonic — a weaker action arriving late is a no-op, not an
    error (§D2), so this returns 204 either way.

    **404 when the id is not visible to this caller**, deliberately not 403:
    an announcement targeted at another role should not have its existence
    confirmed by the error code on a guessed id.
    """
    service = get_announcements_service()
    feed = await service.build_feed(
        user_id=current_user.user_id,
        user_roles=current_user.roles or [],
        user_created_at=await _user_created_at(current_user.user_id),
    )

    announcement = feed.get(announcement_id)
    if announcement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Announcement '{announcement_id}' not found",
        )

    await service.record_ack(
        user_id=current_user.user_id,
        announcement=announcement,
        action=body.action,
        surface=body.surface,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
