"""``/notifications`` — the signed-in user's in-app inbox (shared-projects §5, PR-1.7).

Addressed by the caller's email, the same identity project membership uses, so
an invitation sent before someone ever signed in is waiting when they do.
Not behind ``PROJECTS_ENABLED``: the inbox is generic, and while projects are
off it is simply empty rather than a 404 the SPA's badge would have to handle.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.notifications import Notification, NotificationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_service: Optional[NotificationService] = None


def _svc() -> NotificationService:
    global _service
    if _service is None:
        _service = NotificationService()
    return _service


class NotificationsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    notifications: List[Notification] = Field(..., description="Newest first")
    unread_count: int = Field(..., alias="unreadCount")
    next_cursor: Optional[str] = Field(None, alias="nextCursor")


class MarkAllReadResponse(BaseModel):
    marked: int


@router.get("", response_model=NotificationsResponse, response_model_by_alias=True)
def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None, max_length=64),
    unread_only: bool = Query(False, alias="unreadOnly"),
    user: User = Depends(get_current_user_from_session),
) -> NotificationsResponse:
    try:
        items, next_cursor = _svc().list(user.email, limit=limit, cursor=cursor, unread_only=unread_only)
        unread = _svc().unread_count(user.email)
    except Exception:
        logger.exception("Failed to read notifications")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Notifications are unavailable.")
    return NotificationsResponse(notifications=items, unread_count=unread, next_cursor=next_cursor)


@router.post("/read-all", response_model=MarkAllReadResponse)
def mark_all_read(user: User = Depends(get_current_user_from_session)) -> MarkAllReadResponse:
    return MarkAllReadResponse(marked=_svc().mark_all_read(user.email))


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_read(notification_id: str, user: User = Depends(get_current_user_from_session)) -> None:
    """Mark one notification read. Idempotent; 404 if it is not in the caller's inbox."""
    if not _svc().mark_read(user.email, notification_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
