"""Backfill: copy every live conversation into the conversation archive (PR-3).

The runtime archives a turn when it finishes (PR-2a), so the archive only holds
turns written since ``CONVERSATION_INDEX_ENABLED`` was turned on. This script
writes the rest: for every live session (active or archived) whose AgentCore
Memory events still exist, it reads the history, cuts it into turns with the
same pure code the runtime uses (``conversation_archive.split_turns``), and
puts one object per turn at ``conversations/{user}/{session}/{index:06d}.json``.
The index consumer (PR-2b) indexes each object as it lands; this script never
talks to the knowledge base. ``docs/specs/conversation-search.md`` §3, §4, §7.

It is also the rescue for conversations Memory is about to drop: once a turn is
in the archive, ``GET /sessions/{id}/messages`` reads it from there when Memory
has nothing. Sessions are processed oldest first, so the ones nearest their
event expiry go first.

What it writes
--------------
Objects byte-identical to the runtime's for the same history:

* **The user's words** come from the same clean source the runtime uses: the
  session's ``displayText`` (``D#``) row for that message when one exists (the
  prompt sent to the model was augmented), otherwise the stored message with
  injected ``<user_context>`` blocks and the attachments marker stripped
  (``message_text`` / ``clean_user_text``, #1466).
* **Project and agent** come from the turn's own cost rows (``turnProjectId`` /
  ``turnAgentId``, which name the agent an ``@``-mention turn ran on), else
  from the session's preferences.
* **``createdAt``** is the time of the turn's last message, which is what the
  runtime stamps when it archives a turn right after it ends.
* **Forks** are ordinary sessions under the forker's id; their copied events
  already carry the display text, and positions count exactly as the runtime
  and the messages route count them.

What it skips
-------------
* deleted sessions; preview sessions (never archived, anywhere);
* sessions that moved in the last 15 minutes (a turn may be in flight; the
  runtime archives it when it ends, and a re-run picks the rest up);
* every key that already exists. The check is a listing per session, and the
  put itself is conditional (``If-None-Match: *``), so a turn the runtime
  archived between the two is never overwritten with an older copy.

Throttle
--------
The consumer drains its queue at most two batches of ten at a time, so the
knowledge base never sees more than that whatever this script does. The pause
here keeps that drain short and gentle on the per-account document quotas
(20 ingests/s, 10 concurrent operations) that every agent's uploads share:
``--sleep`` seconds (default 0.2) after every Memory read and every put, so at
most ~5 objects a second, about half an ingest call a second downstream.

Safety
------
* **Dry-run by default.** It reads Memory and the archive and writes nothing.
  The report counts sessions, turns, objects already present and the bytes
  and minutes an apply would take; ``--out`` writes the per-session plan.
* **``--apply`` needs ``--confirm-prefix``** equal to ``--project-prefix``, and
  refuses to run where the deployment has ``CONVERSATION_INDEX_ENABLED`` off:
  that flag is how a deployment opts out of a second copy of its transcripts.
  **``--archive-without-index``** overrides that refusal, deliberately, for a
  deployment that wants the archive as a rescue before it has search (the
  messages route reads the archive whatever the flag says). Nothing is indexed
  then: the bucket's index rules are disabled, so the objects' events are
  dropped, and turning the index on later does not index them by itself.
* **``--user``** restricts everything to one owner, for a first apply.
* **Idempotent and resumable.** Re-run until ``toWrite`` is 0.
* **The environment is the deployment's**: app-api's container environment is
  loaded from its task definition (``{prefix}-app-api-task``), as the cliff
  prune script does. Your ``AWS_*`` credentials and region are left alone.

Run the first real pass only after spec §10 q1 is recorded (whether raising
Memory's event expiry extended events already written): it decides whether
this is also the rescue for sessions 90–365 days old.

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_conversation_archive.py \\
        --project-prefix dev-boisestateai-v2 --out /tmp/backfill-plan.jsonl          # dry run
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_conversation_archive.py \\
        --project-prefix dev-boisestateai-v2 --user <one-user-id> \\
        --apply --confirm-prefix dev-boisestateai-v2                                   # one owner
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_conversation_archive.py \\
        --project-prefix dev-boisestateai-v2 --apply --confirm-prefix dev-boisestateai-v2
    AWS_PROFILE=<prod> backend/.venv/bin/python backend/scripts/backfill_conversation_archive.py \\
        --project-prefix boisestateai-v2 --apply --confirm-prefix boisestateai-v2 \\
        --archive-without-index                                                        # index off
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from apis.app_api.sessions.services.retention_pruner import ids_of, is_session_row, parse_timestamp  # noqa: E402
from apis.shared.conversation_archive.documents import (  # noqa: E402
    ArchivedTurn,
    is_turn_start,
    session_prefix,
    split_turns,
)
from apis.shared.sessions.preview import is_preview_session  # noqa: E402

logger = logging.getLogger("backfill_conversation_archive")

DEFAULT_SLEEP_SECONDS = 0.2
#: A session that moved this recently may have a turn in flight.
RECENT_ACTIVITY_GRACE = timedelta(minutes=15)
#: What ``AgentCoreMemorySessionManager.list_messages`` fetches; the same here.
MAX_EVENTS = 10000

_SCAN_ATTRIBUTES = ("PK", "SK", "userId", "sessionId", "#status", "deleted", "createdAt", "lastMessageAt", "preferences")

#: One message of a history: its Converse dict and its ``created_at``.
HistoryMessage = Tuple[Mapping[str, Any], Optional[str]]


# ── History → turns (pure) ───────────────────────────────────────────────────
@dataclass
class TurnContext:
    """What a session's metadata rows say about its turns.

    ``display_texts``: user message index → the words the user typed.
    ``calls``: ``(assistant message index, turnAgentId, turnProjectId)`` per
    cost row, ids None where the row does not carry them.
    """

    display_texts: Dict[int, str] = field(default_factory=dict)
    calls: List[Tuple[int, Optional[str], Optional[str]]] = field(default_factory=list)


def _iso(value: Optional[str]) -> Optional[str]:
    parsed = parse_timestamp(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed else None


def _turn_spans(messages: Sequence[Mapping[str, Any]]) -> Dict[int, int]:
    """Turn start position → end position (exclusive), as ``split_turns`` cuts."""
    starts = [pos for pos, message in enumerate(messages) if is_turn_start(message)]
    return {start: end for start, end in zip(starts, starts[1:] + [len(messages)])}


def session_turns(
    history: Sequence[HistoryMessage],
    *,
    user_id: str,
    session_id: str,
    context: TurnContext,
    project_id: Optional[str],
    assistant_id: Optional[str],
    fallback_created_at: str,
) -> List[ArchivedTurn]:
    """The archived turns of one session, as the runtime would have written them.

    A turn whose user message has a ``displayText`` row is cut from a copy of
    the history with that message's text replaced by it, the same words the
    runtime archives (``original_message``). Everything else is
    ``split_turns`` unchanged.
    """
    messages: List[Mapping[str, Any]] = []
    for position, (message, _) in enumerate(history):
        display = context.display_texts.get(position)
        if display and is_turn_start(message):
            message = {"role": "user", "content": [{"text": display}]}
        messages.append(message)

    spans = _turn_spans(messages)
    turns = []
    for turn in split_turns(messages, user_id=user_id, session_id=session_id, created_at=""):
        end = spans[turn.message_index]
        agent, project = assistant_id, project_id
        for index, call_agent, call_project in context.calls:
            if turn.message_index < index < end:
                agent, project = call_agent or assistant_id, call_project or project_id
                break
        turns.append(
            replace(
                turn,
                created_at=_iso(history[end - 1][1]) or fallback_created_at,
                project_id=project or None,
                assistant_id=agent or None,
            )
        )
    return turns


# ── Reads ────────────────────────────────────────────────────────────────────
def scan_live_sessions(table: Any) -> Iterator[Dict[str, Any]]:
    """Every live (active or archived) session row, with what the backfill needs."""
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


def _query_session_rows(table: Any, session_id: str, sk_prefix: str, projection: str) -> Iterator[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    params: Dict[str, Any] = {
        "IndexName": "SessionLookupIndex",
        "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").begins_with(sk_prefix),
        "ProjectionExpression": projection,
    }
    while True:
        page = table.query(**params)
        yield from page.get("Items") or []
        start = page.get("LastEvaluatedKey")
        if not start:
            return
        params["ExclusiveStartKey"] = start


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_turn_context(table: Any, user_id: str, session_id: str) -> TurnContext:
    """The session's ``D#`` (display text) and ``C#`` (cost) rows, owner-checked."""
    context = TurnContext()
    for row in _query_session_rows(table, session_id, "D#", "userId, messageId, displayText"):
        index, text = _int(row.get("messageId")), row.get("displayText")
        if row.get("userId") == user_id and index is not None and isinstance(text, str) and text.strip():
            context.display_texts[index] = text
    for row in _query_session_rows(table, session_id, "C#", "userId, messageId, turnAgentId, turnProjectId"):
        index = _int(row.get("messageId"))
        if row.get("userId") == user_id and index is not None:
            context.calls.append((index, row.get("turnAgentId") or None, row.get("turnProjectId") or None))
    context.calls.sort()
    return context


