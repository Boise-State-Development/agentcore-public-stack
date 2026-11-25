"""Scheduled-runs worker — executes a single schedule's headless run.

Given `{"scheduleId", "userId"}` from the dispatcher (async-invoked,
`InvocationType=Event`), resolves the schedule record, calls
`run_agent_headless(trigger="schedule")` with its snapshotted config, and
records the outcome via `apis.shared.scheduled_prompts.service`.

Governance (docs/specs/scheduled-runs-phase-b-brief.md §1): a scheduled run
executes as the owning user with that user's own RBAC and delivers back to
that same user's session list. Delivery already happens inside
`run_agent_headless` (the runtime persists the session/messages during the
turn) — this worker's only job after the call returns is bookkeeping:
record the outcome, and pause the schedule on the terminal failure modes
below (mirrors the KB-sync worker's pause/breaker outcomes, `apis/app_api/
kb_sync/worker.py`).

Failure handling (brief §2, "Worker" bullet):
  - HeadlessAuthError (no/expired headless grant)  -> paused_error,
    state_reason="reauth_required". Not a failure streak — the user must
    log in again and re-enable; retry-spamming a dead grant is pointless.
  - RunResult.status == "oauth_required" (a connector tool needs (re-)
    consent; a headless run cannot pop a consent window) -> paused_error,
    state_reason="oauth_required". The consent URL is recorded in
    `last_error` for a future B3 surface to render.
  - Any other error/timeout status (and any unexpected exception) ->
    record_run_result(status="error"), which increments the persistent
    ``consecutive_failures`` counter and returns the new streak; after
    SCHEDULED_RUNS_MAX_FAILURES consecutive failures, paused_error,
    state_reason="repeated_failures" (same breaker shape as KB-sync's
    consecutive_failures >= 5).
  - completed -> record_run_result(status="completed", session_id=...),
    which resets the streak to 0.

A schedule that runs in a project (Shared Projects 3.2) also leaves a run row
on the project for its members, and its pauses go through
``ProjectSchedules.pause``, which adds a row and tells the creator and the
project's owner why it stopped. One set to deliver to the project shares each
completed run with it first (``ScheduleDelivery``, 3.2b), so the run row can
point at the share; a delivery that fails leaves the run completed and private.

While ``PROACTIVE_RUNS_ENABLED`` is on, every run of a personal schedule also
leaves one ``SCHEDRUN#`` history row (``record_schedule_run``,
docs/specs/quiet-watcher.md §3.4): completed, error, timeout, or paused (an
OAuth consent or a dead headless grant). While it is off, nothing is written,
and a schedule's ``modelId`` (settable only while it is on) is passed to the
run whenever one is set.

A personal schedule in ``watch`` mode, while the flag is on, is a quiet
watcher (docs/specs/quiet-watcher.md §3.3). Its run is sent with
``run_mode="watch"`` and ``incognito``, and its prompt starts with the
watcher's ``<watch_state>`` block. A completed run is then **quiet** (its
conversation soft-deleted, its ``C#`` cost rows kept, no unread dot, no inbox
row, ``lastRunSessionId`` untouched) or **notifies** (unread dot, one
``agent_item`` inbox row per new finding), by ``watch_run.decide_watch_outcome``.
Either way the state is saved, conditional on the version the run read. A
failed, timed-out or consent-blocked watch run takes the paths above unchanged.
A standard schedule, or any schedule while the flag is off, runs as before.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apis.shared.harness import (
    CognitoRefreshBearerAuth,
    HeadlessAuthError,
    RunResult,
    run_agent_headless,
)
from apis.shared.feature_flags import proactive_runs_enabled
from apis.shared.projects.schedule_delivery import ScheduleDelivery
from apis.shared.projects.schedules import ProjectSchedules
from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.pausing import pause_schedule
from apis.shared.scheduled_prompts.runs import ScheduleRunStatus, record_schedule_run
from apis.shared.scheduled_prompts.watch_run import (
    WatchOutcome,
    build_watch_prompt,
    decide_watch_outcome,
    next_watch_state,
)
from apis.shared.scheduled_prompts.watch_state import (
    WatchState,
    get_watch_state,
    save_watch_state,
)
from apis.shared.scheduled_prompts.service import (
    get_scheduled_prompt,
    record_run_result,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


async def _pause(schedule: ScheduledPrompt, reason: str) -> None:
    """Pause with ``reason`` and tell the owner (``pause_schedule``).

    ``schedule`` is the record as it was before this run, so still "active". A
    project schedule also gets a run row on the project.
    """
    await pause_schedule(schedule, reason)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _record_history(
    schedule: ScheduledPrompt,
    status: ScheduleRunStatus,
    result: Optional[RunResult] = None,
    *,
    item_count: int = 0,
    with_session: bool = True,
) -> None:
    """Leave the run's history row: personal schedules only, and only while the flag is on.

    ``result`` is the run's outcome when there is one; a run that raised before
    returning gets a fresh run id and now. ``with_session=False`` leaves the
    session off the row (a quiet run, whose conversation is gone).
    """
    if schedule.project_id or not proactive_runs_enabled():
        return
    await asyncio.to_thread(
        record_schedule_run,
        schedule,
        run_id=(result.run_id if result is not None else None) or f"run-{uuid.uuid4().hex[:12]}",
        status=status,
        session_id=result.session_id if result is not None and with_session else None,
        finished_at=(result.finished_at if result is not None else None) or _now_iso(),
        item_count=item_count,
        usage=result.usage if result is not None else None,
    )


async def _record_project_run(schedule: ScheduledPrompt, status: str, share_id: Optional[str] = None) -> None:
    if schedule.project_id:
        await asyncio.to_thread(ProjectSchedules().record_run, schedule, status, None, share_id)


async def _record_failure_and_maybe_pause(
    schedule: ScheduledPrompt,
    *,
    error: Optional[str],
    session_id: Optional[str],
    max_failures: int,
    result_label: str,
    result: Optional[RunResult] = None,
) -> Dict[str, Any]:
    """Record an errored run and trip the breaker on a real failure streak.

    ``record_run_result`` maintains the persistent ``consecutive_failures``
    counter (reset on a completed run) and returns the new streak, so the
    breaker fires correctly at any ``max_failures`` — a last-status proxy
    could only ever see one run back and so never reach a threshold > 2.
    """
    streak = await record_run_result(
        schedule.user_id,
        schedule.schedule_id,
        status="error",
        session_id=session_id,
        error=error,
    )
    await _record_project_run(schedule, "timeout" if result_label == "timeout" else "error")
    await _record_history(schedule, "timeout" if result_label == "timeout" else "error", result)
    if streak >= max_failures:
        await _pause(schedule, "repeated_failures")
        logger.warning(
            f"Schedule {schedule.schedule_id}: {streak} consecutive failures "
            f"(max {max_failures}); pausing"
        )
        return {"scheduleId": schedule.schedule_id, "result": "paused_error", "reason": "repeated_failures"}
    return {"scheduleId": schedule.schedule_id, "result": result_label}


async def _pause_reauth(
    schedule: ScheduledPrompt,
    reason: str,
    detail: Optional[str] = None,
    result: Optional[RunResult] = None,
) -> Dict[str, Any]:
    logger.warning(
        f"Schedule {schedule.schedule_id}: pausing ({reason}) for user {schedule.user_id}"
        + (f" — {detail}" if detail else "")
    )
    await record_run_result(schedule.user_id, schedule.schedule_id, status="error", error=detail or reason)
    await _record_history(schedule, "paused", result)
    await _pause(schedule, reason)
    return {"scheduleId": schedule.schedule_id, "result": "paused_error", "reason": reason}


def _is_watch(schedule: ScheduledPrompt) -> bool:
    """A personal schedule in ``watch`` mode while the flag is on; anything else runs as standard."""
    return schedule.mode == "watch" and not schedule.project_id and proactive_runs_enabled()


async def _save_watch_state(
    schedule: ScheduledPrompt,
    previous: Optional[WatchState],
    outcome: WatchOutcome,
    finished_at: str,
) -> None:
    """Save the watcher's next state, conditional on the version the run read. Never raises."""
    try:
        saved = await asyncio.to_thread(
            save_watch_state,
            schedule.user_id,
            schedule.schedule_id,
            next_watch_state(previous, outcome, finished_at),
            expected_version=previous.version if previous is not None else None,
        )
        if not saved:
            logger.warning(f"Schedule {schedule.schedule_id}: watcher state moved on during the run; not saved")
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not save watcher state", exc_info=True)


