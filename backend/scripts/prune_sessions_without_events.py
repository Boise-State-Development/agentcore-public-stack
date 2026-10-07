"""One-off: delete sessions whose messages are already gone (the 90-day cliff).

Before #1450 raised it to 365, AgentCore Memory expired a session's events after
90 days, and the session row has no TTL. Those conversations are still in their
owners' sidebars and open as "No messages yet": 1,439 of 13,057 active prod
sessions on 2026-10-06 (``docs/specs/conversation-search.md`` §0, §3). The daily
retention pruner will not reach them until their last turn is a year old, so
this script removes them now, once, through the same cascade as a user's delete
(``SessionDeleteCascade``): the soft-delete tombstone, Memory summary records,
files, share snapshots, artifact shares, archived turns. Cost rows stay.

What it deletes
---------------
A live session row (active or archived) whose ``lastMessageAt`` is older than
``--min-age-days`` (default 90, the old expiry) **and** whose content is
verifiably gone everywhere it could be:

* AgentCore Memory ``list_events`` for that actor and session returns nothing;
* the conversation archive holds no object under the session's prefix (PR-3's
  backfill may have rescued a session Memory had dropped; this never deletes
  one it saved).

Every guard of the daily pruner applies: never a row with
``lastTurnContinuable`` or ``pendingInterrupts``, never a row without a
readable ``lastMessageAt``. A check that errors is a "keep", not an "empty".
Each candidate is re-read and both content checks are repeated just before
its delete.

Safety
------
* **Dry-run by default.** ``--apply`` deletes. Run the dry run first and read
  its report (``--out`` writes the candidate list, with ids, to a local file).
* **A canary before anything.** At least one of the five newest sessions in
  the scan must show Memory events. If none does, the Memory id, region or
  credentials are wrong, every session would look empty, and the script stops.
* **Throttled.** Oldest first, ``--sleep`` seconds between deletes (default
  1), at most ``--limit`` per run (default 500). Re-run to continue.
* **``--user``** restricts everything to one owner, for a first apply.
* **The environment is the deployment's.** The cascade reads app-api's
  configuration (table, bucket and Memory names), so the script loads the
  ``app-api`` container environment from the deployed task definition
  (``--task-family``, default ``{prefix}-app-api-task``) rather than trusting a
  local ``.env``. Your ``AWS_*`` credentials and region are left as they are.

Run it after spec §10 q1 is answered and #1450 is in prod. If raising the
expiry did not extend events already written, the cliff keeps growing for 90
days after that deploy: run once then, again 90 days later (or after PR-3's
backfill has rescued what is still alive).

    AWS_PROFILE=prod-ai backend/.venv/bin/python backend/scripts/prune_sessions_without_events.py \\
        --prefix boisestateai-v2 --out /tmp/cliff.jsonl                 # dry run
    AWS_PROFILE=prod-ai backend/.venv/bin/python backend/scripts/prune_sessions_without_events.py \\
        --prefix boisestateai-v2 --user <one-user-id> --apply           # one owner first
    AWS_PROFILE=prod-ai backend/.venv/bin/python backend/scripts/prune_sessions_without_events.py \\
        --prefix boisestateai-v2 --apply --limit 500 --sleep 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.app_api.sessions.services.retention_pruner import (  # noqa: E402
    hold_reason,
    ids_of,
    is_session_row,
    parse_timestamp,
    scan_session_rows,
)

logger = logging.getLogger("prune_sessions_without_events")

DEFAULT_MIN_AGE_DAYS = 90
DEFAULT_LIMIT = 500
DEFAULT_SLEEP_SECONDS = 1.0
CANARY_SESSIONS = 5
APP_API_CONTAINER = "app-api"

#: Never copied from the task definition: the operator's own identity and region.
_KEEP_LOCAL_PREFIXES = ("AWS_",)


# ── Environment ──────────────────────────────────────────────────────────────
def load_task_environment(ecs: Any, family: str, container: str = APP_API_CONTAINER) -> Dict[str, str]:
    """The deployed container's environment (latest ACTIVE revision of ``family``)."""
    definition = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
    for candidate in definition.get("containerDefinitions") or []:
        if candidate.get("name") == container:
            return {item["name"]: item["value"] for item in candidate.get("environment") or []}
    raise RuntimeError(f"task definition {family} has no container named {container!r}")


def apply_environment(values: Dict[str, str]) -> None:
    for name, value in values.items():
        if not name.startswith(_KEEP_LOCAL_PREFIXES):
            os.environ[name] = value