def read_history(memory_client: Any, memory_id: str, user_id: str, session_id: str) -> List[HistoryMessage]:
    """The session's messages exactly as the runtime restores them. Raises on failure.

    The same pipeline as ``AgentCoreMemorySessionManager.list_messages`` (which
    the runtime and the messages route use), minus its habit of turning a
    failed read into an empty history: here an error is an error.
    """
    from bedrock_agentcore.memory.integrations.strands.bedrock_converter import AgentCoreMemoryConverter
    from botocore.exceptions import ClientError

    try:
        events = memory_client.list_events(
            memory_id=memory_id, actor_id=user_id, session_id=session_id, max_results=MAX_EVENTS
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return []
        raise
    return [(m.to_message(), m.created_at) for m in AgentCoreMemoryConverter.events_to_messages(events)]


def existing_keys(s3: Any, bucket: str, user_id: str, session_id: str) -> Set[str]:
    keys: Set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=session_prefix(user_id, session_id)):
        keys.update(obj["Key"] for obj in page.get("Contents", []))
    return keys


def put_if_absent(s3: Any, bucket: str, turn: ArchivedTurn) -> str:
    """``"written"``, or ``"exists"`` when the key appeared since the listing."""
    from botocore.exceptions import ClientError

    try:
        s3.put_object(
            Bucket=bucket,
            Key=turn.key,
            Body=turn.to_json(),
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
            return "exists"
        raise
    return "written"


# ── Report ───────────────────────────────────────────────────────────────────
@dataclass
class BackfillReport:
    mode: str = "dry-run"
    scanned: int = 0
    held: Dict[str, int] = field(default_factory=dict)
    sessions_read: int = 0
    sessions_without_events: int = 0
    sessions_with_turns: int = 0
    turns: int = 0
    already_archived: int = 0
    to_write: int = 0
    bytes_to_write: int = 0
    written: int = 0
    raced: int = 0
    failed_sessions: int = 0
    failed_puts: int = 0
    oldest_created_at: Optional[str] = None
    limit_reached: bool = False
    estimated_apply_minutes: float = 0.0

    def hold(self, reason: str) -> None:
        self.held[reason] = self.held.get(reason, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "scanned": self.scanned,
            "held": self.held,
            "sessionsRead": self.sessions_read,
            "sessionsWithoutEvents": self.sessions_without_events,
            "sessionsWithTurns": self.sessions_with_turns,
            "turns": self.turns,
            "alreadyArchived": self.already_archived,
            "toWrite": self.to_write,
            "bytesToWrite": self.bytes_to_write,
            "written": self.written,
            "raced": self.raced,
            "failedSessions": self.failed_sessions,
            "failedPuts": self.failed_puts,
            "oldestCreatedAt": self.oldest_created_at,
            "limitReached": self.limit_reached,
            "estimatedApplyMinutes": round(self.estimated_apply_minutes, 1),
        }


# ── The pass ─────────────────────────────────────────────────────────────────
def _preferences(row: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    prefs = row.get("preferences")
    if not isinstance(prefs, dict):
        return None, None
    return prefs.get("projectId") or None, prefs.get("assistantId") or None


def _age_key(row: Mapping[str, Any]) -> datetime:
    return (
        parse_timestamp(row.get("createdAt"))
        or parse_timestamp(row.get("lastMessageAt"))
        or datetime.max.replace(tzinfo=timezone.utc)
    )


def run(
    *,
    rows: Iterable[Dict[str, Any]],
    history: Callable[[str, str], List[HistoryMessage]],
    context: Callable[[str, str], TurnContext],
    existing: Callable[[str, str], Set[str]],
    put: Callable[[ArchivedTurn], str],
    apply: bool,
    now: datetime,
    user: Optional[str] = None,
    limit: Optional[int] = None,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
    out: Optional[List[Dict[str, Any]]] = None,
) -> BackfillReport:
    """Scan, read, cut and (with ``apply``) write. See the module docstring."""
    report = BackfillReport(mode="apply" if apply else "dry-run")
    recent_cutoff = now - RECENT_ACTIVITY_GRACE

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        report.scanned += 1
        ids = ids_of(row)
        if ids is None:
            report.hold("unreadable_ids")
            continue
        if user and ids[0] != user:
            continue
        if is_preview_session(ids[1]):
            report.hold("preview")
            continue
        last = parse_timestamp(row.get("lastMessageAt"))
        if last is not None and last >= recent_cutoff:
            report.hold("recently_active")
            continue
        candidates.append(row)
    candidates.sort(key=_age_key)
    if limit is not None and len(candidates) > limit:
        report.limit_reached = True
        candidates = candidates[:limit]

    for row in candidates:
        user_id, session_id = ids_of(row)
        project_id, assistant_id = _preferences(row)
        fallback = _iso(row.get("lastMessageAt")) or now.isoformat()
        try:
            messages = history(user_id, session_id)
            report.sessions_read += 1
            if sleep_seconds:
                sleep(sleep_seconds)
            if not messages:
                report.sessions_without_events += 1
                continue
            turns = session_turns(
                messages,
                user_id=user_id,
                session_id=session_id,
                context=context(user_id, session_id),
                project_id=project_id,
                assistant_id=assistant_id,
                fallback_created_at=fallback,
            )
            present = existing(user_id, session_id) if turns else set()
        except Exception as exc:  # noqa: BLE001 - one session's failure must not stop the pass
            logger.warning("session skipped after a read failure (%s)", type(exc).__name__)
            report.failed_sessions += 1
            continue

        missing = [turn for turn in turns if turn.key not in present]
        report.turns += len(turns)
        report.already_archived += len(turns) - len(missing)
        report.to_write += len(missing)
        if turns:
            report.sessions_with_turns += 1
            if report.oldest_created_at is None or turns[0].created_at < report.oldest_created_at:
                report.oldest_created_at = turns[0].created_at
        report.bytes_to_write += sum(len(turn.to_json()) for turn in missing)
        if out is not None:
            out.append(
                {
                    "userId": user_id,
                    "sessionId": session_id,
                    "status": row.get("status"),
                    "turns": len(turns),
                    "toWrite": len(missing),
                }
            )
        if not apply:
            continue
        for turn in missing:
            try:
                outcome = put(turn)
            except Exception as exc:  # noqa: BLE001 - counted; a re-run retries it
                logger.warning("put failed (%s)", type(exc).__name__)
                report.failed_puts += 1
                continue
            if outcome == "written":
                report.written += 1
            else:
                report.raced += 1
            if sleep_seconds:
                sleep(sleep_seconds)

    if not apply:
        report.estimated_apply_minutes = (report.sessions_read + report.to_write) * sleep_seconds / 60
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Copy live conversations into the conversation archive.")
    parser.add_argument("--project-prefix", required=True, help="deployment prefix, e.g. dev-boisestateai-v2")
    parser.add_argument("--task-family", help="app-api task definition family, if not {prefix}-app-api-task")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--confirm-prefix", default=None, help="required with --apply; must equal --project-prefix")
    parser.add_argument("--user", help="only this owner's sessions")
    parser.add_argument("--limit", type=int, default=None, help="sessions per run at most (oldest first)")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS,
                        help="seconds after every Memory read and every put")
    parser.add_argument("--out", help="write the per-session plan (with ids) to this local JSONL file")
    parser.add_argument("--archive-without-index", action="store_true",
                        help="allow --apply where CONVERSATION_INDEX_ENABLED is off (archive only, nothing indexed)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.apply and args.confirm_prefix != args.project_prefix:
        parser.error("--apply requires --confirm-prefix equal to --project-prefix")

    import boto3
    from prune_sessions_without_events import apply_environment, load_task_environment

    family = args.task_family or f"{args.project_prefix}-app-api-task"
    apply_environment(load_task_environment(boto3.client("ecs"), family))

    from apis.shared.conversation_archive import archive_bucket_name
    from apis.shared.feature_flags import conversation_index_enabled

    if args.apply and not conversation_index_enabled():
        if not args.archive_without_index:
            parser.error(
                f"{family} has CONVERSATION_INDEX_ENABLED off; this deployment does not archive conversations"
                " (--archive-without-index writes the archive anyway, without indexing it)"
            )
        logger.warning("CONVERSATION_INDEX_ENABLED is off: archiving only, nothing written here will be indexed")
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
    table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "").strip()
    bucket = archive_bucket_name()
    if not memory_id or not table_name or not bucket:
        parser.error(f"{family} lacks AGENTCORE_MEMORY_ID, the sessions table or the archive bucket")

    from bedrock_agentcore.memory import MemoryClient

    region = os.environ.get("AWS_REGION") or boto3.session.Session().region_name
    # Proves the Memory exists, so a ResourceNotFound later means "no such session".
    boto3.client("bedrock-agentcore-control", region_name=region).get_memory(memoryId=memory_id)
    memory_client = MemoryClient(region_name=region)
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    s3 = boto3.client("s3", region_name=region)
    plan: List[Dict[str, Any]] = []

    report = run(
        rows=scan_live_sessions(table),
        history=lambda u, s: read_history(memory_client, memory_id, u, s),
        context=lambda u, s: load_turn_context(table, u, s),
        existing=lambda u, s: existing_keys(s3, bucket, u, s),
        put=lambda turn: put_if_absent(s3, bucket, turn),
        apply=args.apply,
        now=datetime.now(timezone.utc),
        user=args.user,
        limit=args.limit,
        sleep_seconds=max(0.0, args.sleep),
        out=plan if args.out else None,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for row in plan:
                handle.write(json.dumps(row) + "\n")
        logger.info("wrote %d session plan(s) to %s", len(plan), args.out)
    logger.info("conversation archive backfill: %s", json.dumps(report.to_dict()))
    if not args.apply:
        logger.info("DRY-RUN only — nothing written. Re-run with --apply --confirm-prefix %s.", args.project_prefix)
    return 1 if report.failed_sessions or report.failed_puts else 0


if __name__ == "__main__":
    raise SystemExit(main())
