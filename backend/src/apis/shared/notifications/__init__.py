"""Per-user in-app notifications (docs/specs/shared-projects.md §3.1, PR-1.7).

The first producer is Shared Projects (invited, role changed, removed, made
owner). There is no email: the inbox is in-app only until Phase 4.4.
"""

from .models import Notification, NotificationKind
from .service import NotificationService

__all__ = ["Notification", "NotificationKind", "NotificationService"]