# ── Content checks ───────────────────────────────────────────────────────────
def memory_has_events(client: Any, memory_id: str, user_id: str, session_id: str) -> Optional[bool]:
    """True/False, or None when the answer is unknown (any error)."""
    try:
        page = client.list_events(memoryId=memory_id, actorId=user_id, sessionId=session_id, maxResults=1)
    except Exception as exc:  # noqa: BLE001 - classified below
        code = str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code") or "")
        if code == "ResourceNotFoundException":
            # The session is unknown to a Memory that exists (checked up front).
            return False
        logger.warning("list_events failed (%s); keeping the session", code or type(exc).__name__)
        return None
    return bool(page.get("events"))


def archive_has_objects(s3: Any, bucket: Optional[str], user_id: str, session_id: str) -> Optional[bool]:
    """True/False, or None when the answer is unknown. No bucket means no archive."""
    if not bucket:
        return False
    from apis.shared.conversation_archive.documents import session_prefix

    try:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=session_prefix(user_id, session_id), MaxKeys=1)
    except Exception as exc:  # noqa: BLE001 - a failed check is a keep
        logger.warning("archive listing failed (%s); keeping the session", type(exc).__name__)
        return None
    return bool(page.get("Contents"))


def content_gone(memory: Callable[[str, str], Optional[bool]], archive: Callable[[str, str], Optional[bool]],
                 user_id: str, session_id: str) -> Tuple[bool, str]:
    """``(gone, reason)``. Gone only when both checks answered "nothing there"."""
    events = memory(user_id, session_id)
    if events is None:
        return False, "memory_unverifiable"
    if events:
        return False, "has_events"
    objects = archive(user_id, session_id)
    if objects is None:
        return False, "archive_unverifiable"
    if objects:
        return False, "archived"
    return True, ""


# ── Report ───────────────────────────────────────────────────────────────────
@dataclass
class CliffReport:
    mode: str = "dry-run"
    min_age_days: int = DEFAULT_MIN_AGE_DAYS
    scanned: int = 0
    old_enough: int = 0
    candidates: int = 0
    oldest_last_message_at: Optional[str] = None
    newest_last_message_at: Optional[str] = None
    held: Dict[str, int] = field(default_factory=dict)
    deleted: int = 0
    skipped_on_recheck: int = 0
    failed: int = 0
    cascade_step_failures: Dict[str, int] = field(default_factory=dict)
    limit_reached: bool = False
    aborted: Optional[str] = None

    def hold(self, reason: str) -> None:
        self.held[reason] = self.held.get(reason, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "minAgeDays": self.min_age_days,
            "scanned": self.scanned,
            "oldEnough": self.old_enough,
            "candidates": self.candidates,
            "oldestLastMessageAt": self.oldest_last_message_at,
            "newestLastMessageAt": self.newest_last_message_at,
            "held": self.held,
            "deleted": self.deleted,
            "skippedOnRecheck": self.skipped_on_recheck,
            "failed": self.failed,
            "cascadeStepFailures": self.cascade_step_failures,
            "limitReached": self.limit_reached,
            "aborted": self.aborted,
        }


