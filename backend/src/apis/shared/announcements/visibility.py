"""Who sees which announcement — the whole of it, in one pure function.

Spec §D5: **the server computes visibility, the client renders what it is
handed.** The alternative — ship every announcement plus the ack list and
filter in the SPA — puts these rules in two languages, lets them drift, and
leaks announcements to users who were never targeted.

So this module is deliberately dependency-free: no DynamoDB, no FastAPI, no
clock of its own. It takes the announcements, the user's roles, the user's
acks, and `now`, and returns exactly what the response should contain. That
makes the rules table-testable without moto, which is the point — this is
where the logic lives, so this is where the tests are.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from apis.shared.timestamps import from_iso

from .models import SUPPRESSING_RANK, Announcement, AnnouncementAck

logger = logging.getLogger(__name__)

#: Tie-break order when more than one announcement wants the single banner or
#: modal slot. Not a semantic severity scale — it is the loudness the banner
#: colours already imply, used only to decide which of several eligible items
#: goes first. Lower sorts earlier.
SEVERITY_ORDER: Dict[str, int] = {"warning": 0, "success": 1, "info": 2}

_TARGET_EVERYONE = "*"


@dataclass
class VisibleAnnouncement:
    """One announcement plus this user's relationship to it."""

    announcement: Announcement
    #: No ack at all for the current revision — drives the unread dot.
    is_unread: bool
    #: Acked an *earlier* revision but not this one. The panel says "Updated"
    #: rather than plain "New", which is the whole reason acks are keyed by
    #: revision (§D4).
    is_updated: bool


@dataclass
class AnnouncementFeed:
    """Everything ``GET /announcements`` returns, already filtered and capped."""

    panel: List[VisibleAnnouncement] = field(default_factory=list)
    banner: Optional[VisibleAnnouncement] = None
    modal: Optional[VisibleAnnouncement] = None
    unread_count: int = 0

    def contains(self, announcement_id: str) -> bool:
        """Whether this user may act on ``announcement_id`` at all.

        Membership is checked against the **panel**, which holds every eligible
        announcement — the banner and modal are capped subsets of it, and a
        dismissed item leaves those two but stays here.
        """
        return any(v.announcement.announcement_id == announcement_id for v in self.panel)

    def get(self, announcement_id: str) -> Optional[Announcement]:
        for v in self.panel:
            if v.announcement.announcement_id == announcement_id:
                return v.announcement
        return None


def _parse(value: Optional[str], *, what: str) -> Optional[datetime]:
    """Parse a stored timestamp, or None if it is absent or unusable.

    Never raises. Every caller here treats None as "no constraint", so a row
    hand-written into DynamoDB with a broken date fails toward *showing* the
    announcement. That is the recoverable direction — the spec makes the same
    choice for a missing ``created_at`` (§D6), and for the same reason: an
    announcement that appears when it should not is visible and fixable, while
    one that silently never appears is neither.
    """
    if not value:
        return None
    try:
        return from_iso(value)
    except (ValueError, TypeError):
        logger.warning("Unparseable %s on announcement row: %r", what, value)
        return None


def _targets_user(announcement: Announcement, roles: Sequence[str]) -> bool:
    """§D9 — a display filter over ``User.roles``, never an RBAC grant.

    There is no ``can_access_*`` predicate behind this and nothing is inherited;
    it decides what a notice board shows, not what a user may do.
    """
    targets = announcement.target_roles or [_TARGET_EVERYONE]
    if _TARGET_EVERYONE in targets:
        return True
    return bool(set(targets) & set(roles or []))


def _joined_before_publication(
    announcement: Announcement, user_created_at: Optional[datetime]
) -> bool:
    """§D6 — new-user backfill suppression.

    A user who joined *after* an announcement was published does not see it:
    without this, someone signing up eighteen months from now meets a queue of
    modals about features that have always existed from their point of view.

    ``showToNewUsers`` is the deliberate exception, for a standing notice that
    genuinely applies to everyone who ever joins.

    Fallback: no usable ``created_at`` means treat the user as **existing** and
    show the announcement.
    """
    if announcement.show_to_new_users:
        return True
    if user_created_at is None:
        return True
    published_at = _parse(announcement.publish_at, what="publishAt")
    if published_at is None:
        return True
    return published_at > user_created_at


