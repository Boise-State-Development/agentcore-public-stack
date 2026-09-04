"""Backfill: migrate session-metadata rows to the static sort key (issue #175 Phase 2).

Converts legacy session rows — whose sort key encodes ``lastMessageAt``
(``S#ACTIVE#{lastMessageAt}#{id}`` / ``S#DELETED#{deletedAt}#{id}``) — to the
static ``S#{session_id}`` scheme, populating the sparse ``SessionRecencyIndex``
(GSI4) for active sessions, and deletes the ghost/stub rows the old rotating-SK
writers produced. Once a full pass finds zero legacy rows remaining, it writes the
"migration complete" marker that lets Phase 3 collapse the dual-scheme read to
GSI-only.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to actually write/delete.
* **Idempotent + re-runnable.** The static put uses ``attribute_not_exists(SK)``
  so it never clobbers a row a live writer already migrated (with fresher data);
  the legacy delete is a harmless no-op if already gone.
* **Throttled.** ``--sleep`` between processed items (default 50ms) keeps read/write
  pressure low on the live table.
* **Marker is gated.** ``--set-marker`` re-scans and only writes the marker if zero
  legacy rows remain, so Phase 3 can never be unblocked while data is un-migrated.

Run against dev first, then prod::

    AWS_PROFILE=dev-ai python backend/scripts/backfill_session_static_sk.py \
        --table dev-boisestateai-v2-sessions-metadata            # dry-run
    AWS_PROFILE=dev-ai python backend/scripts/backfill_session_static_sk.py \
        --table dev-boisestateai-v2-sessions-metadata --apply --set-marker
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any, Dict, Optional

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_session_static_sk")

LEGACY_ACTIVE_PREFIX = "S#ACTIVE#"
LEGACY_DELETED_PREFIX = "S#DELETED#"
MARKER_PK = "MIGRATION#session-sk"
MARKER_SK = "STATE"

# Every real session row (legacy or static) carries these; a ghost/stub upsert
# from the old rotating-SK race does not.
REQUIRED_FIELDS = (
    "sessionId", "userId", "title", "status", "createdAt", "lastMessageAt", "messageCount",
)


def _session_id_from_sk(sk: str) -> str:
    """Legacy SK tail is the session id: S#ACTIVE#{ts}#{id} / S#DELETED#{ts}#{id}."""
    return sk.rsplit("#", 1)[-1]


def is_ghost(row: Dict[str, Any]) -> bool:
    """A legacy-SK row that is not a valid session — the stub the migration cleans up.

    Real session rows always have ``GSI_SK == 'META'`` and the required fields; the
    ghost upserts (REMOVE/SET on a rotated-away key) have neither.
    """
    if row.get("GSI_SK") != "META":
        return True
    return any(field not in row for field in REQUIRED_FIELDS)


