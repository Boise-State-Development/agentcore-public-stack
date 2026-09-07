#!/usr/bin/env python3
"""Clear interrupted-turn markers that no interruption could have produced.

WHY THIS EXISTS
Until PR #988 the SPA released a session's ``AbortController`` only on the
Stop button, never on normal stream teardown. ``streamingSessionIds()`` reads
that controller to decide which turns a page departure interrupted, so every
completed turn stayed "in flight" for the life of the tab and the next
refresh / tab close / navigation POSTed ``navigated_away`` for it — one
departure marking every session ever streamed in that tab.

A stale marker costs twice. It shows a "Response interrupted" chip plus a
Continue button on a complete answer (and Continue bills a
``continue_truncated`` turn that resumes an already-finished message), and it
makes the session's NEXT prompt carry a false ``<interruption_note>`` —
persisted in history, invisible in the UI, telling the model its previous
response was cut off. Markers self-clear only at the start of that session's
next turn (``clear_interrupted_turn``), so a conversation nobody returns to
stays armed indefinitely.

WHY THE 900s THRESHOLD, AND NOT "THE MARKER LANDED AFTER THE LAST MESSAGE"
A genuine interruption does not always bump ``lastMessageAt``: the
"marker only, no synthetic write" branch of ``_persist_interruption`` (an
interrupted continuation, where the history tail is already an assistant
turn) leaves it at the previous turn. So a modest positive gap is ambiguous
and clearing on it would erase real interruptions.

What is NOT ambiguous is a gap wider than a turn can live. The SSE stream
times out at 600s, so no turn is still running 15 minutes after its last
message — a departure signal that lands then cannot have interrupted
anything. That is the only claim this script acts on.

SAFETY
* Dry-run by default; ``--apply`` is required to write.
* Only ``navigated_away`` rows are considered. ``user_stopped`` is the user's
  own attested intent and ``connection_lost`` comes from the server's own
  backstop — neither is this bug, and neither is touched.
* Each write is conditional on the exact ``lastTurnInterruptedAt`` /
  ``lastTurnInterruptReason`` the scan read, so a session that started a new
  turn (and was legitimately re-marked) between scan and write is skipped
  rather than clobbered.
* Idempotent: a second run finds nothing left to do.

USAGE
    python scripts/backfill_false_interrupted_markers.py \
        --table boisestateai-v2-sessions-metadata --profile prod-ai
    # …review the dry-run summary, then:
    python scripts/backfill_false_interrupted_markers.py \
        --table boisestateai-v2-sessions-metadata --profile prod-ai --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import boto3
from botocore.exceptions import ClientError

# The SSE stream times out at 600s. A departure signal that lands more than
# this long after the session's last message cannot have interrupted a live
# turn, whatever the history tail looks like. The extra margin over 600s is
# deliberate slack for clock skew and teardown latency.
DEFAULT_MIN_GAP_SECONDS = 900

MARKER_ATTRS = ("lastTurnInterrupted", "lastTurnInterruptReason", "lastTurnInterruptedAt")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored ISO-8601 timestamp, tolerating a missing offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _scan_marked_rows(table: Any) -> Iterator[dict]:
    """Yield every session row carrying an interrupted-turn marker."""
    kwargs: dict[str, Any] = {
        "FilterExpression": "attribute_exists(#lti)",
        "ProjectionExpression": "#pk, #sk, #gsipk, #lti, #ltr, #ltia, #lma",
        "ExpressionAttributeNames": {
            "#pk": "PK",
            "#sk": "SK",
            "#gsipk": "GSI_PK",
            "#lti": "lastTurnInterrupted",
            "#ltr": "lastTurnInterruptReason",
            "#ltia": "lastTurnInterruptedAt",
            "#lma": "lastMessageAt",
        },
    }
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key


def _select(items: list[dict], min_gap_seconds: int) -> tuple[list[dict], dict[str, int]]:
    """Split scanned rows into the provably-false ones and a reason tally."""
    selected: list[dict] = []
    skipped = {"other_reason": 0, "gap_too_small": 0, "unparseable": 0}

    for item in items:
        if item.get("lastTurnInterruptReason") != "navigated_away":
            skipped["other_reason"] += 1
            continue
        marked_at = _parse_iso(item.get("lastTurnInterruptedAt"))
        last_message_at = _parse_iso(item.get("lastMessageAt"))
        if marked_at is None or last_message_at is None:
            skipped["unparseable"] += 1
            continue
        gap = (marked_at - last_message_at).total_seconds()
        if gap <= min_gap_seconds:
            skipped["gap_too_small"] += 1
            continue
        selected.append({**item, "_gapSeconds": gap})

    selected.sort(key=lambda row: row["_gapSeconds"], reverse=True)
    return selected, skipped


def _clear(table: Any, row: dict) -> str:
    """Remove one row's marker. Returns 'cleared', 'raced', or 'error'."""
    try:
        table.update_item(
            Key={"PK": row["PK"], "SK": row["SK"]},
            UpdateExpression="REMOVE #lti, #ltr, #ltia",
            ExpressionAttributeNames={
                "#lti": "lastTurnInterrupted",
                "#ltr": "lastTurnInterruptReason",
                "#ltia": "lastTurnInterruptedAt",
            },
            # The row must still be exactly what the scan saw. A session that
            # ran a new turn in between has either cleared the marker itself
            # or been re-marked by a real interruption; both must be left
            # alone.
            ConditionExpression=(
                "attribute_exists(#lti) AND #ltia = :ts AND #ltr = :reason"
            ),
            ExpressionAttributeValues={
                ":ts": row["lastTurnInterruptedAt"],
                ":reason": "navigated_away",
            },
        )
        return "cleared"
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "raced"
        print(f"  ERROR on {row.get('GSI_PK', row['SK'])}: {e}", file=sys.stderr)
        return "error"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True, help="Sessions-metadata DynamoDB table name")
    parser.add_argument("--profile", default=None, help="AWS profile (default: ambient credentials)")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--min-gap-seconds",
        type=int,
        default=DEFAULT_MIN_GAP_SECONDS,
        help=(
            "Only clear markers written more than this long after the session's "
            f"last message (default {DEFAULT_MIN_GAP_SECONDS}; must exceed the "
            "600s stream timeout to stay provably false)"
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Write. Without it, dry-run only.")
    args = parser.parse_args()

    if args.min_gap_seconds <= 600:
        parser.error(
            "--min-gap-seconds must exceed the 600s stream timeout; below that a "
            "marker can belong to a turn that was genuinely still running."
        )

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)

    print(f"Scanning {args.table} ({args.region}) for interrupted-turn markers…")
    items = list(_scan_marked_rows(table))
    selected, skipped = _select(items, args.min_gap_seconds)

    print(f"\n  rows carrying a marker:            {len(items)}")
    print(f"  left alone — not navigated_away:   {skipped['other_reason']}")
    print(f"  left alone — gap <= {args.min_gap_seconds}s:{'':<9}{skipped['gap_too_small']}")
    if skipped["unparseable"]:
        print(f"  left alone — unparseable dates:    {skipped['unparseable']}")
    print(f"  provably false, to clear:          {len(selected)}")

    if selected:
        print("\n  widest gaps:")
        for row in selected[:5]:
            days = row["_gapSeconds"] / 86400
            print(f"    {row.get('GSI_PK', row['SK'])}  +{days:.1f}d after last message")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to clear.")
        return 0

    print(f"\nClearing {len(selected)} markers…")
    tally = {"cleared": 0, "raced": 0, "error": 0}
    for row in selected:
        tally[_clear(table, row)] += 1

    print(f"\n  cleared: {tally['cleared']}")
    print(f"  skipped (row changed under us): {tally['raced']}")
    print(f"  errors:  {tally['error']}")
    return 1 if tally["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
