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
While ``WATCH_TRIAGE_ENABLED`` is on, that same write also stores what the run
leaves for the triage stage (docs/specs/quiet-watcher-triage.md §3.4): its
probe (from the ``watch_probe`` frame and the tool trace), ``lastFullRunAt``
and ``wakeAt``. The run's outcome is the same either way.
While it is on, each watch run is also **triaged in shadow** first (triage §6.5,
PR T.2): unless §2.4 says to skip (no probe, a stale version, ``wakeAt`` due, or
the last full run older than the staleness floor), the worker asks the Runtime
to replay the stored probe (``run_watch_triage``, on the run's own session id),
records the verdict on the run's ``SCHEDRUN#`` row as ``triage``, and then runs
the full run **regardless**. A failed triage is an escalation, never a failed
run.
A standard schedule, or any schedule while the flag is off, runs as before.

A run that paused on the user (``needs_user``: a tool approval, a question, a
form or a browser sign-in, docs/specs/answer-from-inbox.md §3.2) is, while the
flag is on and for a personal schedule, recorded as done (the streak resets),
leaves a ``needs_user`` history row and one ``agent_needs_*`` inbox row, and,
for a watcher, sets the hold: the watcher skips its runs until that pause is
answered or expires. While the flag is off, or for a project schedule, it takes
exactly the ``completed`` path, as it did before the status existed.

The same function resumes such a run when its owner answers it from the inbox
(``{"kind": "inbox_resume", …}`` from app-api's ``POST /notifications/{id}/respond``,
docs/specs/answer-from-inbox.md §3.5). It first sets the claim's start mark, one
conditional write: a retried event that finds the mark spends no second model
call (it ends the run's audit row and gives the row back to ``retry``), and a
late event for a claim since retaken is dropped. It then resumes headlessly
through ``run_agent_headless(interrupt_responses=…)`` on the headless grant the
respond call renewed, and settles the row, the run's history row and a
watcher's state with ``finish_resumed_run``. The answers are never logged.
"""

import asyncio
import contextvars
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from apis.shared.harness import (
    CognitoRefreshBearerAuth,
    GovernanceFloor,
    HeadlessAuthError,
    RunAuditRecorder,
    RunResult,
    run_agent_headless,
    run_watch_triage,
)
from apis.shared.feature_flags import proactive_runs_enabled, watch_triage_enabled
from apis.shared.projects.schedule_delivery import ScheduleDelivery
from apis.shared.projects.schedules import ProjectSchedules
from apis.shared.scheduled_prompts.conversation_resume import WATCH_RESUME_REPORT_ATTR, apply_conversation_resume
from apis.shared.scheduled_prompts.models import HEADLESS_SESSION_PREFIX, ScheduledPrompt
from apis.shared.scheduled_prompts.pausing import pause_schedule
from apis.shared.scheduled_prompts.needs_user import (
    INBOX_RESUME_KIND,
    live_pending_prompts,
    needs_user_kind,
    needs_user_payload,
)
from apis.shared.scheduled_prompts.resume_run import finish_resumed_run
from apis.shared.scheduled_prompts.runs import ScheduleRunStatus, record_schedule_run
from apis.shared.scheduled_prompts.watch_probe import FullRunTriage, full_run_triage
from apis.shared.scheduled_prompts.watch_triage import (
    TRIAGE_FAILED,
    escalate,
    history_triage,
    skip_reason,
    triage_request_from_state,
)
from apis.shared.scheduled_prompts.watch_run import (
    WatchOutcome,
    build_watch_prompt,
    decide_watch_outcome,
    next_watch_state,
)
from apis.shared.scheduled_prompts.watch_state import (
    WatchState,
    clear_watch_pause,
    get_watch_state,
    save_watch_state,
    set_watch_pause,
)
from apis.shared.scheduled_prompts.service import (
    get_scheduled_prompt,
    record_run_result,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: The shadow triage of the run being handled (triage §6.5), which every history row it writes
#: carries as ``triage``. Set once per ``run_schedule``; ``None`` when no triage applied.
_RUN_TRIAGE: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "watch_run_triage", default=None
)


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
    expires_at: Optional[str] = None,
    prompt_count: int = 0,
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
        expires_at=expires_at,
        prompt_count=prompt_count,
        triage=_RUN_TRIAGE.get(),
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
    triage: Optional[FullRunTriage] = None,
) -> None:
    """Save the watcher's next state, conditional on the version the run read. Never raises."""
    try:
        saved = await asyncio.to_thread(
            save_watch_state,
            schedule.user_id,
            schedule.schedule_id,
            next_watch_state(previous, outcome, finished_at),
            expected_version=previous.version if previous is not None else None,
            triage=triage,
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


def _full_run_triage(schedule: ScheduledPrompt, result: RunResult) -> Optional[FullRunTriage]:
    """What a completed full run leaves for triage (triage spec §3.4), or ``None`` while it is off.

    Never raises: bookkeeping for a later stage must not cost this run its state.
    A run whose probe can't be read leaves none, so the old baseline is removed
    rather than outliving the full run that replaced it.
    """
    if not watch_triage_enabled():
        return None
    try:
        triage = full_run_triage(
            frame=result.watch_probe,
            tool_trace=result.tool_trace,
            run_id=result.run_id,
            finished_at=result.finished_at,
            now=datetime.now(timezone.utc),
        )
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not read the run's probe", exc_info=True)
        return FullRunTriage(last_full_run_at=result.finished_at or _now_iso())
    logger.info(
        f"Schedule {schedule.schedule_id}: watch run {result.run_id} "
        + (f"left a probe of {len(triage.probe)} call(s)" if triage.probe else "left no probe")
        + (f", wakeAt {triage.wake_at}" if triage.wake_at else "")
    )
    return triage


async def _finish_watch_run(
    schedule: ScheduledPrompt,
    result: RunResult,
    previous: Optional[WatchState],
) -> Dict[str, Any]:
    """A completed watch run: quiet or notify (spec §3.3), then save the state."""
    triage = _full_run_triage(schedule, result)
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
        await _save_watch_state(schedule, previous, outcome, result.finished_at, triage)
        logger.info(f"Schedule {schedule_id}: watch run {result.run_id} notified {len(outcome.items)} item(s)")
        if (_RUN_TRIAGE.get() or {}).get("verdict") == "quiet":
            # The shadow's one failure (triage §6.5): read each by hand; no content is logged.
            logger.warning(f"Schedule {schedule_id}: shadow miss, triage said quiet and run {result.run_id} notified")
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
    await _save_watch_state(schedule, previous, outcome, result.finished_at, triage)
    logger.info(f"Schedule {schedule_id}: watch run {result.run_id} was quiet")
    return {"scheduleId": schedule_id, "result": "quiet"}


async def _load_meta_row(session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """The session's ``META`` row for this user, or ``None``. One read."""
    from apis.shared.sessions.metadata import load_session_meta

    return (await load_session_meta(session_id, user_id)).row


def _pause_live_in(row: Optional[Dict[str, Any]]) -> bool:
    """Whether a session's ``META`` row still waits on the user (``derive_awaiting_input``'s rule)."""
    from apis.shared.sessions.models import derive_awaiting_input

    if not row:
        return False
    kinds = [(item or {}).get("kind") for item in row.get("pendingInterrupts") or [] if isinstance(item, dict)]
    paused = row.get("pausedTurn") or {}
    expires_at = paused.get("expiresAt") if isinstance(paused, dict) else None
    return derive_awaiting_input(kinds, expires_at) is not None


async def _watcher_holds(
    schedule: ScheduledPrompt, state: Optional[WatchState]
) -> Tuple[bool, Optional[WatchState]]:
    """Whether a watcher skips this run because an earlier run still waits on the user (§3.2),
    and the state the run should start from.

    Reads the paused session's ``META`` only while a hold is set and unexpired. A hold whose
    pause has gone (answered, superseded or expired) ends, and the watcher runs. When the pause was answered
    in its conversation, the Runtime left that resume's ``report`` on the row
    (``conversation_resume``); it is saved here and the hold ended in one conditional write,
    so the run starts past the finding instead of asking again (#2490), and the state is
    read again for the run. A bell resume ended the hold in its own save, so its record is
    never read. Fails open: an error clears nothing and runs.
    """
    if state is None or not state.paused_session_id:
        return False, state
    session_id = state.paused_session_id
    try:
        row = await _load_meta_row(session_id, schedule.user_id) if state.holding() else None
        if _pause_live_in(row):
            return True, state
        record = (row or {}).get(WATCH_RESUME_REPORT_ATTR)
        if isinstance(record, dict):
            saved = await asyncio.to_thread(
                apply_conversation_resume, schedule.user_id, schedule.schedule_id, schedule.label, state, record
            )
            if saved:
                logger.info(f"Schedule {schedule.schedule_id}: saved the report of a run answered in its conversation")
                return False, await asyncio.to_thread(get_watch_state, schedule.user_id, schedule.schedule_id)
            logger.warning(f"Schedule {schedule.schedule_id}: watcher state moved on; the conversation's report not saved")
        await asyncio.to_thread(clear_watch_pause, schedule.user_id, schedule.schedule_id, session_id=session_id)
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not check the watcher's hold", exc_info=True)
    return False, state


def _notify_needs_user(schedule: ScheduledPrompt, result: RunResult, prompts, expires_at: Optional[str]) -> bool:
    """The run's one ``agent_needs_*`` inbox row, to the owner. Returns whether it was written."""
    email = schedule.owner_email
    if not email:
        logger.info(f"Schedule {schedule.schedule_id}: no owner email on record, so no inbox row")
        return False
    from apis.shared.notifications.service import NotificationService

    return NotificationService().notify_own(
        recipient_email=email,
        kind=needs_user_kind(prompts),
        payload=needs_user_payload(
            schedule_id=schedule.schedule_id,
            run_id=result.run_id,
            run_finished_at=result.finished_at,
            session_id=result.session_id,
            label=schedule.label,
            expires_at=expires_at,
            prompts=prompts,
        ),
    ) is not None


async def _record_completed(schedule: ScheduledPrompt, result: RunResult) -> Dict[str, Any]:
    """A standard run that finished: reset the streak, deliver to a project, leave the history row."""
    await record_run_result(
        schedule.user_id, schedule.schedule_id, status="completed", session_id=result.session_id
    )
    share_id = None
    if schedule.project_id and schedule.deliver_to == "project" and result.session_id:
        share_id = await asyncio.to_thread(ScheduleDelivery().deliver, schedule, result.session_id)
    await _record_project_run(schedule, "completed", share_id)
    await _record_history(schedule, "completed", result)
    logger.info(f"Schedule {schedule.schedule_id}: run {result.run_id} completed (session {result.session_id})")
    return {"scheduleId": schedule.schedule_id, "result": "completed", "sessionId": result.session_id}


async def _finish_needs_user_run(
    schedule: ScheduledPrompt,
    result: RunResult,
    watch_state: Optional[WatchState],
    watch: bool,
) -> Dict[str, Any]:
    """A personal run that paused on the user, while the flag is on (§3.2)."""
    schedule_id = schedule.schedule_id
    prompts: list = []
    expires_at: Optional[str] = None
    try:
        row = await _load_meta_row(result.session_id, schedule.user_id)
        prompts, expires_at = live_pending_prompts(row, result.pending_interrupt_ids)
    except Exception:
        logger.warning(f"Schedule {schedule_id}: could not read run {result.run_id}'s pause", exc_info=True)
    if not prompts:
        # Answered or cleared before the worker looked: the run is simply done.
        # A watcher's run is not quiet (it asked the user), so it keeps its conversation.
        logger.info(f"Schedule {schedule_id}: run {result.run_id} paused, but the pause is gone")
        return await _record_completed(schedule, result)

    await record_run_result(schedule.user_id, schedule_id, status="completed", session_id=result.session_id)
    await _record_history(schedule, "needs_user", result, expires_at=expires_at, prompt_count=len(prompts))
    await asyncio.to_thread(_notify_needs_user, schedule, result, prompts, expires_at)
    if watch:
        # A watch run leaves the unread dot to the worker, and this one holds a card.
        try:
            from apis.shared.sessions.metadata import set_session_unread

            await set_session_unread(result.session_id, schedule.user_id, True)
        except Exception:
            logger.warning(f"Schedule {schedule_id}: could not mark the run unread", exc_info=True)
        if expires_at:
            try:
                held = await asyncio.to_thread(
                    set_watch_pause,
                    schedule.user_id,
                    schedule_id,
                    session_id=result.session_id,
                    paused_until=expires_at,
                    expected_version=watch_state.version if watch_state is not None else None,
                )
                if not held:
                    logger.warning(f"Schedule {schedule_id}: watcher state moved on during the run; no hold set")
            except Exception:
                logger.warning(f"Schedule {schedule_id}: could not set the watcher's hold", exc_info=True)
    logger.info(f"Schedule {schedule_id}: run {result.run_id} waits on the user ({len(prompts)} prompt(s))")
    return {
        "scheduleId": schedule_id,
        "result": "needs_user",
        "sessionId": result.session_id,
        "promptCount": len(prompts),
    }


async def _shadow_triage(
    schedule: ScheduledPrompt, state: Optional[WatchState], session_id: str
) -> Optional[Dict[str, Any]]:
    """What tier 1 would have done for this run (triage §6.5), for its history row, or ``None``.

    ``None`` while ``WATCH_TRIAGE_ENABLED`` is off. A run §2.4 skips records why and sends no
    invocation. Changes no outcome: the full run follows whatever this returns. Never raises
    but ``HeadlessAuthError``, which the full run would meet too.
    """
    if not watch_triage_enabled():
        return None
    skipped = skip_reason(state, datetime.now(timezone.utc))
    if skipped is not None:
        logger.info(f"Schedule {schedule.schedule_id}: no triage ({skipped})")
        return history_triage(None, ms=0, skipped=skipped)
    request = triage_request_from_state(schedule.schedule_id, state)
    if request is None:
        return history_triage(escalate(TRIAGE_FAILED), ms=0)
    try:
        verdict, ms = await run_watch_triage(
            user_id=schedule.user_id,
            session_id=session_id,
            request=request,
            auth=CognitoRefreshBearerAuth(),
            rag_assistant_id=schedule.assistant_id,
            enabled_tools=schedule.enabled_tools,
        )
    except HeadlessAuthError:
        raise
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: triage failed", exc_info=True)
        verdict, ms = escalate(TRIAGE_FAILED), 0
    logger.info(
        f"Schedule {schedule.schedule_id}: shadow triage {verdict.verdict} ({verdict.reason}) in {ms} ms"
    )
    return history_triage(verdict, ms=ms)


async def run_schedule(payload: Dict[str, Any]) -> Dict[str, Any]:
    schedule_id = payload["scheduleId"]
    user_id = payload["userId"]

    # A worker that handles several runs in one context must not carry one's triage to the next.
    _RUN_TRIAGE.set(None)
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
            held, watch_state = await _watcher_holds(schedule, watch_state)
            if held:
                # Nothing is invoked and no history row is written: the
                # schedule page shows the wait from the state (§3.8).
                logger.info(f"Schedule {schedule_id}: holding while an earlier run waits on the user")
                return {"scheduleId": schedule_id, "result": "held"}
            prompt = build_watch_prompt(schedule.prompt_text, watch_state)
            # The triage and the full run share one session, so they land on one microVM (§4.1).
            run_config["session_id"] = f"{HEADLESS_SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
            _RUN_TRIAGE.set(await _shadow_triage(schedule, watch_state, run_config["session_id"]))
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

    if result.status == "needs_user":
        if proactive_runs_enabled() and not schedule.project_id:
            return await _finish_needs_user_run(schedule, result, watch_state, watch)
        # Flag off, or a project schedule: exactly as a completed run, as before.
        result.status = "completed"

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
        return await _record_completed(schedule, result)

    # error / timeout — record and apply the consecutive-failure breaker.
    return await _record_failure_and_maybe_pause(
        schedule,
        error=result.error,
        session_id=result.session_id,
        max_failures=_env_int("SCHEDULED_RUNS_MAX_FAILURES", 3),
        result_label=result.status,
        result=result,
    )


def _inbox_service():
    from apis.shared.notifications.service import NotificationService

    return NotificationService()


def _audit() -> RunAuditRecorder:
    return RunAuditRecorder()


async def _end_lost_resume(user_id: str, email: str, notification_id: str, resume_run_id: str, why: str) -> None:
    """A resume event that will not run: end its audit row if nothing did, and give the row back.

    The row goes back to ``retry`` only while this resume still holds the claim, so a newer
    claim is never touched. Never raises.
    """
    from apis.shared.notifications.models import NotificationResolution

    try:
        await asyncio.to_thread(
            _audit().end_if_started, user_id=user_id, run_id=resume_run_id, status="error", error=why
        )
    except Exception:
        logger.warning(f"Inbox resume {resume_run_id}: could not end its audit row", exc_info=True)
    await asyncio.to_thread(
        _inbox_service().set_resolution,
        email,
        notification_id,
        NotificationResolution(state="retry", at=_now_iso(), resume_run_id=resume_run_id),
        claimed_by=resume_run_id,
    )


async def run_inbox_resume(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resume a paused scheduled run with the answers given in the inbox (§3.5)."""
    user_id = payload["userId"]
    email = payload["email"]
    notification_id = payload["notificationId"]
    resume_run_id = payload["resumeRunId"]
    session_id = payload["sessionId"]
    schedule_id = payload.get("scheduleId") or ""
    service = _inbox_service()

    if not await asyncio.to_thread(service.mark_worker_started, email, notification_id, resume_run_id=resume_run_id):
        current = await asyncio.to_thread(service.get, email, notification_id)
        resolution = current.resolution if current is not None else None
        if (
            resolution is not None
            and resolution.state == "applying"
            and resolution.resume_run_id == resume_run_id
            and resolution.worker_started_at
        ):
            # A retry of an attempt that started and died: never a second model call.
            logger.warning(f"Inbox resume {resume_run_id}: an earlier attempt started and was lost")
            await _end_lost_resume(user_id, email, notification_id, resume_run_id, "resume_lost")
            return {"resumeRunId": resume_run_id, "result": "resume_lost"}
        logger.info(f"Inbox resume {resume_run_id}: its claim is gone or was retaken; dropping the event")
        try:
            await asyncio.to_thread(
                _audit().end_if_started, user_id=user_id, run_id=resume_run_id, status="error", error="superseded"
            )
        except Exception:
            logger.warning(f"Inbox resume {resume_run_id}: could not end its audit row", exc_info=True)
        return {"resumeRunId": resume_run_id, "result": "dropped"}

    notification = await asyncio.to_thread(service.get, email, notification_id)
    schedule = await get_scheduled_prompt(user_id, schedule_id) if schedule_id else None
    watch_state: Optional[WatchState] = None
    if schedule is not None and _is_watch(schedule):
        try:
            watch_state = await asyncio.to_thread(get_watch_state, user_id, schedule_id)
        except Exception:
            logger.warning(f"Inbox resume {resume_run_id}: could not read the watcher's state", exc_info=True)

    try:
        result = await run_agent_headless(
            user_id=user_id,
            prompt="",
            auth=CognitoRefreshBearerAuth(),
            session_id=session_id,
            run_id=resume_run_id,
            # app-api wrote the start row before its 202; the runner writes the end onto it.
            audit_started=True,
            governance=GovernanceFloor(audit=_audit()),
            interrupt_responses=payload.get("responses") or [],
            trigger="inbox_respond",
            mark_unread=False,
        )
    except HeadlessAuthError as e:
        # The grant was renewed a moment ago, so this is rare; nothing resumed.
        logger.warning(f"Inbox resume {resume_run_id}: could not mint for the user: {e}")
        await _end_lost_resume(user_id, email, notification_id, resume_run_id, "auth")
        return {"resumeRunId": resume_run_id, "result": "retry"}
    except Exception as e:
        logger.error(f"Inbox resume {resume_run_id}: unexpected failure: {e}", exc_info=True)
        result = RunResult(
            run_id=resume_run_id,
            session_id=session_id,
            user_id=user_id,
            status="error",
            error=str(e)[:500],
            finished_at=_now_iso(),
        )
        try:
            await GovernanceFloor(audit=_audit()).on_run_end(result=result)
        except Exception:
            logger.warning(f"Inbox resume {resume_run_id}: could not end its audit row", exc_info=True)

    if notification is None:
        logger.warning(f"Inbox resume {resume_run_id}: its notification is gone; nothing to settle")
        return {"resumeRunId": resume_run_id, "result": result.status}
    summary = await finish_resumed_run(
        result=result,
        notification=notification,
        email=email,
        user_id=user_id,
        resume_run_id=resume_run_id,
        schedule=schedule,
        watch_state=watch_state,
        service=service,
    )
    logger.info(f"Inbox resume {resume_run_id}: {summary}")
    return summary


def lambda_handler(event, context):
    """Async-invoke entry point (InvocationType=Event from the dispatcher, or app-api's respond)."""
    if isinstance(event, dict) and event.get("kind") == INBOX_RESUME_KIND:
        return asyncio.run(run_inbox_resume(event))
    return asyncio.run(run_schedule(event))
