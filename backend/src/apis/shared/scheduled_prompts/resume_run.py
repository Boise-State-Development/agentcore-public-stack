"""After a paused scheduled run is resumed from the inbox (docs/specs/answer-from-inbox.md §3.6).

Only the scheduled-runs worker calls :func:`finish_resumed_run`, so a run's bookkeeping has one
home: the inbox row's resolution, the run's own history row, the unread dot, and a watcher's
state. Never raises: the resume already happened, and a failed write here must not make the
worker's event retry into a second resume (the start mark would stop it anyway).

| Result | Inbox row | History row |
|---|---|---|
| completed | answered, finished | resolution, finished, tokens added |
| needs_user again | answered, waiting_again; a new row for the new pause | waiting_again, new expiresAt |
| error ``resume_unavailable`` | answered_elsewhere, or expired | answered_elsewhere; unchanged when expired |
| error HTTP 409 (the lease), transport error | retry | unchanged |
| any other error, timeout | answered, failed | failed |
| oauth_required | answered, waiting_again; a row naming the consent | waiting_again |

Every row a resume writes onto adds its ``costUsd`` (docs/specs/proactive-trust.md §4.1); the
worker adds the same cost to the schedule's day (``budget.count_run_cost``) after this returns.

A resume never pauses or resumes the schedule and never moves ``lastRunSessionId``. For a
watcher, a finished resume saves the resumed ``report``'s cursors, tracking and seen keys and
ends the hold in one conditional write, and writes no ``agent_item`` rows (settled question 3).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from apis.shared.harness import RunResult
from apis.shared.notifications.models import Notification, NotificationResolution
from apis.shared.sessions.models import PendingInterrupt
from apis.shared.timestamps import utc_now_iso

from .models import ScheduledPrompt
from .needs_user import live_pending_prompts, needs_user_kind, needs_user_payload, normalize_expiry
from .runs import record_schedule_run_answered_elsewhere, record_schedule_run_resume
from .watch_run import decide_watch_outcome, next_watch_state
from .watch_state import WatchState, clear_watch_pause, save_watch_state, set_watch_pause

logger = logging.getLogger(__name__)


def _label(schedule: Optional[ScheduledPrompt], notification: Notification) -> str:
    if schedule is not None:
        return schedule.label
    label = notification.payload.get("label")
    return label if isinstance(label, str) else ""


async def _load_meta_row(session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    from apis.shared.sessions.metadata import load_session_meta

    return (await load_session_meta(session_id, user_id)).row


def _live_oauth_prompts(
    row: Optional[Dict[str, Any]], provider_ids: List[str]
) -> Tuple[List[PendingInterrupt], Optional[str]]:
    """The session's live OAuth consents for ``provider_ids``, one per provider, and the pause's expiry."""
    if not row or not provider_ids:
        return [], None
    paused = row.get("pausedTurn") or {}
    expires_at = normalize_expiry(paused.get("expiresAt")) if isinstance(paused, dict) else None
    if expires_at is None:
        return [], None
    prompts: List[PendingInterrupt] = []
    seen: set[str] = set()
    for item in row.get("pendingInterrupts") or []:
        try:
            interrupt = PendingInterrupt.model_validate(item)
        except ValidationError:
            continue
        if interrupt.kind == "oauth" and interrupt.provider_id in provider_ids and interrupt.provider_id not in seen:
            seen.add(interrupt.provider_id or "")
            prompts.append(interrupt)
    return prompts, expires_at


async def _set_unread(result: RunResult, user_id: str) -> None:
    try:
        from apis.shared.sessions.metadata import set_session_unread

        await set_session_unread(result.session_id, user_id, True)
    except Exception:
        logger.warning("Inbox resume %s: could not mark the run unread", result.run_id, exc_info=True)


async def _settle_row(
    service: Any, email: str, notification: Notification, resume_run_id: str, resolution: NotificationResolution
) -> None:
    written = await asyncio.to_thread(
        service.set_resolution, email, notification.notification_id, resolution, claimed_by=resume_run_id
    )
    if not written:
        logger.warning(
            "Inbox resume %s: notification %s was claimed again meanwhile; left as it is",
            resume_run_id, notification.notification_id,
        )


async def _record_history(
    schedule: Optional[ScheduledPrompt],
    notification: Notification,
    *,
    resolution: str,
    resume_run_id: str,
    resume_outcome: str,
    result: RunResult,
    expires_at: Optional[str] = None,
    prompt_count: Optional[int] = None,
) -> None:
    run_id = notification.payload.get("runId")
    finished_at = notification.payload.get("runFinishedAt")
    if schedule is None or not isinstance(run_id, str) or not isinstance(finished_at, str):
        return
    await asyncio.to_thread(
        record_schedule_run_resume,
        schedule.user_id,
        schedule.schedule_id,
        run_finished_at=finished_at,
        run_id=run_id,
        resolution=resolution,
        resume_run_id=resume_run_id,
        resumed_at=result.finished_at or utc_now_iso(),
        resume_outcome=resume_outcome,
        usage=result.usage,
        expires_at=expires_at,
        prompt_count=prompt_count,
        cost_usd=result.cost_usd,
        write_count=result.write_count,
    )


async def _mark_history_answered_elsewhere(schedule: Optional[ScheduledPrompt], notification: Notification) -> None:
    run_id = notification.payload.get("runId")
    finished_at = notification.payload.get("runFinishedAt")
    if schedule is None or not isinstance(run_id, str) or not isinstance(finished_at, str):
        return
    await asyncio.to_thread(
        record_schedule_run_answered_elsewhere,
        schedule.user_id,
        schedule.schedule_id,
        run_finished_at=finished_at,
        run_id=run_id,
    )


def _notify_again(
    service: Any,
    email: str,
    schedule: Optional[ScheduledPrompt],
    notification: Notification,
    prompts: List[PendingInterrupt],
    expires_at: Optional[str],
) -> bool:
    """A new needs-user row for the pause the resume ran into, pointing at the same run."""
    payload = notification.payload
    return service.notify_own(
        recipient_email=email,
        kind=needs_user_kind(prompts),
        payload=needs_user_payload(
            schedule_id=str(payload.get("scheduleId") or (schedule.schedule_id if schedule else "")),
            run_id=str(payload.get("runId") or ""),
            run_finished_at=str(payload.get("runFinishedAt") or ""),
            session_id=str(payload.get("sessionId") or ""),
            label=_label(schedule, notification),
            expires_at=expires_at,
            prompts=prompts,
        ),
    ) is not None


async def _finish_watch(
    schedule: ScheduledPrompt, result: RunResult, previous: Optional[WatchState]
) -> None:
    """Save the resumed run's ``report`` and end the hold, in one conditional write."""
    try:
        outcome = decide_watch_outcome(
            result.report_input,
            result.final_message,
            previous.seen if previous is not None else [],
            run_id=result.run_id,
            label=schedule.label,
            write_count=result.write_count,
        )
        saved = await asyncio.to_thread(
            save_watch_state,
            schedule.user_id,
            schedule.schedule_id,
            next_watch_state(previous, outcome, result.finished_at or utc_now_iso()),
            expected_version=previous.version if previous is not None else None,
            clear_pause=True,
        )
        if saved:
            return
        logger.warning(f"Schedule {schedule.schedule_id}: watcher state moved on during the resume; not saved")
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not save the resumed run's state", exc_info=True)
    # Whatever happened to the cursors, this wait is over.
    try:
        await asyncio.to_thread(
            clear_watch_pause, schedule.user_id, schedule.schedule_id, session_id=result.session_id
        )
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not end the watcher's hold", exc_info=True)


