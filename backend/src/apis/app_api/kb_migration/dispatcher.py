"""Migration dispatcher: hand due knowledge bases to the worker, a few at a time.

Requirements 19.6, 15.14. One EventBridge tick reads the sparse ``KbWorkIndex``,
takes at most a bounded number of records, and asynchronously invokes the worker
once per record. It performs no migration itself and holds no state.

Follows ``apis/app_api/kb_sync/dispatcher.py`` closely — third use of that shape on
this table, after the sync dispatcher and the scheduled-runs dispatcher — so the
things that matter about it are already established: a bounded per-tick limit, one
broken record never starving the sweep, and metrics emitted from the tick rather
than from the worker.

Why the index makes the queue correct by physics
------------------------------------------------
``GSI7_PK``/``GSI7_SK`` are written only while a record is work-eligible
(``shadow``, ``verify``, ``promote``) and ``REMOVE``d on reaching a terminal state.
So this dispatcher cannot see a finished knowledge base even if it wanted to: there
is no filter to get wrong, because ineligible records are not in the index. Same
convention as ``DueSyncIndex``, ``AgentDirectoryIndex`` and ``AgentReportsIndex``
on this table.

Why it no-ops rather than refusing to start
-------------------------------------------
With ``MANAGED_KB_MIGRATION_ENABLED`` off the tick returns its zeroed counts and
invokes nothing. The Lambda still exists, still runs on schedule, and still emits
metrics — which is what makes turning the flag on a change with a known blast
radius rather than the first time this code has ever executed in production.

Feature: managed-kb-migration
Requirements: 19.6, 15.14
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: The migration flag. Absent, empty, or anything outside the truthy set means the
#: dispatcher invokes nothing.
FLAG_MIGRATION_ENABLED = "MANAGED_KB_MIGRATION_ENABLED"

#: Recognised affirmative spellings, matching the reconciler's. An allow-list
#: rather than a truthiness test, because the failure being designed around is a
#: value that is present but empty: ``bool("")`` is correct by luck,
#: ``bool("false")`` is not.
_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})

#: Mirrors ``KB_SYNC_DISPATCH_LIMIT``'s default of 20 (Requirement 15.14). The
#: limit exists twice over: it bounds the damage of a bug in the index sweep, and
#: it keeps a burst of enrolments from colliding with ``StartIngestionJob``'s
#: 0.1 RPS account-wide ceiling — which is not adjustable, so the only way to stay
#: under it is to not ask.
DEFAULT_DISPATCH_LIMIT = 20

#: Ceiling on the env-var override. A larger sweep should require repeated observed
#: ticks, not a variable edit.
DISPATCH_LIMIT_CEILING = 100

METRIC_DISPATCHED = "KbMigrationDispatched"
METRIC_DUE = "KbMigrationDue"
METRIC_DISPATCH_FAILED = "KbMigrationDispatchFailed"


def migration_enabled() -> bool:
    """Whether the dispatcher may invoke the worker at all.

    Read at call time. Bound as a module constant it would be captured at import
    and a test overriding the variable would silently get the production value —
    the mistake that cost a 33-second test on this feature already.
    """
    return (os.environ.get(FLAG_MIGRATION_ENABLED) or "").strip().lower() in _TRUTHY


def dispatch_limit() -> int:
    """Records taken per tick, bounded above by :data:`DISPATCH_LIMIT_CEILING`."""
    raw = os.environ.get("KB_MIGRATION_DISPATCH_LIMIT")
    try:
        value = int(raw) if raw else DEFAULT_DISPATCH_LIMIT
    except ValueError:
        logger.warning(
            f"KB_MIGRATION_DISPATCH_LIMIT={raw!r} is not an integer; using "
            f"{DEFAULT_DISPATCH_LIMIT}"
        )
        return DEFAULT_DISPATCH_LIMIT
    if value < 0:
        return 0
    if value > DISPATCH_LIMIT_CEILING:
        logger.warning(
            f"KB_MIGRATION_DISPATCH_LIMIT={value} exceeds the ceiling of "
            f"{DISPATCH_LIMIT_CEILING}; clamping"
        )
        return DISPATCH_LIMIT_CEILING
    return value


def _now_iso() -> str:
    from apis.shared.timestamps import utc_now_iso

    return utc_now_iso()


def _work_states() -> List[str]:
    """Every work-eligible state, drained-first.

    Derived from ``WORK_ELIGIBLE_STATES`` rather than restated, with an explicit
    priority order laid over it. A record in ``promote`` is one conditional write
    from being finished, so serving it ahead of new ``shadow`` work drains the queue
    instead of accumulating half-migrated knowledge bases.

    Anything work-eligible but absent from the priority list is appended rather
    than dropped. A state added to the records module and forgotten here then
    migrates slowly, which is a scheduling nuisance; dropped, it would stall
    forever with its work keys written and nothing ever reading them — invisible,
    because the record still looks queued.
    """
    from apis.shared.kb_backend.records import PROMOTE, SHADOW, VERIFY, WORK_ELIGIBLE_STATES

    priority = (PROMOTE, VERIFY, SHADOW)
    ordered = [state for state in priority if state in WORK_ELIGIBLE_STATES]
    remainder = sorted(set(WORK_ELIGIBLE_STATES) - set(priority))
    if remainder:
        logger.warning(
            f"work-eligible states {remainder} are not in the dispatcher's priority "
            f"order; sweeping them last"
        )
    return ordered + remainder


def _invoke_worker(payload: Dict[str, Any]) -> None:
    """Async-invoke the migration worker. Same shape as the sync dispatcher's."""
    import boto3

    function_name = os.environ.get("KB_MIGRATION_WORKER_FUNCTION_NAME")
    if not function_name:
        raise RuntimeError("KB_MIGRATION_WORKER_FUNCTION_NAME is not set")

    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def _emit_metrics(counts: Dict[str, int]) -> None:
    from apis.shared.kb_backend.metrics import emit_count

    for metric, value in (
        (METRIC_DUE, counts.get("Due", 0)),
        (METRIC_DISPATCHED, counts.get("Dispatched", 0)),
        (METRIC_DISPATCH_FAILED, counts.get("Failed", 0)),
    ):
        if value:
            emit_count(metric, value)


