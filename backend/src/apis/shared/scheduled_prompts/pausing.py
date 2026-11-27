"""Pausing a schedule because the platform stopped it, and telling its owner.

The scheduled-runs dispatcher and worker stop a schedule on their own for a
few reasons: its runaway guard tripped (``max_runs_per_day_exceeded``), it
failed several times in a row (``repeated_failures``), its owner must sign in
again (``reauth_required``) or reconnect a tool (``oauth_required``), it
reached its daily budget three days in a row (``budget``), or, for
a project schedule, its creator's standing in the project changed.

Every one of those goes through :func:`pause_schedule`, so the owner is always
told. A project schedule goes through ``ProjectSchedules.pause``, which leaves
a run row on the project and writes ``project_schedule_paused``. A personal
schedule writes ``schedule_paused`` to its owner's inbox
(docs/specs/proactive-agent-gaps.md, slice 0). Before this, a personal
schedule that stopped said nothing until its owner happened to open
``/schedules``.

A pause the owner made themselves never comes through here: that is a route,
and nobody needs telling about their own click.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from .models import ScheduledPrompt
from .service import set_schedule_state

logger = logging.getLogger(__name__)

#: The inbox kind for a personal schedule the platform stopped.
SCHEDULE_PAUSED_KIND = "schedule_paused"


async def pause_schedule(
    schedule: ScheduledPrompt, reason: str, *, notifications=None, details: Optional[Dict[str, Any]] = None
) -> bool:
    """Pause ``schedule`` as ``paused_error`` for ``reason`` and tell its owner. Returns whether it paused.

    ``schedule`` is the record as the caller read it before the run. Blocking
    DynamoDB calls run off the event loop. ``details`` adds to a personal
    schedule's bell payload (a budget pause's amounts, ``budget.budget_payload``).
    """
    if schedule.project_id:
        # Imported here: the projects package imports this one.
        from apis.shared.projects.schedules import ProjectSchedules

        return await asyncio.to_thread(ProjectSchedules(notifications=notifications).pause, schedule, reason)

    paused = await set_schedule_state(schedule.user_id, schedule.schedule_id, "paused_error", state_reason=reason)
    if paused:
        await asyncio.to_thread(notify_schedule_paused, schedule, reason, notifications, details)
    return paused


def notify_schedule_paused(
    schedule: ScheduledPrompt, reason: str, service=None, details: Optional[Dict[str, Any]] = None
) -> bool:
    """Write ``schedule_paused`` to the owner's inbox. Never raises; returns whether it wrote.

    ``details`` are extra payload fields (ids, amounts and times only). A budget's
    hold until local midnight writes one too, with ``resumesAt``, though the
    schedule stays active (docs/specs/proactive-trust.md §4.2).

    The inbox is keyed by email. A schedule created before its owner's email
    was recorded has none until the owner next lists their schedules
    (``remember_owner_email``), so its notice is skipped and logged.
    """
    email: Optional[str] = schedule.owner_email
    if not email:
        logger.info(
            "Schedule %s paused (%s): no owner email on record, so no notice", schedule.schedule_id, reason
        )
        return False
    try:
        if service is None:
            from apis.shared.notifications.service import NotificationService

            service = NotificationService()
        written = service.notify_own(
            recipient_email=email,
            kind=SCHEDULE_PAUSED_KIND,
            payload={**(details or {}), "scheduleId": schedule.schedule_id, "label": schedule.label, "reason": reason},
        )
        return written is not None
    except Exception:
        logger.warning("Could not send the pause notice for schedule %s", schedule.schedule_id, exc_info=True)
        return False
