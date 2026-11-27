"""Warn a schedule's owner before their headless grant lapses (docs/specs/proactive-trust.md §5.1).

A headless grant acts as its user for 30 days from the login it is anchored on
(``HeadlessGrant.ttl``). When it lapses, every scheduled run the user has stops
with ``reauth_required``. The run that minted from the grant already knows when
that is (``RunResult.grant_expires_at``), so warning costs no extra read.

**The rule.** After a run, warn when the grant lapses before the schedule's next
run, or within ``WARN_WINDOW`` (3 days), whichever is later. A 15-minute watcher
warns 3 days ahead; a weekly digest warns at its last run before the lapse, up to
7 days ahead, rather than after a run that already failed. The schedule the
worker holds was re-armed by the dispatcher before the run, so its ``nextRunAt``
is the run after this one.

**Once per epoch.** ``HeadlessGrantService.mark_expiry_warned`` claims the
warning for the grant's ``token_issued_at`` with one conditional write; only the
winner writes the ``connection_expiring`` bell row. A re-pin moves the grant to a
new login, a new epoch, which can warn again when its own window closes.

``grant_expiry_settled`` is the other half: app-api calls it after a re-pin to
mark the bell rows read once the new expiry clears the window.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import ScheduledPrompt

logger = logging.getLogger(__name__)

#: The bell kind for a grant about to lapse.
CONNECTION_EXPIRING_KIND = "connection_expiring"

#: Warn at least this far ahead, whatever the cadence.
WARN_WINDOW = timedelta(days=3)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def warn_deadline(next_run_at: Optional[str], now: datetime) -> datetime:
    """The latest lapse that still warrants a warning: the next run, or ``now + WARN_WINDOW``, whichever is later."""
    floor = now + WARN_WINDOW
    next_run = _parse_iso(next_run_at)
    return max(floor, next_run) if next_run is not None else floor


def needs_expiry_warning(grant_expires_at: Optional[int], next_run_at: Optional[str], now: datetime) -> bool:
    """Whether a grant lapsing at ``grant_expires_at`` (epoch seconds) should be warned about now.

    A grant already lapsed is not warned about here: its next run fails and
    pauses with ``reauth_required``, which already tells the owner.
    """
    if not grant_expires_at:
        return False
    expires = datetime.fromtimestamp(int(grant_expires_at), tz=timezone.utc)
    return now < expires <= warn_deadline(next_run_at, now)


def expiry_clears_window(grant_expires_at: Optional[int], now: Optional[datetime] = None) -> bool:
    """Whether a (re-pinned) grant's expiry is past the warning window, so a warning about it is settled."""
    if not grant_expires_at:
        return False
    now = now or datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(grant_expires_at), tz=timezone.utc) > now + WARN_WINDOW


async def maybe_warn_grant_expiry(
    schedule: ScheduledPrompt,
    result,
    *,
    grants=None,
    notifications=None,
    now: Optional[datetime] = None,
) -> bool:
    """After a run of ``schedule``: warn its owner if the grant ``result`` minted from lapses soon.

    Returns whether a bell row was written. Never raises: a warning must not
    cost the run its bookkeeping.
    """
    try:
        now = now or datetime.now(timezone.utc)
        grant_id = getattr(result, "grant_id", None)
        issued_at = getattr(result, "grant_issued_at", None)
        expires_at = getattr(result, "grant_expires_at", None)
        if not grant_id or not issued_at or not needs_expiry_warning(expires_at, schedule.next_run_at, now):
            return False
        if not schedule.owner_email:
            # The inbox is keyed by email. /schedules shows the same warning
            # from GET /runs/grant, which needs none.
            logger.info("Grant lapses soon for schedule %s, but no owner email is on record", schedule.schedule_id)
            return False
        if grants is None:
            from apis.shared.harness.grants import HeadlessGrantService

            grants = HeadlessGrantService()
        if not await grants.mark_expiry_warned(grant_id, int(issued_at)):
            return False  # Already warned for this login, or the grant moved on.
        from .service import list_scheduled_prompts

        active = [s for s in await list_scheduled_prompts(schedule.user_id) if s.state == "active"]
        if notifications is None:
            from apis.shared.notifications.service import NotificationService

            notifications = NotificationService()
        written = await asyncio.to_thread(
            notifications.notify_own,
            recipient_email=schedule.owner_email,
            kind=CONNECTION_EXPIRING_KIND,
            payload={
                "scope": "grant",
                "expiresAt": _iso(datetime.fromtimestamp(int(expires_at), tz=timezone.utc)),
                "scheduleCount": len(active),
            },
        )
        logger.info("Warned that the headless grant lapses at %s (schedule %s)", expires_at, schedule.schedule_id)
        return written is not None
    except Exception:
        logger.warning("Could not check the headless grant's expiry after a run", exc_info=True)
        return False


def grant_expiry_settled(email: Optional[str], grant_expires_at: Optional[int], *, notifications=None) -> int:
    """Mark the owner's grant-expiry warnings read once a re-pin moved the expiry past the window.

    Returns how many rows it marked. Never raises. Synchronous: callers run it
    off the event loop.
    """
    if not email or not expiry_clears_window(grant_expires_at):
        return 0
    try:
        if notifications is None:
            from apis.shared.notifications.service import NotificationService

            notifications = NotificationService()
        return notifications.mark_read_matching(email, kinds=[CONNECTION_EXPIRING_KIND], payload={"scope": "grant"})
    except Exception:
        logger.warning("Could not settle the grant-expiry warnings", exc_info=True)
        return 0