def _within_window(announcement: Announcement, now: datetime) -> bool:
    published_at = _parse(announcement.publish_at, what="publishAt")
    if published_at is not None and published_at > now:
        return False
    expires_at = _parse(announcement.expires_at, what="expiresAt")
    if expires_at is not None and expires_at <= now:
        return False
    return True


def _sort_key_for_slot(visible: VisibleAnnouncement) -> tuple:
    """Highest severity, then oldest ``publishAt`` (§D7).

    Oldest-first drains the queue in the order things happened; whatever loses
    the slot stays eligible for the next page load.
    """
    a = visible.announcement
    published_at = _parse(a.publish_at, what="publishAt")
    return (
        SEVERITY_ORDER.get(a.severity, len(SEVERITY_ORDER)),
        published_at.timestamp() if published_at else 0.0,
        a.announcement_id,
    )


def compute_feed(
    *,
    announcements: Iterable[Announcement],
    user_roles: Sequence[str],
    acks: Iterable[AnnouncementAck],
    now: datetime,
    user_created_at: Optional[str] = None,
) -> AnnouncementFeed:
    """Filter and cap the announcements this user should see.

    Note where the ack check lands. The spec's filter chain lists it as step 5,
    before the caps — but D1 and D2 are explicit that dismissing a loud surface
    keeps the entry in the panel, so a literal reading would delete the durable
    record the whole design is built around. The eligibility filter is
    therefore steps 1–4, and the ack suppression applies **only when choosing
    the banner and the modal**.
    """
    # Every datetime this function compares comes from `from_iso`, which is
    # always tz-aware. A naive `now` would therefore raise on the first
    # comparison rather than mis-sort, so normalize it instead of trusting
    # each caller.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    joined_at = _parse(user_created_at, what="user createdAt")

    # Highest rank this user has recorded per (announcement, revision).
    ranks: Dict[tuple, int] = {}
    revisions_seen: Dict[str, set] = {}
    for ack in acks:
        key = (ack.announcement_id, int(ack.revision))
        ranks[key] = max(ranks.get(key, 0), int(ack.action_rank))
        revisions_seen.setdefault(ack.announcement_id, set()).add(int(ack.revision))

    eligible: List[VisibleAnnouncement] = []
    for a in announcements:
        if a.state != "published":
            continue
        if not _within_window(a, now):
            continue
        if not _targets_user(a, user_roles):
            continue
        if not _joined_before_publication(a, joined_at):
            continue

        current = (a.announcement_id, int(a.revision))
        acked_current = current in ranks
        acked_earlier = any(
            r < int(a.revision) for r in revisions_seen.get(a.announcement_id, ())
        )
        eligible.append(
            VisibleAnnouncement(
                announcement=a,
                is_unread=not acked_current,
                is_updated=not acked_current and acked_earlier,
            )
        )

    # The panel is uncapped and newest-first — it is a list, and a list of five
    # is fine.
    eligible.sort(
        key=lambda v: (
            _parse(v.announcement.publish_at, what="publishAt")
            or datetime.min.replace(tzinfo=timezone.utc),
            v.announcement.announcement_id,
        ),
        reverse=True,
    )

    def _unsuppressed(surface: str) -> List[VisibleAnnouncement]:
        return [
            v
            for v in eligible
            if surface in v.announcement.surfaces
            and ranks.get(
                (v.announcement.announcement_id, int(v.announcement.revision)), 0
            )
            < SUPPRESSING_RANK
        ]

    banner_candidates = sorted(_unsuppressed("banner"), key=_sort_key_for_slot)
    # `requiresAck` first, so a blocking notice is never queued behind an
    # informational one.
    modal_candidates = sorted(
        _unsuppressed("modal"),
        key=lambda v: (not v.announcement.requires_ack, *_sort_key_for_slot(v)),
    )

    return AnnouncementFeed(
        panel=eligible,
        banner=banner_candidates[0] if banner_candidates else None,
        modal=modal_candidates[0] if modal_candidates else None,
        unread_count=sum(1 for v in eligible if v.is_unread),
    )