def _soft_delete_quiet_session(user_id: str, session_id: str) -> None:
    """Soft-delete a quiet run's conversation, keeping its C# cost rows. Never raises."""
    from apis.shared.sessions.soft_delete import soft_delete_session_row

    try:
        if not soft_delete_session_row(user_id, session_id):
            logger.info("Quiet watcher run: no session row to delete")
    except Exception:
        logger.warning("Quiet watcher run: could not delete its session", exc_info=True)


def _notify_items(schedule: ScheduledPrompt, result: RunResult, outcome: WatchOutcome) -> int:
    """One ``agent_item`` inbox row per new item, to the owner. Returns how many were written."""
    email = schedule.owner_email
    if not email:
        logger.info(f"Schedule {schedule.schedule_id}: no owner email on record, so no inbox rows")
        return 0
    from apis.shared.notifications.service import NotificationService

    service = NotificationService()
    written = 0
    for item in outcome.items:
        if service.notify_own(
            recipient_email=email,
            kind="agent_item",
            payload={
                "scheduleId": schedule.schedule_id,
                "runId": result.run_id,
                "sessionId": result.session_id,
                "label": schedule.label,
                "title": item.title,
                "urgency": item.urgency,
                "key": item.key,
            },
        ) is not None:
            written += 1
    return written


