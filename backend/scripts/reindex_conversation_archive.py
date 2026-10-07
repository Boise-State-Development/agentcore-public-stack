"""Re-index the conversation archive: queue every archived turn for the index consumer.

For a deployment whose conversation-search knowledge base already holds
documents under an older id format. The first format,
``conv#{session_id}#{message_index}``, left the user out, so two users with the
same session id shared one document (``docs/specs/conversation-search.md`` §4).
The consumer now writes ``conv#{user_id}#{session_id}#{message_index}``, but it
only acts on S3 events, and turns archived before the change raise none. This
script raises them: one synthetic *Object Created* message per archived turn,
in the shape EventBridge delivers, sent to the consumer's own queue. The
consumer reads each object as it is now and ingests it under the current id,
at its usual pace (batches of ten, two at a time), so the knowledge base sees
nothing it would not see from ordinary traffic. The daily reconciler deletes
the old-format documents as orphans.

Nothing in the archive changes. Copying each object onto itself would raise the
same events, but it would also restart every object's retention clock.

Safety
------
* **Dry-run by default**: lists and counts, sends nothing.
* **``--apply`` needs ``--confirm-prefix``** equal to ``--project-prefix``.
* **``--user``** limits it to one owner's turns.
* **Throttled**: ten messages per ``SendMessageBatch``, ``--sleep`` seconds
  (default 1) between batches, so at most ~10 turns a second reach the queue.
* **Idempotent**: re-ingesting a document id replaces it, so a re-run, or a
  turn the runtime also archives meanwhile, only repeats work.

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/reindex_conversation_archive.py \\
        --project-prefix dev-boisestateai-v2                                         # dry run
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/reindex_conversation_archive.py \\
        --project-prefix dev-boisestateai-v2 --apply --confirm-prefix dev-boisestateai-v2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.shared.conversation_archive.documents import ARCHIVE_PREFIX, parse_archive_key  # noqa: E402

logger = logging.getLogger("reindex_conversation_archive")

DEFAULT_SLEEP_SECONDS = 1.0
#: ``SendMessageBatch`` takes at most ten entries.
BATCH = 10


def archive_keys(s3: Any, bucket: str, user: Optional[str] = None) -> Iterator[str]:
    """Every archived-turn key, optionally one owner's only."""
    prefix = f"{ARCHIVE_PREFIX}{user}/" if user else ARCHIVE_PREFIX
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            if parse_archive_key(obj["Key"]) is not None:
                yield obj["Key"]


def object_created_event(bucket: str, key: str) -> Dict[str, Any]:
    """The EventBridge *Object Created* event the consumer reads (``extract_records``)."""
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": bucket}, "object": {"key": key}, "reason": "Reindex"},
    }


@dataclass
class ReindexReport:
    mode: str = "dry-run"
    turns: int = 0
    sent: int = 0
    failed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "turns": self.turns, "sent": self.sent, "failed": self.failed}


def run(
    *,
    keys: Iterator[str],
    bucket: str,
    send: Callable[[List[Dict[str, str]]], List[str]],
    apply: bool,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
) -> ReindexReport:
    """Queue one event per key, ten to a batch. ``send`` returns the ids it failed."""
    report = ReindexReport(mode="apply" if apply else "dry-run")
    batch: List[Dict[str, str]] = []

    def flush() -> None:
        if not batch:
            return
        if report.sent or report.failed:
            sleep(sleep_seconds)
        failed = send(list(batch))
        report.failed += len(failed)
        report.sent += len(batch) - len(failed)
        batch.clear()

    for key in keys:
        report.turns += 1
        if not apply:
            continue
        batch.append({"Id": str(len(batch)), "MessageBody": json.dumps(object_created_event(bucket, key))})
        if len(batch) == BATCH:
            flush()
    flush()
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Queue every archived turn for the conversation index consumer.")
    parser.add_argument("--project-prefix", required=True, help="deployment prefix, e.g. dev-boisestateai-v2")
    parser.add_argument("--apply", action="store_true", help="send (default: dry run)")
    parser.add_argument("--confirm-prefix", default=None, help="required with --apply; must equal --project-prefix")
    parser.add_argument("--user", help="only this owner's turns")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS, help="seconds between batches of ten")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.apply and args.confirm_prefix != args.project_prefix:
        parser.error("--apply requires --confirm-prefix equal to --project-prefix")

    import boto3

    bucket = f"{args.project_prefix}-conversation-archive"
    sqs = boto3.client("sqs")
    queue_url = sqs.get_queue_url(QueueName=f"{args.project_prefix}-conversation-index")["QueueUrl"]

    def send(entries: List[Dict[str, str]]) -> List[str]:
        try:
            response = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        except Exception as exc:  # noqa: BLE001 - counted; a re-run repeats it
            logger.warning("SendMessageBatch failed (%s)", type(exc).__name__)
            return [entry["Id"] for entry in entries]
        return [failure["Id"] for failure in response.get("Failed") or []]

    report = run(
        keys=archive_keys(boto3.client("s3"), bucket, args.user),
        bucket=bucket,
        send=send,
        apply=args.apply,
        sleep_seconds=max(0.0, args.sleep),
    )
    logger.info("conversation archive reindex: %s", json.dumps(report.to_dict()))
    if not args.apply:
        logger.info("DRY-RUN only — nothing sent. Re-run with --apply --confirm-prefix %s.", args.project_prefix)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
