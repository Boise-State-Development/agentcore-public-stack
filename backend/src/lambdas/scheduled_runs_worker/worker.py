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
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from apis.shared.harness import (
    CognitoRefreshBearerAuth,
    HeadlessAuthError,
    RunResult,
    run_agent_headless,
)
from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.service import (
    get_scheduled_prompt,
    record_run_result,
    set_schedule_state,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


async def _record_failure_and_maybe_pause(
    schedule: ScheduledPrompt,
    *,
    error: Optional[str],
    session_id: Optional[str],
    max_failures: int,
    result_label: str,
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
    if streak >= max_failures:
        await set_schedule_state(
            schedule.user_id,
            schedule.schedule_id,
            "paused_error",
            state_reason="repeated_failures",
        )
        logger.warning(
            f"Schedule {schedule.schedule_id}: {streak} consecutive failures "
            f"(max {max_failures}); pausing"
        )
        return {"scheduleId": schedule.schedule_id, "result": "paused_error", "reason": "repeated_failures"}
    return {"scheduleId": schedule.schedule_id, "result": result_label}


async def _pause_reauth(schedule: ScheduledPrompt, reason: str, detail: Optional[str] = None) -> Dict[str, Any]:
    logger.warning(
        f"Schedule {schedule.schedule_id}: pausing ({reason}) for user {schedule.user_id}"
        + (f" — {detail}" if detail else "")
    )
    await record_run_result(schedule.user_id, schedule.schedule_id, status="error", error=detail or reason)
    await set_schedule_state(
        schedule.user_id,
        schedule.schedule_id,
        "paused_error",
        state_reason=reason,
    )
    return {"scheduleId": schedule.schedule_id, "result": "paused_error", "reason": reason}


async def run_schedule(payload: Dict[str, Any]) -> Dict[str, Any]:
    schedule_id = payload["scheduleId"]
    user_id = payload["userId"]

    schedule = await get_scheduled_prompt(user_id, schedule_id)
    if schedule is None:
        logger.info(f"Scheduled-runs worker: schedule {schedule_id} no longer exists; dropping run")
        return {"scheduleId": schedule_id, "result": "dropped"}

    try:
        result: RunResult = await run_agent_headless(
            user_id=user_id,
            prompt=schedule.prompt_text,
            auth=CognitoRefreshBearerAuth(),
            title=schedule.label,
            rag_assistant_id=schedule.assistant_id,
            enabled_tools=schedule.enabled_tools,
            trigger="schedule",
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
        return await _pause_reauth(schedule, "oauth_required", detail)

    if result.status == "completed":
        await record_run_result(
            user_id, schedule_id, status="completed", session_id=result.session_id
        )
        logger.info(f"Schedule {schedule_id}: run {result.run_id} completed (session {result.session_id})")
        return {"scheduleId": schedule_id, "result": "completed", "sessionId": result.session_id}

    # error / timeout — record and apply the consecutive-failure breaker.
    return await _record_failure_and_maybe_pause(
        schedule,
        error=result.error,
        session_id=result.session_id,
        max_failures=_env_int("SCHEDULED_RUNS_MAX_FAILURES", 3),
        result_label=result.status,
    )


def lambda_handler(event, context):
    """Async-invoke entry point (InvocationType=Event from the dispatcher)."""
    return asyncio.run(run_schedule(event))
