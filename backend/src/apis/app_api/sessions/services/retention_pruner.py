"""Retention pruning: a session whose content has all expired leaves the sidebar.

docs/specs/conversation-search.md §3, "The session row". Every copy of a
conversation's text (Memory events, the archive, the search index) is kept for
``CONVERSATION_RETENTION_DAYS`` after each turn, but the session row has no TTL,
so without this a conversation past retention stays in the sidebar and opens as
"No messages yet". Once a day this deletes those rows through the same
:class:`SessionDeleteCascade` a user's delete runs, so the summary records,
files, shares and archive go with them. Cost rows (``C#``) are untouched.

Where it runs
-------------
As a scheduled one-off task on the **app-api task definition**
(``python -m apis.app_api.sessions.services.retention_pruner``), not a Lambda.
The cascade reaches most of app-api (the file, share and artifact services and
the Memory client), and running it in app-api's own image, with app-api's
environment and role, is what makes it the same cascade rather than a copy.
CDK points the schedule at the task definition *family*, so it always runs the
revision the deploy last registered.

What it will not delete
-----------------------
* A row whose ``lastMessageAt`` is missing or unreadable: unknown age is not old.
* A row with ``lastTurnContinuable`` set, or any ``pendingInterrupts``: the user
  has a turn they can still resume.
* A row that changed since the scan: each candidate is re-read just before its
  delete and the checks are run again.

Report, then delete: the first run in an environment is dry
-----------------------------------------------------------
Deleting needs three things: :func:`conversation_retention_prunes_sessions`
(default on; ``=false`` turns the scan off entirely),
:func:`conversation_retention_prune_armed` (default **off**, set per
environment), and a dry-run report already recorded in this environment for
the same retention setting. Anything less is a dry run, which logs the count
and the oldest and newest ``lastMessageAt`` it would delete and records itself
in SSM (``/{PROJECT_PREFIX}/conversation-retention/prune-dry-run``). So an
environment never deletes on its first run, even one armed before its first
deploy, and changing the retention setting makes the next run dry again,
because a shorter retention can make a very different set eligible.

Deletes are throttled: oldest first, a pause between sessions (``--sleep``),
and at most ``--limit`` per run, so a backlog drains over several days.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

from apis.shared.conversation_archive.retention import retention_cutoff, retention_days
from apis.shared.feature_flags import (
    conversation_retention_prune_armed,
    conversation_retention_prunes_sessions,
)

logger = logging.getLogger(__name__)

#: Sessions deleted per run at most. The 2026-10-06 prod census found 1,439
#: sessions past 90 days; 500 a day drains a backlog that size in three runs.
DEFAULT_MAX_PER_RUN = 500

#: Seconds between deletes. Each cascade is a handful of Memory, S3 and
#: DynamoDB calls; a second's pause keeps the pruner a background trickle.
DEFAULT_SLEEP_SECONDS = 1.0

#: SSM parameter (under ``/{PROJECT_PREFIX}/``) that records the last dry run.
DRY_RUN_PARAMETER_SUFFIX = "conversation-retention/prune-dry-run"

_SCAN_ATTRIBUTES = (
    "PK",
    "SK",
    "userId",
    "sessionId",
    "lastMessageAt",
    "#status",
    "deleted",
    "lastTurnContinuable",
    "pendingInterrupts",
)


# ── Rows ─────────────────────────────────────────────────────────────────────
def parse_timestamp(value: Any) -> Optional[datetime]:
    """An ISO-8601 ``lastMessageAt`` as aware UTC, or None if it is not one."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_session_row(item: Dict[str, Any]) -> bool:
    """A live (not deleted) session row in a user's partition."""
    if not str(item.get("PK", "")).startswith("USER#"):
        return False
    sk = str(item.get("SK", ""))
    if not sk.startswith("S#") or sk.startswith("S#DELETED#"):
        return False
    return not item.get("deleted") and item.get("status") != "deleted"


def ids_of(item: Dict[str, Any]) -> Optional[tuple]:
    """``(user_id, session_id)`` for a session row."""
    user_id = item.get("userId") or str(item.get("PK", ""))[len("USER#"):]
    session_id = item.get("sessionId") or str(item.get("SK", "")).rsplit("#", 1)[-1]
    if not user_id or not session_id:
        return None
    return user_id, session_id