async def _due_records(limit: int, now_iso: str) -> List[Dict[str, Any]]:
    """Records whose ``dueAt`` has passed, across every work-eligible state.

    Queried per state because ``GSI7_PK`` *is* the state — one partition each — and
    trimmed to ``limit`` overall so the bound is on the tick's total work rather
    than per state, which is how a three-state sweep would quietly become a
    3× limit.
    """
    from apis.shared.kb_backend.records import query_due_work

    collected: List[Dict[str, Any]] = []
    for state in _work_states():
        if len(collected) >= limit:
            break
        remaining = limit - len(collected)
        try:
            found = await asyncio.to_thread(query_due_work, state, now_iso, remaining)
        except Exception as exc:
            logger.error(f"KbWorkIndex query failed for state {state}: {exc}", exc_info=True)
            continue
        collected.extend(found)
    return collected[:limit]


async def dispatch_once() -> Dict[str, int]:
    """One dispatcher tick. Returns the metric counts (also emitted)."""
    counts: Dict[str, int] = {"Due": 0, "Dispatched": 0, "Failed": 0}

    if not migration_enabled():
        logger.info(f"{FLAG_MIGRATION_ENABLED} is not truthy; dispatcher tick is a no-op")
        return counts

    limit = dispatch_limit()
    if limit == 0:
        logger.info("dispatch limit is 0; nothing will be dispatched this tick")
        return counts

    now_iso = _now_iso()
    due = await _due_records(limit, now_iso)
    counts["Due"] = len(due)
    logger.info(f"migration dispatcher tick: {len(due)} due records (limit {limit})")

    for record in due:
        app_kb_id = record.get("appKbId")
        pk = record.get("PK") or ""
        assistant_id = pk.split("#", 1)[1] if "#" in pk else ""
        if not assistant_id or not app_kb_id:
            # A record the index returned but that cannot be addressed. Logged and
            # skipped rather than raised: one malformed row must not starve the
            # sweep, and it will still be there next tick to be noticed.
            logger.error(f"skipping unaddressable KbWorkIndex row: PK={pk!r} appKbId={app_kb_id!r}")
            counts["Failed"] += 1
            continue

        try:
            _invoke_worker(
                {
                    "assistantId": assistant_id,
                    "appKbId": app_kb_id,
                    "migrationState": record.get("migrationState"),
                    "migrationGeneration": int(record.get("migrationGeneration") or 0),
                }
            )
            counts["Dispatched"] += 1
        except Exception as exc:
            logger.error(
                f"failed to dispatch migration for kb {app_kb_id}: {exc}", exc_info=True
            )
            counts["Failed"] += 1

    _emit_metrics(counts)
    return counts


def lambda_handler(event, context):
    """EventBridge entry point.

    Nothing is read from ``event``. The dispatcher's behaviour is a function of the
    index and the environment only — the same reasoning that fixed the reconciler's
    arming bypass, where an invocation field could turn a report-only job into a
    deleting one.
    """
    counts = asyncio.run(dispatch_once())
    return {"statusCode": 200, "body": counts}
