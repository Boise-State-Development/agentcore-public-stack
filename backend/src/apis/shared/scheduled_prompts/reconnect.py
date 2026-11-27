"""Resume the schedules a lapsed connector stopped, once the user reconnects it.

A run that needs a connector the user hasn't connected (or whose token lapsed)
pauses its schedule as ``paused_error`` / ``oauth_required``, with
``lastError = "provider={id} authorization_url=..."`` (the scheduled-runs
worker). Before this, reconnecting the tool left the schedule paused until the
user also pressed Resume on ``/schedules``. ``complete_consent`` now calls
``resume_after_reconnect`` once the vault holds the token
(docs/specs/proactive-trust.md §5.3), as it already resumes KB sync policies.

* **Only that provider's pauses.** The ``provider=`` field of ``lastError`` is
  parsed whole and compared exactly, so reconnecting ``github`` never resumes a
  schedule paused for ``github-enterprise``.
* **Resumed as the Resume button does.** ``resume_next_run_at`` is that one
  function: the cadence from now, never inside a budget hold (§4.2).
* **Personal schedules only.** A project schedule is left to its project's own
  resume, which re-checks the creator's standing there.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

from .budget import respect_hold, utc_now
from .models import ScheduledPrompt
from .service import compute_next_run_at, interval_to_minutes, list_scheduled_prompts, set_schedule_state

logger = logging.getLogger(__name__)

_PROVIDER_FIELD = re.compile(r"(?:^|\s)provider=(\S+)")


def resume_next_run_at(schedule: ScheduledPrompt) -> str:
    """A resumed schedule's ``nextRunAt``: its cadence from now, never inside a budget hold."""
    next_run_at = compute_next_run_at(
        schedule.cadence,
        schedule.hour_local,
        schedule.timezone,
        weekday=schedule.weekday,
        interval_minutes=interval_to_minutes(schedule.interval_value, schedule.interval_unit),
    )
    return respect_hold(schedule, next_run_at, utc_now())


def paused_for_provider(schedule: ScheduledPrompt, provider_id: str) -> bool:
    """Whether ``schedule`` is a personal schedule the worker paused waiting on ``provider_id``."""
    if schedule.project_id or schedule.state != "paused_error" or schedule.state_reason != "oauth_required":
        return False
    match = _PROVIDER_FIELD.search(schedule.last_error or "")
    return bool(match) and match.group(1) == provider_id


async def resume_after_reconnect(
    user_id: str, provider_id: str, *, email: Optional[str] = None, notifications=None
) -> List[str]:
    """Resume ``user_id``'s schedules paused for ``provider_id``, and settle their bell rows.

    Returns the resumed schedule ids. One Query on the user's ``SCHEDPROMPT#``
    items, which ``GET /schedules`` already makes, plus one write per resumed
    schedule. A schedule that fails to resume is logged and left paused.
    """
    if not provider_id:
        return []
    resumed: List[str] = []
    for schedule in await list_scheduled_prompts(user_id):
        if not paused_for_provider(schedule, provider_id):
            continue
        try:
            if await set_schedule_state(user_id, schedule.schedule_id, "active", next_run_at=resume_next_run_at(schedule)):
                resumed.append(schedule.schedule_id)
        except Exception:
            logger.warning("Could not resume schedule %s after a reconnect", schedule.schedule_id, exc_info=True)
    if resumed and email:
        if notifications is None:
            from apis.shared.notifications.service import NotificationService

            notifications = NotificationService()
        for schedule_id in resumed:
            try:
                await asyncio.to_thread(
                    notifications.mark_read_matching,
                    email,
                    kinds=["schedule_paused"],
                    payload={"scheduleId": schedule_id},
                )
            except Exception:
                logger.warning("Could not settle the pause notice for schedule %s", schedule_id, exc_info=True)
    if resumed:
        logger.info("Reconnecting a provider resumed %d schedule(s)", len(resumed))
    return resumed