def hold_reason(item: Dict[str, Any], cutoff: datetime) -> Optional[str]:
    """Why this row must not be pruned, or None when it may be.

    ``"recent"`` is the ordinary case; the others are the guards the spec
    requires, counted separately in the report.
    """
    last = parse_timestamp(item.get("lastMessageAt"))
    if last is None:
        return "missing_last_message_at"
    if last >= cutoff:
        return "recent"
    if item.get("lastTurnContinuable"):
        return "continuable"
    if item.get("pendingInterrupts"):
        return "pending_interrupt"
    return None


def scan_session_rows(table: Any) -> Iterator[Dict[str, Any]]:
    """Every live session row (both static-SK and legacy). Read-only."""
    from boto3.dynamodb.conditions import Attr

    params: Dict[str, Any] = {
        "FilterExpression": Attr("SK").begins_with("S#") & Attr("PK").begins_with("USER#"),
        "ProjectionExpression": ", ".join(_SCAN_ATTRIBUTES),
        "ExpressionAttributeNames": {"#status": "status"},
    }
    while True:
        page = table.scan(**params)
        for item in page.get("Items") or []:
            if is_session_row(item):
                yield item
        start = page.get("LastEvaluatedKey")
        if not start:
            return
        params["ExclusiveStartKey"] = start


# ── Report ───────────────────────────────────────────────────────────────────
@dataclass
class PruneReport:
    """What one run found and did. Counts and timestamps only, no ids."""

    mode: str = "dry-run"
    reason: str = ""
    retention_days: int = 0
    cutoff: str = ""
    scanned: int = 0
    candidates: int = 0
    oldest_last_message_at: Optional[str] = None
    newest_last_message_at: Optional[str] = None
    held: Dict[str, int] = field(default_factory=dict)
    deleted: int = 0
    skipped_on_recheck: int = 0
    failed: int = 0
    cascade_step_failures: Dict[str, int] = field(default_factory=dict)
    limit_reached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "retentionDays": self.retention_days,
            "cutoff": self.cutoff,
            "scanned": self.scanned,
            "candidates": self.candidates,
            "oldestLastMessageAt": self.oldest_last_message_at,
            "newestLastMessageAt": self.newest_last_message_at,
            "held": self.held,
            "deleted": self.deleted,
            "skippedOnRecheck": self.skipped_on_recheck,
            "failed": self.failed,
            "cascadeStepFailures": self.cascade_step_failures,
            "limitReached": self.limit_reached,
        }


# ── The dry-run record ───────────────────────────────────────────────────────
def _dry_run_parameter_name() -> Optional[str]:
    prefix = os.environ.get("PROJECT_PREFIX", "").strip()
    return f"/{prefix}/{DRY_RUN_PARAMETER_SUFFIX}" if prefix else None


def _ssm():
    import boto3

    return boto3.client("ssm", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def read_dry_run_record(ssm: Any = None) -> Optional[Dict[str, Any]]:
    """The last recorded dry run, or None (never run, or unreadable)."""
    name = _dry_run_parameter_name()
    if not name:
        return None
    try:
        value = (ssm or _ssm()).get_parameter(Name=name)["Parameter"]["Value"]
        record = json.loads(value)
    except Exception:  # noqa: BLE001 - "no record" is the safe reading of any failure
        return None
    return record if isinstance(record, dict) else None


def write_dry_run_record(report: PruneReport, ssm: Any = None) -> bool:
    name = _dry_run_parameter_name()
    if not name:
        logger.warning("PROJECT_PREFIX is not set; the dry run cannot be recorded")
        return False
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "retentionDays": report.retention_days,
        "candidates": report.candidates,
        "oldestLastMessageAt": report.oldest_last_message_at,
        "newestLastMessageAt": report.newest_last_message_at,
    }
    try:
        (ssm or _ssm()).put_parameter(
            Name=name,
            Value=json.dumps(record),
            Type="String",
            Overwrite=True,
            Description="Last dry run of the conversation retention pruner (docs/specs/conversation-search.md §3)",
        )
    except Exception:  # noqa: BLE001 - the next run is simply dry again
        logger.warning("Could not record the retention pruner's dry run", exc_info=True)
        return False
    return True


def deletes_allowed(days: int, record: Optional[Dict[str, Any]]) -> tuple:
    """``(allowed, reason)``: whether this run may delete, and why not if not."""
    if not conversation_retention_prune_armed():
        return False, "not armed (CONVERSATION_RETENTION_PRUNE_ARMED)"
    if record is None:
        return False, "first run in this environment: no dry run recorded yet"
    if record.get("retentionDays") != days:
        return False, "retention setting changed since the last dry run"
    return True, "armed"


