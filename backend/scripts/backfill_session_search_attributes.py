"""Backfill: write ``titleLower`` on existing session rows (conversation search, PR-4).

The lexical leg of ``GET /sessions/search`` (``docs/specs/conversation-search.md``
§5) matches a DynamoDB ``contains()`` on ``titleLower``, the title normalized by
``apis.shared.sessions.search_text.normalize_search_text``. New titles and
renames write it as they happen; this copies ``title → titleLower`` for rows
written before that, so their titles are found by the server-side search and
not only by the sidebar's client-side filter over loaded pages.

``firstPrompt`` is not backfilled: it is written when a conversation's first
title is generated, and for older conversations the text leg (the index over
every archived turn) already finds the opening prompt.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write.
* **Only ever sets ``titleLower``**, and only on a live session row (``SK``
  ``S#…``, ``GSI_SK = META``) whose ``titleLower`` is missing or stale. The
  write is conditional on the row still holding the title that was read, so a
  rename that lands between the scan and the write is never overwritten with
  the old title's normalization (the rename wrote its own).
* **Idempotent + re-runnable.** A second run finds nothing to do.
* **Throttled.** ``--sleep`` between writes (default 50 ms).

Run against dev first, then prod::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_session_search_attributes.py \\
        --table dev-boisestateai-v2-sessions-metadata              # dry-run
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_session_search_attributes.py \\
        --table dev-boisestateai-v2-sessions-metadata --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, Iterator, Optional

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.shared.sessions.search_text import normalize_search_text  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_session_search_attributes")


def wanted_title_lower(row: Dict[str, Any]) -> Optional[str]:
    """The ``titleLower`` this row should carry, or None when it needs no write."""
    title = row.get("title")
    if not isinstance(title, str) or not title:
        return None
    if row.get("status") == "deleted" or row.get("deleted") is True:
        return None
    value = normalize_search_text(title)
    if not value or row.get("titleLower") == value:
        return None
    return value


def scan_session_rows(table, limit: Optional[int]) -> Iterator[Dict[str, Any]]:
    """Every session row (``SK`` ``S#…`` with ``GSI_SK = META``), paginated."""
    kwargs: Dict[str, Any] = {
        "FilterExpression": Attr("SK").begins_with("S#") & Attr("GSI_SK").eq("META"),
        "ProjectionExpression": "PK, SK, title, titleLower, #st, deleted",
        "ExpressionAttributeNames": {"#st": "status"},
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


def process_row(table, row: Dict[str, Any], apply: bool, stats: Dict[str, int]) -> bool:
    """Write one row's ``titleLower`` if it needs it. True when a write was due."""
    value = wanted_title_lower(row)
    if value is None:
        stats["unchanged"] += 1
        return False
    stats["updated"] += 1
    if not apply:
        return True
    try:
        table.update_item(
            Key={"PK": row["PK"], "SK": row["SK"]},
            UpdateExpression="SET titleLower = :tl",
            ConditionExpression="attribute_exists(PK) AND title = :t",
            ExpressionAttributeValues={":tl": value, ":t": row["title"]},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        stats["updated"] -= 1
        stats["changed_since_scan"] += 1
    return True


def run(table_name: str, region: str, apply: bool, sleep: float, limit: Optional[int]) -> Dict[str, int]:
    table = boto3.Session(region_name=region).resource("dynamodb").Table(table_name)
    stats = {"updated": 0, "unchanged": 0, "changed_since_scan": 0}
    logger.info(
        "Backfill titleLower %s table=%s region=%s%s",
        "APPLY" if apply else "DRY-RUN", table_name, region, f" limit={limit}" if limit else "",
    )
    for row in scan_session_rows(table, limit):
        if process_row(table, row, apply, stats) and apply and sleep:
            time.sleep(sleep)
    logger.info(
        "Done: %s=%d unchanged=%d changed_since_scan=%d",
        "updated" if apply else "would_update", stats["updated"], stats["unchanged"], stats["changed_since_scan"],
    )
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", default=os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME"),
                   help="sessions-metadata table name (or DYNAMODB_SESSIONS_METADATA_TABLE_NAME)")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    p.add_argument("--sleep", type=float, default=0.05, help="seconds between writes (throttle)")
    p.add_argument("--limit", type=int, default=None, help="max rows to examine (for testing)")
    args = p.parse_args()
    if not args.table:
        p.error("--table is required (or set DYNAMODB_SESSIONS_METADATA_TABLE_NAME)")
    run(args.table, args.region, args.apply, args.sleep, args.limit)
    if not args.apply:
        logger.info("DRY-RUN only — no changes written. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