async def _finish_watch_run(
    schedule: ScheduledPrompt,
    result: RunResult,
    previous: Optional[WatchState],
) -> Dict[str, Any]:
    """A completed watch run: quiet or notify (spec §3.3), then save the state."""
    outcome = decide_watch_outcome(
        result.report_input,
        result.final_message,
        previous.seen if previous is not None else [],
        run_id=result.run_id,
        label=schedule.label,
    )
    schedule_id = schedule.schedule_id

    if outcome.notify:
        try:
            from apis.shared.sessions.metadata import set_session_unread

            await set_session_unread(result.session_id, schedule.user_id, True)
        except Exception:
            logger.warning(f"Schedule {schedule_id}: could not mark the run unread", exc_info=True)
        await record_run_result(schedule.user_id, schedule_id, status="completed", session_id=result.session_id)
        await asyncio.to_thread(_notify_items, schedule, result, outcome)
        await _record_history(schedule, "notify", result, item_count=len(outcome.items))
        await _save_watch_state(schedule, previous, outcome, result.finished_at)
        logger.info(f"Schedule {schedule_id}: watch run {result.run_id} notified {len(outcome.items)} item(s)")
        return {
            "scheduleId": schedule_id,
            "result": "notify",
            "sessionId": result.session_id,
            "itemCount": len(outcome.items),
        }

    # Quiet: nothing left behind. No session id, so lastRunSessionId still
    # names the last run that had something to show.
    if result.session_id:
        await asyncio.to_thread(_soft_delete_quiet_session, schedule.user_id, result.session_id)
    await record_run_result(schedule.user_id, schedule_id, status="completed")
    await _record_history(schedule, "quiet", result, with_session=False)
    await _save_watch_state(schedule, previous, outcome, result.finished_at)
    logger.info(f"Schedule {schedule_id}: watch run {result.run_id} was quiet")
    return {"scheduleId": schedule_id, "result": "quiet"}


