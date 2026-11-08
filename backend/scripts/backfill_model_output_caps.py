"""Backfill: carry a raised curated ``max_tokens`` default onto catalog rows already saved.

The curated catalog (``frontend/ai.client/src/app/admin/manage-models/models/
curated-models.ts``) only seeds a model row when an admin adds it. After that,
the row in the managed-models table is the only thing the runtime reads, so a
release that raises a curated default changes nothing for a deployment that
already has the model. ``_merge_inference_params`` sends that row's
``supportedParams.params.max_tokens.default`` on every call that names no
``max_tokens``, and agent authors cannot override it (``AUTHOR_HIDDEN_PARAMS``
in the Agent Designer), so the stored default is the output cap for every chat
and every agent on that model.

This script moves rows that still hold the **old curated default** to the new
one. Today that is one change:

    Claude Opus 5.5 (``*anthropic.claude-opus-5-5``)  32,000 -> 64,000

WHICH ROWS IT TOUCHES
---------------------
Only a row that still looks exactly like the curated seed on this one value:

* its ``modelId`` is the model (any inference-profile prefix: ``us.``,
  ``global.``, or none), and
* its ``max_tokens`` spec is present, supported, and its ``default`` equals
  the old curated default.

Everything else is left alone and reported:

* a row whose default is anything else was **edited by an admin** — their
  value stands, whether it is higher or lower;
* a row whose ``max_tokens`` ``max`` (or the model's ``maxOutputTokens``) is
  below the new default was narrowed on purpose, and writing the new default
  would also break the row's own validation (``max_tokens default must be <=
  maxOutputTokens``).

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write.
* **Per-row before/after** is printed for every row it would change or did.
* **Conditional write.** The update requires the default to still equal the
  old value, so an admin edit that lands between the scan and the write wins,
  and a second run finds nothing to do.
* **Never resurrects a deleted row.** ``attribute_exists(PK)`` on every update.
* **Touches one attribute** (plus ``updatedAt``): the nested
  ``supportedParams.params.max_tokens.default``. Pricing, access control and
  every other param are not read back or rewritten.
* **Takes effect within a minute.** The Runtime and app-api cache the catalog
  for ``CONFIG_CACHE_TTL_SECONDS`` (60 s by default); no deploy or restart.

Run from the repo root, against dev first, then prod. Nothing here imports
``apis.*``; the backend venv's interpreter (for ``boto3``) is the only
requirement::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_model_output_caps.py \\
        --table dev-boisestateai-v2-managed-models --region us-west-2
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_model_output_caps.py \\
        --table dev-boisestateai-v2-managed-models --region us-west-2 --apply

The table is ``<prefix>-managed-models`` (``DYNAMODB_MANAGED_MODELS_TABLE_NAME``).
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_model_output_caps")


@dataclass(frozen=True)
class CapChange:
    """One curated ``max_tokens`` default that a release raised."""

    curated_key: str  # the row's `key` in curated-models.ts, for the drift test
    label: str
    model_id: "re.Pattern[str]"
    old_default: int
    new_default: int


# Kept as literals rather than read from curated-models.ts so the script runs
# standalone against a deployed table. `tests/test_backfill_model_output_caps.py`
# asserts each `new_default` against the curated row, so the two cannot drift.
CHANGES: List[CapChange] = [
    CapChange(
        curated_key="claude-opus-5-5",
        label="Claude Opus 5.5",
        # `us.anthropic.claude-opus-5-5`, `global.anthropic.claude-opus-5-5`,
        # or the bare `anthropic.claude-opus-5-5`. Anchored, so a future
        # `claude-opus-5-5-v2` or `-1m` variant is never matched by accident.
        model_id=re.compile(r"^(?:[a-z]+\.)?anthropic\.claude-opus-5-5$"),
        old_default=32_000,
        new_default=64_000,
    ),
]


def _as_int(value: Any) -> Optional[int]:
    """A whole number from a DynamoDB value (``Decimal``), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)) or (isinstance(value, float) and value.is_integer()):
        number = Decimal(value)
        if number == number.to_integral_value():
            return int(number)
    return None


def iter_model_rows(table: Any) -> Iterator[Dict[str, Any]]:
    """Every managed-model row (``PK = SK = MODEL#<uuid>``)."""
    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(PK, :p)",
        "ExpressionAttributeValues": {":p": "MODEL#"},
    }
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            if item.get("SK") == item.get("PK"):
                yield item
        last = resp.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def change_for(model_id: str) -> Optional[CapChange]:
    for change in CHANGES:
        if change.model_id.match(model_id):
            return change
    return None


