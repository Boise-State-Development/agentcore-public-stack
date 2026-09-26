"""Repair managed-KB byte counters left wrong by the pre-fix accounting.

Three defects in ``kb_backend/byte_cap`` accounting were fixed together; this script
repairs the records they already wrote. It is the one-off companion to that fix.

1. **Migrated corpora were never committed.** ``run_shadow`` reserves the whole
   corpus up front and nothing ever settled the reservation, so every migrated
   knowledge base holds its corpus in ``reservedBytes`` with ``storedBytes=0``, and
   its ``DOC#`` rows carry no settlement markers. (Checked against prod before the
   fix: on every promoted knowledge base, ``reservedBytes`` equalled the summed
   ``sizeBytes`` of its unsettled ``complete`` documents exactly.)
2. **Completed documents never refunded on delete.** Their bytes stayed in
   ``storedBytes``/``totalBytes`` after the row was gone.
3. **Pre-fix completed documents have no ``committedBytes``**, which is the only
   amount the new delete path refunds.

WHAT IT DOES, per managed knowledge base the worker is not part-way through
---------------------------------------------------------------------------
* **adopt** — when ``reservedBytes`` equals the summed size of the unsettled
  ``complete`` documents and no upload is in flight, each of those documents is
  claimed (``byte_cap.settle_as_committed``: ``byteCapSettled`` + ``committedBytes``)
  and committed (``reservedBytes`` -> ``storedBytes``; ``totalBytes`` unchanged).
  Any other shape is reported and left for a human.
* **backfill** — a settled ``complete`` document with ``retrievableAt`` (the
  ingestion consumer's completion stamp) but no ``committedBytes`` gets
  ``committedBytes = sizeBytes``, so deleting it refunds.
* **re-anchor** — with nothing in flight and nothing reserved, ``storedBytes`` and
  ``totalBytes`` are set to the ledger: the ``committedBytes`` of the live
  ``complete`` documents. That returns bytes leaked by earlier deletes and restores
  ``totalBytes == storedBytes + reservedBytes``. Conditioned on the counters being
  exactly what this run expects, so a concurrent upload or delete makes the write
  refuse (re-run it) rather than race.

SAFETY
------
* **Read-only by default.** Nothing is written without ``--apply``, and ``--apply``
  requires ``--confirm-prefix`` equal to ``--project-prefix``.
* ``--agent`` limits a run to named agent ids.
* Every write is conditional; a refused one is reported, never forced.

Run (a human, not CI; production is read-only from agents)::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/repair_managed_kb_byte_counters.py \\
        --project-prefix dev-boisestateai-v2 --region us-west-2                   # report only
    ... --apply --confirm-prefix dev-boisestateai-v2                                # repair

The table name is derived from the prefix (``{prefix}-rag-assistants``) and can be
overridden. The report names agent ids, document ids and byte counts only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

#: Statuses of a managed upload whose request-time reservation is still
#: outstanding. ``chunking``/``embedding`` are written only by the legacy pipeline
#: (``documents/models.py``), so a row stuck there on a managed knowledge base is a
#: pre-migration leftover that never reserved anything.
IN_FLIGHT_STATUSES = frozenset({"provisioning", "uploading"})


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _document_id(row: Dict[str, Any]) -> str:
    return str(row.get("SK", "")).split("#", 1)[1]


@dataclass
class KbPlan:
    agent_id: str
    stored: int
    reserved: int
    total: int
    adopt: List[Tuple[str, int]] = field(default_factory=list)
    backfill: List[Tuple[str, int]] = field(default_factory=list)
    in_flight: int = 0
    ledger: int = 0
    reanchor: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def stored_after_adopt(self) -> int:
        return self.stored + sum(n for _, n in self.adopt)

    @property
    def has_work(self) -> bool:
        return bool(self.adopt or self.backfill or self.reanchor)

    def summary(self) -> Dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "storedBytes": self.stored,
            "reservedBytes": self.reserved,
            "totalBytes": self.total,
            "inFlight": self.in_flight,
            "adopt": {"documents": len(self.adopt), "bytes": sum(n for _, n in self.adopt)},
            "backfill": {"documents": len(self.backfill), "bytes": sum(n for _, n in self.backfill)},
            "ledgerBytes": self.ledger,
            "reanchor": (
                {"storedBytes": [self.stored_after_adopt, self.ledger], "totalBytes": [self.total, self.ledger]}
                if self.reanchor
                else None
            ),
            "notes": self.notes,
        }


def plan_kb(agent_id: str, record: Dict[str, Any], rows: Sequence[Dict[str, Any]]) -> KbPlan:
    """What this knowledge base needs, from one read of its record and ``DOC#`` rows."""
    plan = KbPlan(
        agent_id=agent_id,
        stored=_int(record.get("storedBytes")),
        reserved=_int(record.get("reservedBytes")),
        total=_int(record.get("totalBytes")),
    )

    unsettled_complete: List[Tuple[str, int]] = []
    committed = 0
    for row in rows:
        status = row.get("status")
        settled = bool(row.get("byteCapSettled"))
        size = _int(row.get("sizeBytes"))
        if status in IN_FLIGHT_STATUSES and not settled:
            plan.in_flight += 1
        if status != "complete":
            continue
        if not settled:
            if size > 0:
                unsettled_complete.append((_document_id(row), size))
        elif row.get("committedBytes") is not None:
            if not row.get("byteCapRefunded"):
                committed += _int(row.get("committedBytes"))
        elif row.get("retrievableAt"):
            plan.backfill.append((_document_id(row), size))
        else:
            plan.notes.append(f"settled complete document {_document_id(row)} has no completion stamp; not counted")

    unsettled_bytes = sum(n for _, n in unsettled_complete)
    if unsettled_complete:
        if plan.in_flight == 0 and plan.reserved == unsettled_bytes:
            plan.adopt = unsettled_complete
        else:
            plan.notes.append(
                f"reservedBytes {plan.reserved} is not explained by {len(unsettled_complete)} unsettled "
                f"complete document(s) of {unsettled_bytes} bytes with {plan.in_flight} in flight; "
                f"left for review"
            )

    plan.ledger = committed + sum(n for _, n in plan.backfill) + sum(n for _, n in plan.adopt)

    reserved_after = plan.reserved - sum(n for _, n in plan.adopt)
    settled_everything = not unsettled_complete or bool(plan.adopt)
    if plan.in_flight == 0 and settled_everything and reserved_after == 0:
        plan.reanchor = plan.ledger != plan.stored_after_adopt or plan.total != plan.stored_after_adopt
    elif plan.in_flight == 0 and not unsettled_complete and plan.reserved:
        plan.notes.append(
            f"reservedBytes {plan.reserved} with no upload in flight is a leaked reservation; "
            f"left for review"
        )
    elif plan.total != plan.stored + plan.reserved:
        plan.notes.append("totalBytes != storedBytes + reservedBytes, but the record is not quiet; not re-anchored")
    return plan