async def _hold_again(
    schedule: ScheduledPrompt, result: RunResult, previous: Optional[WatchState], expires_at: str
) -> None:
    try:
        await asyncio.to_thread(
            set_watch_pause,
            schedule.user_id,
            schedule.schedule_id,
            session_id=result.session_id,
            paused_until=expires_at,
            expected_version=previous.version if previous is not None else None,
        )
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not renew the watcher's hold", exc_info=True)


def _is_watch(schedule: Optional[ScheduledPrompt]) -> bool:
    return schedule is not None and schedule.mode == "watch" and not schedule.project_id


async def finish_resumed_run(
    *,
    result: RunResult,
    notification: Notification,
    email: str,
    user_id: str,
    resume_run_id: str,
    schedule: Optional[ScheduledPrompt],
    watch_state: Optional[WatchState] = None,
    service: Any = None,
) -> Dict[str, Any]:
    """Settle the inbox row, the run's history row and a watcher's state from a resume's result.

    ``schedule`` is ``None`` when it was deleted meanwhile: the row still settles, and nothing
    else is written. Returns a summary for the worker's log and tests.
    """
    if service is None:
        from apis.shared.notifications.service import NotificationService

        service = NotificationService()
    decision = (notification.resolution.decision if notification.resolution else None) or "answered"
    now = utc_now_iso()
    summary: Dict[str, Any] = {"resumeRunId": resume_run_id, "status": result.status}

    def resolved(state: str, **fields: Any) -> NotificationResolution:
        return NotificationResolution(state=state, at=now, resume_run_id=resume_run_id, **fields)

    if result.status == "error" and result.error_kind == "resume_unavailable":
        # The Runtime's backstop for a race the pre-check missed: nothing resumed.
        state = "expired" if result.error_reason == "expired" else "answered_elsewhere"
        reason = None if state == "expired" else (result.error_reason or None)
        await _settle_row(service, email, notification, resume_run_id, resolved(state, reason=reason))
        if state == "answered_elsewhere":
            # The bell's list never settles this row now, so the run's history row would keep
            # "Waiting for you" until its pause expired (§3.6 point 1). An expired one reads as
            # expired from its own `expiresAt`.
            await _mark_history_answered_elsewhere(schedule, notification)
        return {**summary, "row": state}

    if result.status == "error" and (result.http_status == 409 or (result.error or "").startswith("transport:")):
        # The user is mid-turn in that conversation, or the Runtime was unreachable: nothing was
        # resumed, so the buttons come back.
        await _settle_row(service, email, notification, resume_run_id, resolved("retry"))
        return {**summary, "row": "retry"}

    if result.status in ("needs_user", "oauth_required"):
        prompts: List[PendingInterrupt] = []
        expires_at: Optional[str] = None
        try:
            row = await _load_meta_row(result.session_id, user_id)
            if result.status == "needs_user":
                prompts, expires_at = live_pending_prompts(row, result.pending_interrupt_ids)
            else:
                prompts, expires_at = _live_oauth_prompts(row, [o.provider_id for o in result.oauth_required])
        except Exception:
            logger.warning("Inbox resume %s: could not read the new pause", resume_run_id, exc_info=True)
        if prompts:
            await _settle_row(
                service, email, notification, resume_run_id,
                resolved("answered", decision=decision, outcome="waiting_again"),
            )
            await _record_history(
                schedule, notification, resolution=decision, resume_run_id=resume_run_id,
                resume_outcome="waiting_again", result=result, expires_at=expires_at, prompt_count=len(prompts),
            )
            await asyncio.to_thread(_notify_again, service, email, schedule, notification, prompts, expires_at)
            await _set_unread(result, user_id)
            if _is_watch(schedule) and expires_at:
                await _hold_again(schedule, result, watch_state, expires_at)
            return {**summary, "row": "answered", "outcome": "waiting_again", "promptCount": len(prompts)}
        # The new pause is already gone (or was a pre-flight consent with nothing to resume):
        # the run is simply done.
        result.status = "completed"

    if result.status == "completed":
        await _settle_row(
            service, email, notification, resume_run_id, resolved("answered", decision=decision, outcome="finished")
        )
        await _record_history(
            schedule, notification, resolution=decision, resume_run_id=resume_run_id,
            resume_outcome="finished", result=result,
        )
        await _set_unread(result, user_id)
        if _is_watch(schedule):
            await _finish_watch(schedule, result, watch_state)
        return {**summary, "row": "answered", "outcome": "finished"}

    # Any other error, or a timeout. The schedule's failure streak is not touched: its next run
    # is independent of this one.
    await _settle_row(
        service, email, notification, resume_run_id, resolved("answered", decision=decision, outcome="failed")
    )
    await _record_history(
        schedule, notification, resolution=decision, resume_run_id=resume_run_id,
        resume_outcome="failed", result=result,
    )
    await _set_unread(result, user_id)
    return {**summary, "row": "answered", "outcome": "failed"}