# ── The pass ─────────────────────────────────────────────────────────────────
async def prune(
    *,
    table: Any,
    recheck: Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]],
    delete: Callable[[str, str], Awaitable[bool]],
    cascade: Callable[[str, str], Awaitable[List[str]]],
    allowed: bool,
    reason: str,
    now: Optional[datetime] = None,
    limit: int = DEFAULT_MAX_PER_RUN,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PruneReport:
    """Scan, report, and (when ``allowed``) delete. See the module docstring."""
    days = retention_days()
    cutoff = retention_cutoff(now, days)
    report = PruneReport(
        mode="live" if allowed else "dry-run",
        reason=reason,
        retention_days=days,
        cutoff=cutoff.isoformat(),
    )

    candidates: List[tuple] = []
    for item in scan_session_rows(table):
        report.scanned += 1
        held = hold_reason(item, cutoff)
        if held == "recent":
            continue
        if held:
            report.held[held] = report.held.get(held, 0) + 1
            continue
        ids = ids_of(item)
        if ids is None:
            report.held["unidentifiable"] = report.held.get("unidentifiable", 0) + 1
            continue
        candidates.append((parse_timestamp(item.get("lastMessageAt")), ids))

    candidates.sort(key=lambda candidate: candidate[0])
    report.candidates = len(candidates)
    if candidates:
        report.oldest_last_message_at = candidates[0][0].isoformat()
        report.newest_last_message_at = candidates[-1][0].isoformat()
    if len(candidates) > limit:
        report.limit_reached = True
        candidates = candidates[:limit]

    if not allowed:
        return report

    for position, (_, (user_id, session_id)) in enumerate(candidates):
        if position:
            await sleep(sleep_seconds)
        try:
            current = await recheck(session_id, user_id)
        except Exception:  # noqa: BLE001 - leave it for tomorrow
            report.failed += 1
            continue
        if current is None or not is_session_row(current) or hold_reason(current, cutoff) is not None:
            report.skipped_on_recheck += 1
            continue
        try:
            deleted = await delete(user_id, session_id)
        except Exception:  # noqa: BLE001 - leave it for tomorrow
            deleted = False
        if not deleted:
            report.failed += 1
            continue
        report.deleted += 1
        for step in await cascade(user_id, session_id):
            report.cascade_step_failures[step] = report.cascade_step_failures.get(step, 0) + 1
    return report


async def run(*, force_dry_run: bool = False, limit: int, sleep_seconds: float) -> PruneReport:
    """Production wiring: the sessions table, the real cascade, the SSM record."""
    if not conversation_retention_prunes_sessions():
        return PruneReport(mode="off", reason="CONVERSATION_RETENTION_PRUNES_SESSIONS=false")

    from apis.app_api.sessions.services.delete_cascade import default_cascade
    from apis.app_api.sessions.services.session_service import SessionService
    from apis.shared.sessions.metadata import _get_session_by_gsi

    service = SessionService()
    if not service._is_cloud_mode():
        return PruneReport(mode="off", reason="DYNAMODB_SESSIONS_METADATA_TABLE_NAME is not set")

    days = retention_days()
    record = read_dry_run_record()
    allowed, reason = deletes_allowed(days, record)
    if force_dry_run:
        allowed, reason = False, "--dry-run"

    cascade = default_cascade(service)
    report = await prune(
        table=service.table,
        recheck=lambda session_id, user_id: _get_session_by_gsi(session_id, user_id, service.table),
        delete=lambda user_id, session_id: service.delete_session(user_id=user_id, session_id=session_id),
        cascade=cascade.run,
        allowed=allowed,
        reason=reason,
        limit=limit,
        sleep_seconds=sleep_seconds,
    )
    if not allowed:
        write_dry_run_record(report)
    return report


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, whatever the arming flag says")
    parser.add_argument(
        "--limit",
        type=int,
        default=int(_env_number("CONVERSATION_RETENTION_PRUNE_MAX_PER_RUN", DEFAULT_MAX_PER_RUN)),
        help=f"sessions deleted per run at most (default {DEFAULT_MAX_PER_RUN})",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=_env_number("CONVERSATION_RETENTION_PRUNE_SLEEP_SECONDS", DEFAULT_SLEEP_SECONDS),
        help=f"seconds between deletes (default {DEFAULT_SLEEP_SECONDS})",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    report = asyncio.run(run(force_dry_run=args.dry_run, limit=max(0, args.limit), sleep_seconds=max(0.0, args.sleep)))
    logger.info("conversation retention prune: %s", json.dumps(report.to_dict()))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
