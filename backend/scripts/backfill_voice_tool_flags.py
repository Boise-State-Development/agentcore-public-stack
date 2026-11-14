"""Backfill: mark the tools that may run in voice mode (#625).

Voice mode loads only tools whose catalog row carries ``voiceAvailable: true``
(``apis.shared.tools.freshness.keep_voice_tools``). Every row written before
the flag existed reads as ``false``, so after the deploy a voice session runs
with no tools until this sets the flag on the tools that suit voice.

WHICH TOOLS
-----------
Nova 2 Sonic goes quiet for the rest of a voice session when a tool takes
longer than about a second, and a tool whose result is something to look at
(a chart, a document, a browser) does nothing for a listener. The list below
is the fast lookups in the catalog as of 2026-11, by tool id:

* ``calculator`` — local, instant.
* ``campus_directory`` — a directory lookup.
* ``class_search`` — a course lookup.
* ``gateway_search_boise_state`` — site search through the Gateway.
* ``gateway_policy_search`` — policy search through the Gateway.

Left out on purpose: web search and URL fetch (often over a second), the
browser, Code Interpreter, charts, artifacts, Office documents, spreadsheets,
skill authoring, the meeting/Zoom/GitHub/Google tools (OAuth prompts and
multi-step writes), and ``ask_user_question`` (an on-screen picker). Admins can
change any tool later in Admin > Tools; this script only sets the first list.

An id the table does not have is reported and skipped, so the same list runs
against every environment.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write.
* **Only ever sets the flag to true**, and only on the listed ids. A tool an
  admin has already turned on stays on; nothing is turned off.
* **Never resurrects a deleted row.** ``attribute_exists(SK)`` on every update.
* **Leaves ``updatedAt`` alone.** The flag does not change what a text turn
  builds, so bumping the freshness token would only evict cached agents.
* ``--list`` prints every tool's current flag and writes nothing.

Run from the repo root, against dev first, then prod::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_voice_tool_flags.py \\
        --table dev-boisestateai-v2-app-roles --region us-west-2
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_voice_tool_flags.py \\
        --table dev-boisestateai-v2-app-roles --region us-west-2 --apply
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, Dict, Iterable, Iterator, List

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_voice_tool_flags")

#: The tools marked as available in voice mode by this backfill (see the module docstring).
VOICE_TOOL_IDS: tuple = (
    "calculator",
    "campus_directory",
    "class_search",
    "gateway_search_boise_state",
    "gateway_policy_search",
)


def iter_tool_rows(table: Any) -> Iterator[Dict[str, Any]]:
    """Every tool METADATA row in the table (a Scan: the list is small and run rarely)."""
    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(PK, :p) AND SK = :sk",
        "ExpressionAttributeValues": {":p": "TOOL#", ":sk": "METADATA"},
    }
    while True:
        resp = table.scan(**kwargs)
        yield from resp.get("Items", [])
        last = resp.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def backfill(table: Any, tool_ids: Iterable[str], apply: bool) -> Dict[str, int]:
    wanted = list(dict.fromkeys(tool_ids))
    rows = {str(item.get("toolId") or str(item.get("PK", ""))[len("TOOL#"):]): item for item in iter_tool_rows(table)}
    stats = {"listed": len(wanted), "already": 0, "set": 0, "missing": 0, "failed": 0}

    for tool_id in wanted:
        item = rows.get(tool_id)
        if item is None:
            stats["missing"] += 1
            logger.warning("skip %s: not in this table", tool_id)
            continue
        if item.get("voiceAvailable") is True:
            stats["already"] += 1
            continue

        logger.info("set voiceAvailable=true on %s", tool_id)
        if not apply:
            stats["set"] += 1
            continue
        try:
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET voiceAvailable = :t",
                ExpressionAttributeValues={":t": True},
                ConditionExpression="attribute_exists(SK)",
            )
            stats["set"] += 1
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                stats["missing"] += 1
                continue
            stats["failed"] += 1
            logger.error("failed to set %s: %s", tool_id, code, exc_info=True)
    return stats


def list_flags(table: Any) -> List[str]:
    lines = []
    for item in sorted(iter_tool_rows(table), key=lambda i: str(i.get("toolId", ""))):
        flag = "voice" if item.get("voiceAvailable") is True else "-"
        lines.append(f"{flag:6} {item.get('toolId')}  ({item.get('displayName', '')}, {item.get('status', 'active')})")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True, help="app-roles table name")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    parser.add_argument("--list", action="store_true", help="print every tool's current flag and exit")
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    if args.list:
        for line in list_flags(table):
            print(line)
        return

    if not args.apply:
        logger.info("DRY RUN — pass --apply to write")
    stats = backfill(table, VOICE_TOOL_IDS, apply=args.apply)
    logger.info("done: %s", stats)


if __name__ == "__main__":
    main()