def build_static_item(row: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """Rewrite a legacy row to the static-SK shape.

    Drops any stale GSI4 keys and re-derives them from status: active rows get the
    sparse recency keys, deleted rows get none (so they leave the active listing).
    """
    user_id = row["userId"]
    is_deleted = row.get("SK", "").startswith(LEGACY_DELETED_PREFIX) or row.get("deleted") is True \
        or row.get("status") == "deleted"

    new = {k: v for k, v in row.items() if k not in ("SK", "GSI4_PK", "GSI4_SK")}
    new["SK"] = f"S#{session_id}"
    new["GSI_PK"] = f"SESSION#{session_id}"
    new["GSI_SK"] = "META"

    if is_deleted:
        new["status"] = "deleted"
        new["deleted"] = True
    else:
        new["GSI4_PK"] = f"USER#{user_id}"
        new["GSI4_SK"] = f"{row['lastMessageAt']}#{session_id}"
    return new


def process_row(table, row: Dict[str, Any], apply: bool, stats: Dict[str, int]) -> None:
    pk, sk = row["PK"], row["SK"]
    session_id = _session_id_from_sk(sk)

    if is_ghost(row):
        stats["ghosts"] += 1
        logger.info("%s ghost delete PK=%s SK=%s", "APPLY" if apply else "DRYRUN", pk, sk[:60])
        if apply:
            table.delete_item(Key={"PK": pk, "SK": sk})
        return

    target_sk = f"S#{session_id}"
    if sk == target_sk:
        stats["already_static"] += 1
        return

    new_item = build_static_item(row, session_id)
    stats["migrated"] += 1
    logger.info("%s migrate %s -> %s", "APPLY" if apply else "DRYRUN", sk[:50], target_sk)
    if not apply:
        return

    # Put the static row only if it doesn't already exist — a live writer may have
    # migrated this session (with fresher data) between our scan and now; don't
    # clobber it. Either way we then drop the legacy orphan.
    try:
        table.put_item(Item=new_item, ConditionExpression=Attr("SK").not_exists())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            stats["skipped_live_migrated"] += 1
            logger.info("  static row already present (live-migrated); dropping legacy orphan only")
        else:
            raise
    table.delete_item(Key={"PK": pk, "SK": sk})


def scan_legacy(table, limit: Optional[int]):
    """Yield rows whose SK is a legacy session prefix, paginating the table."""
    kwargs: Dict[str, Any] = {
        "FilterExpression": Attr("SK").begins_with(LEGACY_ACTIVE_PREFIX)
        | Attr("SK").begins_with(LEGACY_DELETED_PREFIX),
    }
    yielded = 0
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
            yielded += 1
            if limit and yielded >= limit:
                return
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def count_remaining_legacy(table) -> int:
    kwargs: Dict[str, Any] = {
        "FilterExpression": Attr("SK").begins_with(LEGACY_ACTIVE_PREFIX)
        | Attr("SK").begins_with(LEGACY_DELETED_PREFIX),
        "Select": "COUNT",
    }
    total = 0
    while True:
        resp = table.scan(**kwargs)
        total += resp.get("Count", 0)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return total
        kwargs["ExclusiveStartKey"] = lek


def maybe_set_marker(table, apply: bool) -> bool:
    """Set the migration-complete marker iff zero legacy rows remain."""
    remaining = count_remaining_legacy(table)
    if remaining > 0:
        logger.warning(
            "Marker NOT set: %d legacy rows still remain — re-run the backfill until zero.",
            remaining,
        )
        return False
    logger.info("%s set migration-complete marker (%s / %s)",
                "APPLY" if apply else "DRYRUN", MARKER_PK, MARKER_SK)
    if apply:
        table.put_item(Item={"PK": MARKER_PK, "SK": MARKER_SK, "complete": True})
    return True


def run(table_name: str, region: str, apply: bool, sleep: float,
        limit: Optional[int], set_marker: bool) -> Dict[str, int]:
    table = boto3.Session(region_name=region).resource("dynamodb").Table(table_name)
    stats = {"migrated": 0, "ghosts": 0, "already_static": 0, "skipped_live_migrated": 0}

    logger.info("Backfill %s table=%s region=%s%s",
                "APPLY" if apply else "DRY-RUN", table_name, region,
                f" limit={limit}" if limit else "")
    for row in scan_legacy(table, limit):
        process_row(table, row, apply, stats)
        if sleep:
            time.sleep(sleep)

    logger.info("Done: migrated=%(migrated)d ghosts=%(ghosts)d "
                "already_static=%(already_static)d skipped_live_migrated=%(skipped_live_migrated)d",
                stats)

    if set_marker:
        maybe_set_marker(table, apply)
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", default=os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME"),
                   help="sessions-metadata table name (or DYNAMODB_SESSIONS_METADATA_TABLE_NAME)")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--apply", action="store_true", help="actually write/delete (default: dry-run)")
    p.add_argument("--sleep", type=float, default=0.05, help="seconds between items (throttle)")
    p.add_argument("--limit", type=int, default=None, help="max rows to process (for testing)")
    p.add_argument("--set-marker", action="store_true",
                   help="after the pass, set the migration-complete marker if zero legacy remain")
    args = p.parse_args()

    if not args.table:
        p.error("--table is required (or set DYNAMODB_SESSIONS_METADATA_TABLE_NAME)")

    run(args.table, args.region, args.apply, args.sleep, args.limit, args.set_marker)
    if not args.apply:
        logger.info("DRY-RUN only — no changes written. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