# ── AWS ──────────────────────────────────────────────────────────────────────
def managed_records(table) -> List[Dict[str, Any]]:
    """Every ``KB#`` record on the managed engine that no worker step owns."""
    from apis.shared.kb_backend.records import ENGINE_MANAGED, WORK_ELIGIBLE_STATES

    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(SK, :kb)",
        "ExpressionAttributeValues": {":kb": "KB#"},
    }
    records: List[Dict[str, Any]] = []
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            if item.get("retrievalEngine") != ENGINE_MANAGED:
                continue
            if item.get("migrationState") in WORK_ELIGIBLE_STATES:
                continue
            records.append(item)
        if "LastEvaluatedKey" not in page:
            return records
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def document_rows(table, agent_id: str) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"AST#{agent_id}") & Key("SK").begins_with("DOC#"),
        "ConsistentRead": True,
    }
    rows: List[Dict[str, Any]] = []
    while True:
        page = table.query(**kwargs)
        rows.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def apply_plan(table, plan: KbPlan) -> Dict[str, Any]:
    """Carry out one plan. Every write is conditional; nothing is forced."""
    from botocore.exceptions import ClientError

    from apis.shared.kb_backend import byte_cap

    agent_id = plan.agent_id
    result: Dict[str, Any] = collections.Counter()

    for document_id, n_bytes in plan.adopt:
        if byte_cap.settle_as_committed(agent_id, document_id, n_bytes):
            byte_cap.commit(agent_id, agent_id, n_bytes)
            result["adopted"] += 1
        else:
            result["adoptSkipped"] += 1

    for document_id, n_bytes in plan.backfill:
        try:
            table.update_item(
                Key={"PK": f"AST#{agent_id}", "SK": f"DOC#{document_id}"},
                UpdateExpression="SET committedBytes = :n",
                ConditionExpression=(
                    "attribute_exists(PK) AND attribute_exists(byteCapSettled) "
                    "AND attribute_exists(retrievableAt) AND attribute_not_exists(committedBytes) "
                    "AND #s = :complete"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":n": Decimal(n_bytes), ":complete": "complete"},
            )
            result["backfilled"] += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            result["backfillSkipped"] += 1

    if plan.reanchor:
        if result["adoptSkipped"]:
            result["reanchor"] = "skipped: an adoption was refused; re-run"
        else:
            try:
                table.update_item(
                    Key={"PK": f"AST#{agent_id}", "SK": f"KB#{agent_id}"},
                    UpdateExpression="SET storedBytes = :ledger, totalBytes = :ledger",
                    ConditionExpression=(
                        "attribute_exists(PK) AND storedBytes = :stored AND totalBytes = :total "
                        "AND (attribute_not_exists(reservedBytes) OR reservedBytes = :zero)"
                    ),
                    ExpressionAttributeValues={
                        ":ledger": Decimal(plan.ledger),
                        ":stored": Decimal(plan.stored_after_adopt),
                        ":total": Decimal(plan.total),
                        ":zero": Decimal(0),
                    },
                )
                result["reanchor"] = "done"
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                result["reanchor"] = "refused: counters changed since the read; re-run"
    return dict(result)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-prefix", required=True, help="e.g. dev-boisestateai-v2")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--profile", default=None, help="AWS profile name (or set AWS_PROFILE)")
    p.add_argument("--agent", action="append", default=[], help="Only these agent ids (repeatable)")
    p.add_argument("--apply", action="store_true", help="Repair; without it the run only reports")
    p.add_argument("--confirm-prefix", default=None, help="Required with --apply; must equal --project-prefix")
    p.add_argument("--table", default=None, help="Override {prefix}-rag-assistants")
    p.add_argument("--out", default=None, help="Write the JSON report here")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.apply and args.confirm_prefix != args.project_prefix:
        print("--apply requires --confirm-prefix equal to --project-prefix", file=sys.stderr)
        return 2
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    table_name = args.table or f"{args.project_prefix}-rag-assistants"
    # byte_cap resolves its table from the environment.
    os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"] = table_name
    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)

    import boto3

    table = boto3.resource("dynamodb", region_name=args.region).Table(table_name)

    records = managed_records(table)
    if args.agent:
        wanted = set(args.agent)
        records = [r for r in records if str(r["PK"]).split("#", 1)[1] in wanted]

    print(f"{'APPLY' if args.apply else 'REPORT ONLY'}: {table_name}, {len(records)} managed knowledge base(s)")
    results = []
    for record in records:
        agent_id = str(record["PK"]).split("#", 1)[1]
        plan = plan_kb(agent_id, record, document_rows(table, agent_id))
        summary = plan.summary()
        if args.apply and plan.has_work:
            summary["result"] = apply_plan(table, plan)
        results.append(summary)
        print(json.dumps(summary), flush=True)

    if args.out:
        report = {
            "table": table_name,
            "applied": args.apply,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "knowledgeBases": results,
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
