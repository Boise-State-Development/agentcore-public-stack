"""KB sync dispatcher — the single initiator of scheduled sync work.

Fired by one EventBridge rate rule (every 15 minutes). Sweeps the sparse
DueSyncIndex and, for each due policy, applies the runaway guards from
docs/specs/assistant-kb-sync.md §7 in order:

1. Kill switch      — KB_SYNC_ENABLED must be "true" or the tick no-ops.
2. Liveness         — assistant and source must still exist; a miss
                      hard-deletes the policy on the spot (self-healing
                      backstop behind the eager delete cascades).
3. Circuit breaker  — consecutive_not_found >= 2 or consecutive_failures
                      >= 5 pauses the policy instead of dispatching.
4. Inactivity       — assistant unused for 30 days pauses the policy
                      (auto-resumed by the chat-path bump, PR-5).
5. In-flight skip   — a fresh syncRunStartedAt stamp means the previous
                      run hasn't finished; skip without re-arming. Stale
                      stamps (crashed runs) are overwritten by the re-arm.
6. Re-arm BEFORE work — next_sync_at advances (with failure backoff)
                      via a conditional write, then the worker is
                      async-invoked. A crashed worker costs one missed
                      sync, never a hot loop; a double-fired tick loses
                      the conditional write and skips.

Guard failures transition state (removing the policy from the sparse
index) — they never reschedule-and-retry.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from apis.shared.sync_policies.models import SyncPolicy
from apis.shared.timestamps import from_iso
from apis.shared.sync_policies.service import (
    INTERVAL_DELTAS,
    delete_sync_policy,
    list_due_policies,
    rearm_policy,
    set_policy_state,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "KBSync"

# Backoff cap from the spec: interval * 2^failures never exceeds 30 days.
MAX_BACKOFF = timedelta(days=30)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(dt: datetime) -> str:
    # Normalize the +00:00 offset to a single trailing Z: "…+00:00Z" (offset AND
    # Z) is invalid ISO 8601 and renders as Invalid Date in strict JS engines.
    return dt.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parse the house timestamp format, tolerating both the current trailing
    'Z' and the legacy '+00:00Z' (offset AND Z). Always returns a UTC-aware
    datetime so comparisons with :func:`_now` never mix naive and aware."""
    try:
        dt = from_iso(value)
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _get_assistant_item(assistant_id: str) -> Optional[Dict[str, Any]]:
    """See apis.app_api.kb_sync.records for the raw-lookup rationale."""
    from apis.app_api.kb_sync import records

    return records.get_assistant_item(assistant_id)


def _get_source_item(assistant_id: str, source_type: str, source_ref: str) -> Optional[Dict[str, Any]]:
    from apis.app_api.kb_sync import records

    return records.get_source_item(assistant_id, source_type, source_ref)


def _invoke_worker(payload: Dict[str, Any]) -> None:
    """Async-invoke the sync worker Lambda (fire-and-forget)."""
    import boto3

    function_name = os.environ["KB_SYNC_WORKER_FUNCTION_NAME"]
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
        logger.warning(f"Failed to emit KBSync metrics: {e}")


def _assistant_last_activity(assistant_item: Dict[str, Any]) -> Optional[datetime]:
    """Reference time for the inactivity guard.

    lastUsedAt when present; otherwise the newest of createdAt /
    updatedAt so pre-existing assistants aren't paused purely because the
    tracking field postdates them.
    """
    candidates = [assistant_item.get("lastUsedAt"), assistant_item.get("updatedAt"), assistant_item.get("createdAt")]
    parsed = [ts for ts in (_parse_timestamp(c) for c in candidates if c) if ts]
    return max(parsed) if parsed else None


def _backoff_next_sync_at(policy: SyncPolicy, now: datetime) -> str:
    """Next due time: the policy interval, doubled per consecutive failure,
    capped at 30 days."""
    delay = INTERVAL_DELTAS[policy.interval] * (2**policy.consecutive_failures)
    return _timestamp(now + min(delay, MAX_BACKOFF))


