"""Backfill: stamp GSI2PK/GSI2SK on artifact HEAD rows written before they existed.

The artifact writer began stamping user-index keys on HEAD rows in
``feat(artifacts): stamp user-index keys on HEAD rows ahead of the index``
(2026-09-04). Every HEAD row written before that carries neither
attribute:

    HEAD row : PK=USER#{user_id}  SK=ARTIFACT#{aid}#HEAD
             + GSI2PK=USER#{user_id}
             + GSI2SK=ARTIFACT#{updated_at}#{aid}

``UserArtifactsIndex`` is **sparse**: DynamoDB indexes a row only if it
carries the index's key attributes. A row missing them is not "stale" in
the index, it is *absent from it forever* — and the omission is silent.
Switching the library's user-wide listing to that index without this
backfill would drop every artifact created before 2026-09-04 from the
page, with no error anywhere.

RUN THIS BEFORE THE INDEX IS CREATED
------------------------------------
DynamoDB backfills a new GSI at creation time from rows that already
carry its keys. Stamping first therefore means the index is complete the
moment it reports ACTIVE, with no window in which it is partially
populated. The attributes are inert until an index consumes them (no
index write is charged), so running early costs nothing.

WHAT IT DOES NOT TOUCH
----------------------
``updated_at``. It is not a display field: HEAD's ``GSI1SK``/``GSI2SK``
embed it, and only the writer maintains it. This script *reads* it to
build ``GSI2SK`` — matching byte-for-byte what the writer would have
written — and never assigns it. Same restraint as
``ArtifactLifecycleService.rename``.

Version rows are skipped by design. The keys belong on HEAD rows only,
so the index holds one row per artifact rather than one per version.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write.
* **Idempotent.** Guarded by ``attribute_not_exists(GSI2PK)``, so a
  second run finds nothing and a row the writer has since stamped is
  left alone.
* **Never resurrects a deleted row.** ``attribute_exists(SK)`` on every
  update, matching the writer's own write-back rule.
* **Encodes a missing ``updated_at`` rather than inventing one.** Such a
  row is stamped with an empty timestamp segment
  (``ARTIFACT##{aid}``), which sorts below every real timestamp and so
  reads last — where the previous in-memory sort put it. Leaving it
  unstamped would drop the artifact from a sparse index, and from its
  owner's library, silently.

Run against dev first, then prod::

    AWS_PROFILE=dev-ai python backend/scripts/backfill_artifact_user_index_keys.py \\
        --table dev-boisestateai-v2-user-artifacts --region us-west-2
    AWS_PROFILE=dev-ai python backend/scripts/backfill_artifact_user_index_keys.py \\
        --table dev-boisestateai-v2-user-artifacts --region us-west-2 --apply
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
logger = logging.getLogger("backfill_artifact_user_index_keys")

HEAD_SUFFIX = "#HEAD"


def iter_head_rows(table: Any) -> Iterator[Dict[str, Any]]:
    """Every artifact HEAD row in the table.

    A Scan, not a Query: this is a whole-table migration across all
    users, and the table's partition key is the user. The
    ``FilterExpression`` runs server-side purely to cut payload —
    DynamoDB still reads every item either way, so it saves bandwidth,
    not capacity.
    """
    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(SK, :p)",
        "ExpressionAttributeValues": {":p": "ARTIFACT#"},
    }
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            if str(item.get("SK", "")).endswith(HEAD_SUFFIX):
                yield item
        last = resp.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def plan_row(item: Dict[str, Any]) -> Dict[str, Any] | None:
    """What this row needs, or None if it needs nothing.

    Returns a dict with the computed keys, or ``{"skip": reason}`` for a
    row that cannot be stamped safely.
    """
    if "GSI2PK" in item:
        return None  # already stamped — writer or a previous run

    pk = str(item.get("PK", ""))
    artifact_id = str(item.get("artifact_id", ""))
    updated_at = str(item.get("updated_at", ""))

    if not pk.startswith("USER#"):
        return {"skip": f"unexpected PK {pk!r}"}
    if not artifact_id:
        return {"skip": "no artifact_id attribute"}
    # A row with no `updated_at` is stamped with an EMPTY timestamp
    # segment, not a fabricated one. Two things make that the right
    # answer rather than a fudge:
    #
    #   * Leaving it unstamped would drop the artifact out of a sparse
    #     index — and so out of its owner's library — permanently and
    #     silently. Dropping somebody's oldest artifacts is worse than
    #     showing them undated.
    #   * "ARTIFACT##{aid}" sorts BELOW every real timestamp ("#" < any
    #     digit), so read descending it lands last — exactly where the
    #     old in-memory sort put undated rows. It encodes "no timestamp"
    #     honestly instead of inventing one that would sort wrongly
    #     forever.
    #
    # Neither dev nor prod had such a row when this was written; this is
    # the defensive branch, and it preserves a contract the library's
    # tests already assert.
    return {
        "gsi2pk": pk,  # GSI2PK is exactly the base PK — USER#{user_id}
        "gsi2sk": f"ARTIFACT#{updated_at}#{artifact_id}",
        "undated": not updated_at,
    }


def backfill(table: Any, apply: bool) -> Dict[str, int]:
    stats = {"head_rows": 0, "already": 0, "stamped": 0, "skipped": 0, "failed": 0}
    skipped: List[str] = []

    for item in iter_head_rows(table):
        stats["head_rows"] += 1
        plan = plan_row(item)

        if plan is None:
            stats["already"] += 1
            continue

        sk = str(item.get("SK", ""))
        if "skip" in plan:
            stats["skipped"] += 1
            skipped.append(f"{item.get('PK')} / {sk}: {plan['skip']}")
            continue

        logger.info(
            "stamp %s %s -> GSI2SK=%s%s",
            item.get("PK"),
            sk,
            plan["gsi2sk"],
            "  (no updated_at — sorts last)" if plan.get("undated") else "",
        )
        if not apply:
            stats["stamped"] += 1
            continue

        try:
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET GSI2PK = :pk, GSI2SK = :sk",
                ExpressionAttributeValues={
                    ":pk": plan["gsi2pk"],
                    ":sk": plan["gsi2sk"],
                },
                # attribute_exists(SK): never resurrect a row the delete
                # path removed between the scan and this write.
                # attribute_not_exists(GSI2PK): idempotent, and yields to
                # the writer if it stamped the row in the meantime.
                ConditionExpression=(
                    "attribute_exists(SK) AND attribute_not_exists(GSI2PK)"
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
            logger.error("failed to stamp %s: %s", sk, code, exc_info=True)

    if skipped:
        logger.warning("%s row(s) could not be stamped:", len(skipped))
        for line in skipped:
            logger.warning("  %s", line)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="user-artifacts table name")
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
        "HEAD rows=%s already-stamped=%s stamped=%s skipped=%s failed=%s",
        stats["head_rows"],
        stats["already"],
        stats["stamped"],
        stats["skipped"],
        stats["failed"],
    )
    if stats["skipped"] or stats["failed"]:
        logger.warning(
            "Index will be INCOMPLETE for the rows above. Resolve them "
            "before switching the library query to UserArtifactsIndex."
        )


if __name__ == "__main__":
    main()
