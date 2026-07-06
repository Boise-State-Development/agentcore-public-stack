"""Scheduled-runs dispatcher — the single initiator of scheduled agent runs.

Fired by one EventBridge rate rule (every 5 minutes). Sweeps the sparse
DueScheduleIndex (`apis.shared.scheduled_prompts.service.list_due_schedules`)
and, for each due schedule, applies the runaway guards from
docs/specs/scheduled-runs-phase-b-brief.md / scheduled-agent-runs.md §7 in
order, mirroring the KB-sync dispatcher
(`apis/app_api/kb_sync/dispatcher.py`) almost line for line:

1. Kill switch       — SCHEDULED_RUNS_ENABLED must be "true" or the tick
                       no-ops (the EventBridge rule itself is also gated
                       by `config.scheduledRuns.enabled` in CDK — belt and
                       braces, same as KB-sync).
2. Runaway guard      — `runs_today` (reset on UTC date rollover) compared
                       against the schedule's own `max_runs_per_day`. A
                       schedule that would exceed its ceiling is paused
                       instead of dispatched.
3. Re-arm BEFORE work — `next_run_at` advances via a conditional write
                       (`rearm_schedule`) before the worker is invoked, so
                       a double-fired tick is idempotent: the second
                       dispatcher loses the conditional write and skips
                       the schedule. A crashed worker costs one missed
                       run, never a hot loop.

Governance note (brief §1): delivery is role/auth-based — the dispatcher's
only job is "who's due, how many times today, don't double-fire". No
content classification happens here or in the worker.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from apis.shared.scheduled_prompts.service import (
    list_due_schedules,
    rearm_schedule,
    set_schedule_state,
)
from apis.shared.scheduled_prompts.models import ScheduledPrompt

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "ScheduledRuns"

# Cadence -> a normalized re-arm delta. The dispatcher recomputes the exact
# next_run_at via the same cadence math the schedule was created with
# (compute_next_run_at); this constant is only a defensive fallback bound so
# an unexpected cadence value can never wedge a schedule into a tight loop.
_FALLBACK_REARM_DELTA = timedelta(hours=1)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return date.today().isoformat()


def _invoke_worker(payload: Dict[str, Any]) -> None:
    """Async-invoke the scheduled-runs worker Lambda (fire-and-forget)."""
    import boto3

    function_name = os.environ["SCHEDULED_RUNS_WORKER_FUNCTION_NAME"]
    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def _emit_metrics(counts: Dict[str, int]) -> None:
    """Best-effort CloudWatch metrics; never fails the tick."""
    import boto3

    try:
        metric_data = [
            {"MetricName": name, "Value": value, "Unit": "Count"} for name, value in counts.items()
        ]
        boto3.client("cloudwatch").put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data)
    except Exception as e:
        logger.warning(f"Failed to emit ScheduledRuns metrics: {e}")


def _next_run_at(schedule: ScheduledPrompt, now: datetime) -> str:
    """Recompute the next due timestamp using the schedule's own cadence.

    Reuses the exact cadence math the schedule was created with
    (`compute_next_run_at`) so a daily/weekday/weekly schedule advances
    correctly — no cron strings, no engine-side interval table.
    """
    from apis.shared.scheduled_prompts.service import compute_next_run_at

    try:
        return compute_next_run_at(
            schedule.cadence,
            schedule.hour_local,
            schedule.timezone,
            weekday=schedule.weekday,
            from_time=now,
        )
    except Exception as e:
        # Defensive fallback — should never trigger for a valid schedule,
        # but a wedged schedule is worse than a slightly-off re-arm.
        logger.error(f"Schedule {schedule.schedule_id}: cadence recompute failed ({e}); using fallback delta")
        return (now + _FALLBACK_REARM_DELTA).isoformat().replace("+00:00", "Z")


def _runs_today(schedule: ScheduledPrompt, today: str) -> int:
    """Runaway-guard counter, reset across a UTC date rollover.

    Mirrors `record_run_result`'s own rollover logic: `runs_today` only
    counts against `runs_today_date`; a stale date means today's count is
    effectively zero (the counter has not been touched yet today).
    """
    if schedule.runs_today_date == today:
        return schedule.runs_today
    return 0


async def _dispatch_schedule(schedule: ScheduledPrompt, now: datetime, counts: Dict[str, int]) -> None:
    today = _today()

    # Guard 2 — runaway: would firing this schedule exceed its own daily
    # ceiling? Check BEFORE re-arming/invoking, using the rollover-aware
    # counter (a run this tick would be the (runs_today + 1)th today).
    runs_today = _runs_today(schedule, today)
    if runs_today >= schedule.max_runs_per_day:
        await set_schedule_state(
            schedule.user_id,
            schedule.schedule_id,
            "paused_error",
            state_reason="max_runs_per_day_exceeded",
        )
        logger.warning(
            f"Schedule {schedule.schedule_id}: runaway guard tripped "
            f"({runs_today}/{schedule.max_runs_per_day} runs today); pausing"
        )
        counts["PausedRunaway"] += 1
        return

    # Guard 3 — re-arm before work; losing the conditional write means
    # another dispatcher tick already claimed this schedule.
    new_next = _next_run_at(schedule, now)
    won = await rearm_schedule(
        schedule.user_id, schedule.schedule_id, schedule.next_run_at, new_next
    )
    if not won:
        counts["RearmLost"] += 1
        return

    _invoke_worker(
        {
            "scheduleId": schedule.schedule_id,
            "userId": schedule.user_id,
        }
    )
    counts["Dispatched"] += 1


async def dispatch_once() -> Dict[str, int]:
    """One dispatcher tick. Returns the metric counts (also emitted)."""
    counts: Dict[str, int] = {
        "SchedulesDue": 0,
        "Dispatched": 0,
        "PausedRunaway": 0,
        "RearmLost": 0,
    }

    if os.environ.get("SCHEDULED_RUNS_ENABLED", "false").lower() != "true":
        logger.info("SCHEDULED_RUNS_ENABLED is not true; dispatcher tick is a no-op")
        return counts

    now = _now()
    limit = _env_int("SCHEDULED_RUNS_DISPATCH_LIMIT", 20)
    now_iso = now.isoformat().replace("+00:00", "Z")
    due = await list_due_schedules(now=now_iso, limit=limit)
    counts["SchedulesDue"] = len(due)
    logger.info(f"Dispatcher tick: {len(due)} due schedules (limit {limit})")

    for schedule in due:
        try:
            await _dispatch_schedule(schedule, now, counts)
        except Exception as e:
            # One broken schedule must not starve the rest of the sweep.
            logger.error(f"Failed to dispatch schedule {schedule.schedule_id}: {e}", exc_info=True)

    _emit_metrics(counts)
    return counts


def lambda_handler(event, context):
    """EventBridge entry point."""
    counts = asyncio.run(dispatch_once())
    return {"statusCode": 200, "body": counts}