async def _dispatch_policy(policy: SyncPolicy, now: datetime, counts: Dict[str, int]) -> None:
    assistant_id = policy.assistant_id

    # Guard 2 — liveness: assistant, then source. A miss deletes the policy.
    assistant = _get_assistant_item(assistant_id)
    if assistant is None:
        logger.warning(f"Sync policy {policy.policy_id}: assistant {assistant_id} gone; deleting policy")
        await delete_sync_policy(assistant_id, policy.policy_id)
        counts["OrphansDeleted"] += 1
        return

    source = _get_source_item(assistant_id, policy.source_type, policy.source_ref)
    if source is None or source.get("status") == "deleting":
        logger.warning(
            f"Sync policy {policy.policy_id}: source {policy.source_ref} gone or deleting; deleting policy"
        )
        await delete_sync_policy(assistant_id, policy.policy_id)
        counts["OrphansDeleted"] += 1
        return

    # Guard 3 — circuit breaker on the streak counters the worker maintains.
    if policy.consecutive_not_found >= _env_int("KB_SYNC_MAX_NOT_FOUND", 2):
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="source no longer accessible"
        )
        counts["PausedBreaker"] += 1
        return
    if policy.consecutive_failures >= _env_int("KB_SYNC_MAX_FAILURES", 5):
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="repeated sync failures"
        )
        counts["PausedBreaker"] += 1
        return

    # Guard 4 — inactivity: nobody is using this assistant, stop paying for it.
    inactivity_days = _env_int("KB_SYNC_INACTIVITY_PAUSE_DAYS", 30)
    last_activity = _assistant_last_activity(assistant)
    if last_activity is not None and (now - last_activity) > timedelta(days=inactivity_days):
        await set_policy_state(
            assistant_id,
            policy.policy_id,
            "paused_inactive",
            state_reason=f"assistant unused for {inactivity_days}+ days (resumes on next use)",
        )
        counts["PausedInactive"] += 1
        return

    # Guard 5 — in-flight skip: fresh run stamp means the last run is still
    # going; leave the policy due and let a later tick retry. Stale stamps
    # are crashed runs — fall through and let the re-arm overwrite them.
    if policy.sync_run_started_at:
        started = _parse_timestamp(policy.sync_run_started_at)
        stale_after = timedelta(hours=_env_int("KB_SYNC_RUN_STAMP_STALE_HOURS", 2))
        if started is not None and (now - started) < stale_after:
            logger.info(f"Sync policy {policy.policy_id}: run in flight since {policy.sync_run_started_at}; skipping")
            counts["InFlightSkipped"] += 1
            return

    # Guard 6 — re-arm before work; losing the conditional write means
    # another dispatcher already claimed this policy.
    new_next = _backoff_next_sync_at(policy, now)
    won = await rearm_policy(
        assistant_id, policy.policy_id, policy.next_sync_at, new_next, mark_run_started=True
    )
    if not won:
        counts["RearmLost"] += 1
        return

    _invoke_worker(
        {
            "policyId": policy.policy_id,
            "assistantId": assistant_id,
            "sourceType": policy.source_type,
            "sourceRef": policy.source_ref,
        }
    )
    counts["Dispatched"] += 1


async def dispatch_once() -> Dict[str, int]:
    """One dispatcher tick. Returns the metric counts (also emitted)."""
    counts: Dict[str, int] = {
        "PoliciesDue": 0,
        "Dispatched": 0,
        "OrphansDeleted": 0,
        "PausedBreaker": 0,
        "PausedInactive": 0,
        "InFlightSkipped": 0,
        "RearmLost": 0,
    }

    if os.environ.get("KB_SYNC_ENABLED", "false").lower() != "true":
        logger.info("KB_SYNC_ENABLED is not true; dispatcher tick is a no-op")
        return counts

    now = _now()
    limit = _env_int("KB_SYNC_DISPATCH_LIMIT", 20)
    due = await list_due_policies(now=_timestamp(now), limit=limit)
    counts["PoliciesDue"] = len(due)
    logger.info(f"Dispatcher tick: {len(due)} due policies (limit {limit})")

    for policy in due:
        try:
            await _dispatch_policy(policy, now, counts)
        except Exception as e:
            # One broken policy must not starve the rest of the sweep.
            logger.error(f"Failed to dispatch sync policy {policy.policy_id}: {e}", exc_info=True)

    _emit_metrics(counts)
    return counts


def lambda_handler(event, context):
    """EventBridge entry point."""
    counts = asyncio.run(dispatch_once())
    return {"statusCode": 200, "body": counts}
