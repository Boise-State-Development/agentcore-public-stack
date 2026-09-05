"""Service layer for feature announcements.

Thin over the repository, with the policy that must not live in a route
handler because both the admin surface and the user surface go through it:

  - ``panel`` is forced into ``surfaces`` (§D1) — dismissing a loud surface can
    never destroy the information.
  - ack TTLs are derived from the announcement, not supplied by the caller
    (§5), so a client cannot pick its own retention.
  - the user feed is assembled here, so ``GET /announcements`` and the ack
    endpoint's 404 check answer "can this user see it?" the same way.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from .models import (
    Announcement,
    AnnouncementAck,
    AnnouncementCreate,
    AnnouncementUpdate,
)
from .repository import AnnouncementsRepository, get_announcements_repository
from .visibility import AnnouncementFeed, compute_feed

logger = logging.getLogger(__name__)

#: A published announcement can only come from these; archived is terminal.
PUBLISHABLE_STATES = frozenset({"draft", "scheduled", "published"})

#: Canonical surface order, so the stored list is deterministic regardless of
#: what order the admin form submitted.
_SURFACE_ORDER = ("panel", "banner", "modal")


def _normalize_surfaces(surfaces: Optional[List[str]]) -> List[str]:
    """Force ``panel`` on and put the list in canonical order (§D1)."""
    chosen = set(surfaces or [])
    chosen.add("panel")
    return [s for s in _SURFACE_ORDER if s in chosen]


class AnnouncementsService:
    def __init__(self, repository: AnnouncementsRepository):
        self._repo = repository

    @property
    def enabled(self) -> bool:
        return self._repo.enabled

    # ── Announcements ────────────────────────────────────────────────────

    async def list_announcements(
        self, states: Optional[List[str]] = None
    ) -> List[Announcement]:
        return await self._repo.list_announcements(states=states)

    async def get_announcement(self, announcement_id: str) -> Optional[Announcement]:
        return await self._repo.get_announcement(announcement_id)

    async def create_announcement(
        self, data: AnnouncementCreate, created_by: Optional[str] = None
    ) -> Announcement:
        data = data.model_copy(update={"surfaces": _normalize_surfaces(data.surfaces)})
        return await self._repo.create_announcement(data, created_by=created_by)

    async def update_announcement(
        self, announcement_id: str, updates: AnnouncementUpdate
    ) -> Optional[Announcement]:
        if updates.surfaces is not None:
            updates = updates.model_copy(
                update={"surfaces": _normalize_surfaces(updates.surfaces)}
            )
        return await self._repo.update_announcement(announcement_id, updates)

    async def publish(self, announcement_id: str) -> Optional[Announcement]:
        existing = await self._repo.get_announcement(announcement_id)
        if not existing:
            return None
        if existing.state not in PUBLISHABLE_STATES:
            raise ValueError(
                f"cannot publish an announcement in state '{existing.state}'"
            )
        return await self._repo.set_state(announcement_id, "published")

    async def archive(self, announcement_id: str) -> Optional[Announcement]:
        """Stop showing it. Acks are deliberately kept — the record of who saw
        what outlives the notice."""
        return await self._repo.set_state(announcement_id, "archived")

    async def revise(self, announcement_id: str) -> Optional[Announcement]:
        return await self._repo.bump_revision(announcement_id)

    async def delete_announcement(self, announcement_id: str) -> bool:
        return await self._repo.delete_announcement(announcement_id)

    # ── Acknowledgements ─────────────────────────────────────────────────

    async def record_ack(
        self,
        *,
        user_id: str,
        announcement: Announcement,
        action: str,
        surface: str,
    ) -> bool:
        """Record an ack against the announcement's **current** revision.

        Returns whether the stored rank was raised; False means an
        equal-or-stronger action was already recorded, which is a no-op, not a
        failure (§D2).
        """
        return await self._repo.record_ack(
            user_id=user_id,
            announcement_id=announcement.announcement_id,
            revision=announcement.revision,
            action=action,
            surface=surface,
            ttl=announcement.ack_ttl(action),
        )

    async def list_acks(self, user_id: str) -> List[AnnouncementAck]:
        return await self._repo.list_acks(user_id)

    async def get_ack(
        self, user_id: str, announcement_id: str, revision: int
    ) -> Optional[AnnouncementAck]:
        return await self._repo.get_ack(user_id, announcement_id, revision)

    # ── User-facing feed ─────────────────────────────────────────────────

    async def build_feed(
        self,
        *,
        user_id: str,
        user_roles: Sequence[str],
        user_created_at: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AnnouncementFeed:
        """What this user should see, already filtered and capped (§D5, §D7).

        Two DynamoDB queries — the published announcements and this user's
        acks. Both are tens of items, and neither is on the model call path
        (§D12).
        """
        announcements = await self._repo.list_announcements(states=["published"])
        acks = await self._repo.list_acks(user_id)
        return compute_feed(
            announcements=announcements,
            user_roles=user_roles,
            acks=acks,
            now=now or datetime.now(timezone.utc),
            user_created_at=user_created_at,
        )


_service: Optional[AnnouncementsService] = None


def get_announcements_service() -> AnnouncementsService:
    global _service
    if _service is None:
        _service = AnnouncementsService(get_announcements_repository())
    return _service
