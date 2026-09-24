"""Notification rows.

``PK=INBOX#{email}  SK=NOTIF#{notificationId}`` on the projects table, whose
``ttl`` attribute expires them after ``RETENTION_DAYS``.

Keyed by **email**, not the spec's ``USER#{userId}``: project membership is
email-keyed so that people who have never signed in can be added, and those are
exactly the people an invitation is for. A user id would leave their
invitation with no address until after they had found the project some other way.

``notificationId`` starts with a zero-padded millisecond timestamp, so the sort
key orders the inbox newest-last and the id is safe in a URL path.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RETENTION_DAYS = 90

NotificationKind = Literal[
    "project_invited",
    "project_role_changed",
    "project_removed",
    "project_ownership_transferred",
]


class Notification(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    notification_id: str = Field(..., alias="notificationId")
    recipient_email: str = Field(..., alias="recipientEmail")
    kind: NotificationKind
    project_id: Optional[str] = Field(None, alias="projectId")
    project_name: Optional[str] = Field(None, alias="projectName")
    actor_email: Optional[str] = Field(None, alias="actorEmail")
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(..., alias="createdAt")
    read_at: Optional[str] = Field(None, alias="readAt")
