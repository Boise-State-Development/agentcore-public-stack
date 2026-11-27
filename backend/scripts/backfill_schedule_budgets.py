"""Backfill: give every existing short-interval schedule a daily budget, once.

From PR 4.1 (docs/specs/proactive-trust.md §4.4) a personal schedule whose
interval is under 60 minutes must have ``maxCostPerDay``: create and edit fill
in the deployment's default when the request names none. A schedule created
before that has none, so its spend is uncapped until it is next edited. This
sets the default on each of them, so none is left uncapped.

WHICH SCHEDULES
---------------
Personal ``SCHEDPROMPT#`` rows (no ``projectId``) with ``cadence: interval`` and
an interval under 60 minutes, in any state (a paused one gets its budget before
its owner resumes it), that have no ``maxCostPerDay``. Watch and standard alike.
A project schedule is left out: budgets are personal (§4.2).

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write.
* **Never overwrites a budget.** ``attribute_not_exists(maxCostPerDay)`` on every
  update, so a budget its owner set in the meantime stays.
* **Never resurrects a deleted row.** ``attribute_exists(PK)`` on every update.
* **Touches nothing else**: not ``nextRunAt``, ``state``, ``updatedAt`` or the
  spend counters. The worker starts counting at the next run.
* Scans the sessions-metadata table with a filter on ``SK``. Paginated; run it
  once per environment, when ``PROACTIVE_RUNS_ENABLED`` is first turned on there
  (dev at PR 4.1's deploy; prod when the flag is promoted).

Run from the repo root, against dev first::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_schedule_budgets.py \\
        --table dev-boisestateai-v2-sessions-metadata --region us-west-2
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_schedule_budgets.py \\
        --table dev-boisestateai-v2-sessions-metadata --region us-west-2 --apply

``--budget`` sets the amount (default: ``SCHEDULE_DEFAULT_MAX_COST_PER_DAY``, else
$0.50), so it should match the deployment's app-api setting. Prints schedule ids,
cadences and models only, never a prompt or a label.
"""

from __future__ import annotations

import argparse
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterator, List, Optional

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_schedule_budgets")

#: An interval under this many minutes must have a budget (``budget.SHORT_INTERVAL_MINUTES``).
SHORT_INTERVAL_MINUTES = 60
#: The shipped default (§12 Q1), when neither ``--budget`` nor the env var is given.
DEFAULT_BUDGET = Decimal("0.50")


def interval_minutes(row: Dict[str, Any]) -> Optional[int]:
    value, unit = row.get("intervalValue"), row.get("intervalUnit")
    if value is None or unit not in ("minutes", "hours"):
        return None
    minutes = int(value)
    return minutes * 60 if unit == "hours" else minutes


def needs_budget(row: Dict[str, Any]) -> bool:
    """A personal interval schedule under 60 minutes with no ``maxCostPerDay``."""
    if not str(row.get("SK", "")).startswith("SCHEDPROMPT#") or row.get("projectId"):
        return False
    if row.get("cadence") != "interval" or row.get("maxCostPerDay") is not None:
        return False
    minutes = interval_minutes(row)
    return minutes is not None and 0 < minutes < SHORT_INTERVAL_MINUTES


def iter_schedule_rows(table: Any) -> Iterator[Dict[str, Any]]:
    """Every ``SCHEDPROMPT#`` row, by a filtered Scan (run once per environment)."""
    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(SK, :p)",
        "ExpressionAttributeValues": {":p": "SCHEDPROMPT#"},
        "ProjectionExpression": "PK, SK, scheduleId, cadence, intervalValue, intervalUnit, projectId, "
        "maxCostPerDay, #state, #mode, modelId",
        "ExpressionAttributeNames": {"#state": "state", "#mode": "mode"},
    }
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def set_budget(table: Any, row: Dict[str, Any], budget: Decimal) -> bool:
    """Set the budget on one row unless it has one or is gone. Returns whether it wrote."""
    try:
        table.update_item(
            Key={"PK": row["PK"], "SK": row["SK"]},
            UpdateExpression="SET maxCostPerDay = :b",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(maxCostPerDay)",
            ExpressionAttributeValues={":b": budget},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def resolve_budget(raw: Optional[str]) -> Decimal:
    text = (raw or os.environ.get("SCHEDULE_DEFAULT_MAX_COST_PER_DAY") or "").strip()
    if not text:
        return DEFAULT_BUDGET
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise SystemExit(f"--budget must be a number of dollars, got {text!r}") from e
    if not value.is_finite() or value <= 0:
        raise SystemExit(f"--budget must be positive, got {text!r}")
    return value


def run(table: Any, budget: Decimal, apply: bool) -> Dict[str, int]:
    counts = {"scanned": 0, "needs_budget": 0, "written": 0, "skipped": 0}
    pending: List[Dict[str, Any]] = []
    for row in iter_schedule_rows(table):
        counts["scanned"] += 1
        if needs_budget(row):
            pending.append(row)
    counts["needs_budget"] = len(pending)
    for row in pending:
        logger.info(
            "%s %s every %s min, %s, %s, model %s",
            "SET" if apply else "WOULD SET",
            row.get("scheduleId"),
            interval_minutes(row),
            row.get("mode") or "standard",
            row.get("state"),
            row.get("modelId") or "default",
        )
        if not apply:
            continue
        if set_budget(table, row, budget):
            counts["written"] += 1
        else:
            counts["skipped"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True, help="The sessions-metadata table")
    parser.add_argument("--region", default=None)
    parser.add_argument("--budget", default=None, help="USD a day (default: the env var, else 0.50)")
    parser.add_argument("--apply", action="store_true", help="Write; without it, a dry run")
    args = parser.parse_args()

    budget = resolve_budget(args.budget)
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    counts = run(table, budget, args.apply)
    logger.info("Budget $%s: %s%s", budget, counts, "" if args.apply else " (dry run; pass --apply to write)")


if __name__ == "__main__":
    main()