def plan_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """What to do with one row.

    ``{"action": "update", "change": CapChange}``, or ``{"action": "skip",
    "reason": str}`` for a target row left alone, or ``{"action": "other"}``
    for a model this release did not change.
    """
    model_id = str(item.get("modelId") or "")
    change = change_for(model_id)
    if change is None:
        return {"action": "other"}

    spec = ((item.get("supportedParams") or {}).get("params") or {}).get("max_tokens")
    if not isinstance(spec, dict):
        return {"action": "skip", "reason": "no max_tokens spec (admin-edited)"}
    if spec.get("supported") is False:
        return {"action": "skip", "reason": "max_tokens marked unsupported (admin-edited)"}

    current = _as_int(spec.get("default"))
    if current == change.new_default:
        return {"action": "skip", "reason": f"already {change.new_default:,}"}
    if current != change.old_default:
        shown = "unset" if spec.get("default") is None else str(spec.get("default"))
        return {"action": "skip", "reason": f"default is {shown}, not the old curated {change.old_default:,} (admin-edited)"}

    spec_max = _as_int(spec.get("max"))
    if spec_max is not None and spec_max < change.new_default:
        return {"action": "skip", "reason": f"max_tokens max is {spec_max:,}, below {change.new_default:,}"}
    ceiling = _as_int(item.get("maxOutputTokens"))
    if ceiling is not None and ceiling < change.new_default:
        return {"action": "skip", "reason": f"maxOutputTokens is {ceiling:,}, below {change.new_default:,}"}

    return {"action": "update", "change": change}


def _describe(item: Dict[str, Any]) -> str:
    return f"{item.get('PK')} {item.get('modelId')} ({item.get('modelName') or 'unnamed'})"


def backfill(table: Any, apply: bool, now: Optional[str] = None) -> Dict[str, int]:
    stats = {"model_rows": 0, "other_models": 0, "updated": 0, "skipped": 0, "raced": 0, "failed": 0}
    stamp = now or datetime.now(timezone.utc).isoformat()
    verb = "updated" if apply else "would update"

    for item in iter_model_rows(table):
        stats["model_rows"] += 1
        plan = plan_row(item)

        if plan["action"] == "other":
            stats["other_models"] += 1
            continue
        if plan["action"] == "skip":
            stats["skipped"] += 1
            logger.info("left alone  %s: %s", _describe(item), plan["reason"])
            continue

        change: CapChange = plan["change"]
        if not apply:
            stats["updated"] += 1
            logger.info(
                "%s %s: max_tokens.default %s -> %s", verb, _describe(item),
                f"{change.old_default:,}", f"{change.new_default:,}",
            )
            continue

        try:
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET supportedParams.params.max_tokens.#d = :new, #u = :now",
                ConditionExpression="attribute_exists(PK) AND supportedParams.params.max_tokens.#d = :old",
                ExpressionAttributeNames={"#d": "default", "#u": "updatedAt"},
                ExpressionAttributeValues={
                    ":new": change.new_default,
                    ":old": change.old_default,
                    ":now": stamp,
                },
            )
            stats["updated"] += 1
            logger.info(
                "%s %s: max_tokens.default %s -> %s", verb, _describe(item),
                f"{change.old_default:,}", f"{change.new_default:,}",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Deleted, or edited by an admin, between the scan and this write.
                stats["raced"] += 1
                logger.info("left alone  %s: changed since the scan", _describe(item))
                continue
            stats["failed"] += 1
            logger.error("failed to update %s: %s", _describe(item), code, exc_info=True)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True, help="managed-models table name (<prefix>-managed-models)")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    parser.add_argument("--dry-run", action="store_true", help="the default; accepted for clarity")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    if not args.apply:
        logger.info("DRY RUN: nothing will be written. Pass --apply to write.")

    stats = backfill(table, apply=args.apply)
    logger.info(
        "done: model_rows=%s other_models=%s %s=%s skipped=%s raced=%s failed=%s",
        stats["model_rows"], stats["other_models"],
        "updated" if args.apply else "would_update", stats["updated"],
        stats["skipped"], stats["raced"], stats["failed"],
    )
    if stats["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
