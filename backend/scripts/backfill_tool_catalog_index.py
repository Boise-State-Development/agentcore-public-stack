"""Backfill: stamp GSI5PK/GSI5SK on tool-catalog rows written before they existed.

``ToolDefinition.to_dynamo_item`` began stamping EntityTypeIndex keys in
``perf(tools): index the tool catalog so listing it stops scanning`` — every
tool row written before that carries neither attribute:

    tool row : PK=TOOL#{tool_id}  SK=METADATA
             + GSI5PK=ENTITY#TOOL
             + GSI5SK=TOOL#{tool_id}

``EntityTypeIndex`` is **sparse**: DynamoDB indexes a row only if it carries
the index's key attributes. A row missing them is not "stale" in the index, it
is *absent from it forever* — and the omission is silent, no error anywhere.
Switching ``list_tools`` to that index without this backfill would serve an
empty (or partial) tool catalog to every user on the deployment, which reads
as "all my tools disappeared", not as an outage anyone gets paged for.

WHY THIS EXISTS AT ALL
----------------------
The app-roles table is shared. Tools, skills, roles, role grants, JWT mappings
and one tool-preferences row PER USER live in it, so listing tools by Scan
reads the whole table and filters. Scan cost tracks table size, not result
size — measured on dev, 95 items read to return 24 tools, 17 of them per-user
rows. That read's cost therefore grows with enrollment rather than with the
number of tools. The index turns it into a Query on one partition.

RUN THIS BEFORE THE INDEX IS CREATED, IF YOU CAN
------------------------------------------------
DynamoDB backfills a new GSI at creation time from rows that already carry its
keys, so stamping first means the index is complete the moment it reports
ACTIVE, with no partially-populated window. The attributes are inert until an
index consumes them, so running early costs nothing.

Running *after* creation is also fine and is the expected order here, because
the CDK change and this script ship in the same PR: the index simply picks up
each row as this script stamps it. Either way the rule that matters is the
same — **do not switch the read to the index until this reports
``skipped=0 failed=0`` and the index item count matches the tool count.**

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write.
* **Idempotent.** Guarded by ``attribute_not_exists(GSI5PK)``, so a second run
  finds nothing and a row the writer has since stamped is left alone.
* **Never resurrects a deleted row.** ``attribute_exists(SK)`` on every update.
* **Touches only tool metadata rows.** ``TOOL#``/``CAPABILITIES`` snapshots,
  skills, roles and user preferences are left alone — they are not tools, and
  the tool partition of the index must contain exactly the tool catalog.
* **Invents nothing.** Both key values derive from the row's own PK, so a
  stamped row is byte-identical to what the writer would have written.

Run against dev first, then prod::

    AWS_PROFILE=dev-ai python backend/scripts/backfill_tool_catalog_index.py \\
        --table dev-boisestateai-v2-app-roles --region us-west-2
    AWS_PROFILE=dev-ai python backend/scripts/backfill_tool_catalog_index.py \\
        --table dev-boisestateai-v2-app-roles --region us-west-2 --apply
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, Dict, Iterator, List

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("backfill_tool_catalog_index")

# Kept as a literal rather than imported from apis.shared.tools.models so the
# script runs standalone against a deployed table without importing the app.
# The value is asserted against the model in
# tests/test_backfill_tool_catalog_index.py, so the two cannot drift.
ENTITY_TYPE_TOOL = "ENTITY#TOOL"


def iter_tool_rows(table: Any) -> Iterator[Dict[str, Any]]:
    """Every tool METADATA row in the table.

    A Scan, not a Query — this migration exists precisely because there is no
    index to query yet. ``FilterExpression`` runs server-side to cut payload;
    DynamoDB still reads every item either way, so it saves bandwidth, not
    capacity.
    """
    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(PK, :p) AND SK = :sk",
        "ExpressionAttributeValues": {":p": "TOOL#", ":sk": "METADATA"},
    }
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        last = resp.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def plan_row(item: Dict[str, Any]) -> Dict[str, Any] | None:
    """What this row needs, or None if it needs nothing.

    Returns the computed keys, or ``{"skip": reason}`` for a row that cannot
    be stamped safely.
    """
    if "GSI5PK" in item:
        return None  # already stamped — writer or a previous run

    pk = str(item.get("PK", ""))
    if not pk.startswith("TOOL#"):
        return {"skip": f"unexpected PK {pk!r}"}
    if pk == "TOOL#":
        return {"skip": "empty tool id in PK"}

    # GSI5SK is the row's own PK. Deriving both keys from the PK rather than
    # from the `toolId` attribute means a row with a missing or disagreeing
    # `toolId` still lands in the index under the identity the base table
    # already uses — there is no way for this to invent a different one.
    return {"gsi5pk": ENTITY_TYPE_TOOL, "gsi5sk": pk}


def backfill(table: Any, apply: bool) -> Dict[str, int]:
    stats = {"tool_rows": 0, "already": 0, "stamped": 0, "skipped": 0, "failed": 0}
    skipped: List[str] = []

    for item in iter_tool_rows(table):
        stats["tool_rows"] += 1
        plan = plan_row(item)

        if plan is None:
            stats["already"] += 1
            continue

        pk = str(item.get("PK", ""))
        if "skip" in plan:
            stats["skipped"] += 1
            skipped.append(f"{pk}: {plan['skip']}")
            continue

        logger.info("stamp %s -> GSI5PK=%s GSI5SK=%s", pk, plan["gsi5pk"], plan["gsi5sk"])
        if not apply:
            stats["stamped"] += 1
            continue

        try:
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET GSI5PK = :pk, GSI5SK = :sk",
                ExpressionAttributeValues={
                    ":pk": plan["gsi5pk"],
                    ":sk": plan["gsi5sk"],
                },
                # attribute_exists(SK): never resurrect a row the delete path
                # removed between the scan and this write.
                # attribute_not_exists(GSI5PK): idempotent, and yields to the
                # writer if it stamped the row in the meantime.
                ConditionExpression=(
                    "attribute_exists(SK) AND attribute_not_exists(GSI5PK)"
                ),
            )
            stats["stamped"] += 1
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Deleted, or stamped by the writer, while we scanned.
                stats["already"] += 1
                continue
            stats["failed"] += 1
            logger.error("failed to stamp %s: %s", pk, code, exc_info=True)

    if skipped:
        logger.warning("%s row(s) could not be stamped:", len(skipped))
        for line in skipped:
            logger.warning("  %s", line)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="app-roles table name")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write (default is a dry run)",
    )
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    if not args.apply:
        logger.info("DRY RUN — no writes. Pass --apply to commit.")

    stats = backfill(table, args.apply)

    logger.info(
        "tool rows=%s already-stamped=%s stamped=%s skipped=%s failed=%s",
        stats["tool_rows"],
        stats["already"],
        stats["stamped"],
        stats["skipped"],
        stats["failed"],
    )
    if stats["skipped"] or stats["failed"]:
        logger.warning(
            "Index will be INCOMPLETE for the rows above. Resolve them before "
            "switching list_tools to EntityTypeIndex — a sparse index drops "
            "them silently, and the catalog just looks short."
        )


if __name__ == "__main__":
    main()