async def run_schedule(payload: Dict[str, Any]) -> Dict[str, Any]:
    schedule_id = payload["scheduleId"]
    user_id = payload["userId"]

    schedule = await get_scheduled_prompt(user_id, schedule_id)
    if schedule is None:
        logger.info(f"Scheduled-runs worker: schedule {schedule_id} no longer exists; dropping run")
        return {"scheduleId": schedule_id, "result": "dropped"}

    run_config: Dict[str, Any] = {}
    if schedule.model_id:
        # Settable only while PROACTIVE_RUNS_ENABLED is on; unset runs on the
        # Agent's or the user's default, exactly as before.
        run_config["model_id"] = schedule.model_id

    watch = _is_watch(schedule)
    watch_state: Optional[WatchState] = None
    prompt = schedule.prompt_text
    if watch:
        # The worker decides the unread dot; the run never feeds long-term memory.
        run_config.update(run_mode="watch", incognito=True, mark_unread=False)

    try:
        if watch:
            watch_state = await asyncio.to_thread(get_watch_state, user_id, schedule_id)
            prompt = build_watch_prompt(schedule.prompt_text, watch_state)
        result: RunResult = await run_agent_headless(
            user_id=user_id,
            prompt=prompt,
            auth=CognitoRefreshBearerAuth(),
            title=schedule.label,
            rag_assistant_id=schedule.assistant_id,
            enabled_tools=schedule.enabled_tools,
            trigger="schedule",
            **run_config,
        )
    except HeadlessAuthError as e:
        # No active headless grant, or Cognito refused the refresh
        # exchange — the user must log in and re-enable. Do not
        # retry-spam a dead credential.
        return await _pause_reauth(schedule, "reauth_required", str(e))
    except Exception as e:
        # Last-resort catch: always record the run so the schedule never
        # gets stuck silently un-run, and the failure-streak breaker sees it.
        logger.error(f"Scheduled-runs worker: unexpected failure on schedule {schedule_id}: {e}", exc_info=True)
        return await _record_failure_and_maybe_pause(
            schedule,
            error=str(e),
            session_id=None,
            max_failures=_env_int("SCHEDULED_RUNS_MAX_FAILURES", 3),
            result_label="error",
        )

    if result.status == "oauth_required":
        detail = None
        if result.oauth_required:
            first = result.oauth_required[0]
            detail = f"provider={first.provider_id} authorization_url={first.authorization_url}"
        return await _pause_reauth(schedule, "oauth_required", detail, result)

    if result.status == "completed" and watch and result.quota_exceeded:
        # The "answer" is the quota message, not a finding: a watcher that can't
        # run is a failure (streak, then a schedule_paused notice), never a bell item.
        return await _record_failure_and_maybe_pause(
            schedule,
            error="quota_exceeded",
            session_id=result.session_id,
            max_failures=_env_int("SCHEDULED_RUNS_MAX_FAILURES", 3),
            result_label="error",
            result=result,
        )

    if result.status == "completed" and watch:
        return await _finish_watch_run(schedule, result, watch_state)

    if result.status == "completed":
        await record_run_result(
            user_id, schedule_id, status="completed", session_id=result.session_id
        )
        share_id = None
        if schedule.project_id and schedule.deliver_to == "project" and result.session_id:
            share_id = await asyncio.to_thread(ScheduleDelivery().deliver, schedule, result.session_id)
        await _record_project_run(schedule, "completed", share_id)
        await _record_history(schedule, "completed", result)
        logger.info(f"Schedule {schedule_id}: run {result.run_id} completed (session {result.session_id})")
        return {"scheduleId": schedule_id, "result": "completed", "sessionId": result.session_id}

    # error / timeout — record and apply the consecutive-failure breaker.
    return await _record_failure_and_maybe_pause(
        schedule,
        error=result.error,
        session_id=result.session_id,
        max_failures=_env_int("SCHEDULED_RUNS_MAX_FAILURES", 3),
        result_label=result.status,
        result=result,
    )


def lambda_handler(event, context):
    """Async-invoke entry point (InvocationType=Event from the dispatcher)."""
    return asyncio.run(run_schedule(event))