# ── The pass ─────────────────────────────────────────────────────────────────
async def run(
    *,
    table: Any,
    memory: Callable[[str, str], Optional[bool]],
    archive: Callable[[str, str], Optional[bool]],
    recheck: Callable[[str, str], Any],
    delete: Callable[[str, str], Any],
    cascade: Callable[[str, str], Any],
    apply: bool,
    now: datetime,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    user: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep: Callable[[float], Any] = asyncio.sleep,
    out: Optional[List[Dict[str, str]]] = None,
) -> CliffReport:
    """Scan, verify, report, and (with ``apply``) delete. See the module docstring."""
    report = CliffReport(mode="apply" if apply else "dry-run", min_age_days=min_age_days)
    cutoff = now - timedelta(days=min_age_days)

    rows: List[Tuple[datetime, str, str]] = []
    every: List[Tuple[datetime, str, str]] = []
    for item in scan_session_rows(table):
        report.scanned += 1
        ids = ids_of(item)
        last = parse_timestamp(item.get("lastMessageAt"))
        if ids is None or last is None:
            continue
        every.append((last, *ids))
        if user and ids[0] != user:
            continue
        held = hold_reason(item, cutoff)
        if held == "recent":
            continue
        report.old_enough += 1
        if held:
            report.hold(held)
            continue
        rows.append((last, *ids))

    # The canary: recent sessions must show events, or every check below
    # would read "empty" for the wrong reason. One of the newest few is
    # enough; a single fresh row can legitimately have none yet.
    newest = sorted(every, key=lambda row: row[0], reverse=True)[:CANARY_SESSIONS]
    if not newest:
        report.aborted = "no session with a readable lastMessageAt to use as a canary"
        return report
    if not any(memory(u, s) is True for _, u, s in newest):
        report.aborted = (
            f"canary failed: none of the {len(newest)} newest sessions shows Memory events "
            "(wrong memory id, region or credentials?)"
        )
        return report

    rows.sort(key=lambda row: row[0])
    candidates: List[Tuple[datetime, str, str]] = []
    for last, user_id, session_id in rows:
        gone, reason = content_gone(memory, archive, user_id, session_id)
        if not gone:
            report.hold(reason)
            continue
        candidates.append((last, user_id, session_id))

    report.candidates = len(candidates)
    if candidates:
        report.oldest_last_message_at = candidates[0][0].isoformat()
        report.newest_last_message_at = candidates[-1][0].isoformat()
    if out is not None:
        out.extend(
            {"userId": u, "sessionId": s, "lastMessageAt": last.isoformat()} for last, u, s in candidates
        )
    if len(candidates) > limit:
        report.limit_reached = True
        candidates = candidates[:limit]

    if not apply:
        return report

    for position, (_, user_id, session_id) in enumerate(candidates):
        if position:
            await sleep(sleep_seconds)
        try:
            current = await recheck(session_id, user_id)
        except Exception:  # noqa: BLE001
            report.failed += 1
            continue
        if current is None or not is_session_row(current) or hold_reason(current, cutoff) is not None:
            report.skipped_on_recheck += 1
            continue
        if not content_gone(memory, archive, user_id, session_id)[0]:
            report.skipped_on_recheck += 1
            continue
        try:
            deleted = await delete(user_id, session_id)
        except Exception:  # noqa: BLE001
            deleted = False
        if not deleted:
            report.failed += 1
            continue
        report.deleted += 1
        for step in await cascade(user_id, session_id):
            report.cascade_step_failures[step] = report.cascade_step_failures.get(step, 0) + 1
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Delete sessions whose messages are already gone (one-off).")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--prefix", help="deployment prefix; the task family is {prefix}-app-api-task")
    target.add_argument("--task-family", help="app-api task definition family, if not {prefix}-app-api-task")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    parser.add_argument("--user", help="only this owner's sessions")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="deletes per run at most")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS, help="seconds between deletes")
    parser.add_argument("--out", help="write the candidate list (with ids) to this local JSONL file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.min_age_days < 3:
        parser.error("--min-age-days must be at least 3 (Memory's minimum event expiry)")

    import boto3

    family = args.task_family or f"{args.prefix}-app-api-task"
    apply_environment(load_task_environment(boto3.client("ecs"), family))

    from apis.app_api.sessions.services.delete_cascade import default_cascade
    from apis.app_api.sessions.services.session_service import SessionService
    from apis.shared.conversation_archive import archive_bucket_name
    from apis.shared.sessions.metadata import _get_session_by_gsi

    memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
    if not memory_id:
        parser.error(f"{family} has no AGENTCORE_MEMORY_ID")
    agentcore = boto3.client("bedrock-agentcore")
    # Proves the Memory exists, so a ResourceNotFound later means "no such session".
    boto3.client("bedrock-agentcore-control").get_memory(memoryId=memory_id)

    service = SessionService()
    if not service._is_cloud_mode():
        parser.error(f"{family} has no DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    s3 = boto3.client("s3")
    bucket = archive_bucket_name()
    cascade = default_cascade(service)
    candidates: List[Dict[str, str]] = []

    report = asyncio.run(
        run(
            table=service.table,
            memory=lambda u, s: memory_has_events(agentcore, memory_id, u, s),
            archive=lambda u, s: archive_has_objects(s3, bucket, u, s),
            recheck=lambda s, u: _get_session_by_gsi(s, u, service.table),
            delete=lambda u, s: service.delete_session(user_id=u, session_id=s),
            cascade=cascade.run,
            apply=args.apply,
            now=datetime.now(timezone.utc),
            min_age_days=args.min_age_days,
            user=args.user,
            limit=max(0, args.limit),
            sleep_seconds=max(0.0, args.sleep),
            out=candidates if args.out else None,
        )
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for row in candidates:
                handle.write(json.dumps(row) + "\n")
        logger.info("wrote %d candidate(s) to %s", len(candidates), args.out)
    logger.info("cliff prune: %s", json.dumps(report.to_dict()))
    return 1 if report.aborted or report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
