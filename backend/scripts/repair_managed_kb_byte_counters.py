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
* **settle-unexplained** (opt-in, ``--settle-unexplained`` with ``--agent``) — for a
  quiet knowledge base whose counters no document explains, trust the ledger:
  every unsettled ``complete`` document is claimed as committed, and the counters
  are set to ``storedBytes = totalBytes = ledger``, ``reservedBytes = 0``. See
  below for the shape that needs it and why it is safe.

THE SHAPE --settle-unexplained EXISTS FOR
-----------------------------------------
File-source imports, crawls and syncs reserve nothing at request time, but write
their real ``sizeBytes`` before the consumer reads the row. The consumer took that
size for a reservation and committed it out of ``reservedBytes`` it never added to
(fixed in ``byte_cap.reserved_at_request``). Each such document left
``reservedBytes`` and ``totalBytes`` short by its size. On a migrated knowledge base
still holding its corpus as a reservation, that reads as::

    reservedBytes == unsettled complete bytes - bytes of imports committed since

which the adopt rule rightly refuses; on one with nothing else reserved it reads as
a negative ``reservedBytes``. The report names that diagnosis when the numbers fit.

Once nothing is in flight, no reservation is legitimately outstanding, so the
counters the ledger implies are the right ones whatever produced the drift. The
mode still refuses unless the knowledge base is provably quiet: no upload in
flight, no row part-way through a commit or a refund, and the same documents seen
again after ``--quiet-seconds`` (an upload reserves before its ``DOC#`` row is
written, so a reservation read with no row yet must get time to show itself). The
claims and the counter write are ONE DynamoDB transaction, conditioned on every
counter being exactly as read and every claimed row being unchanged, so a
concurrent upload, delete, consumer commit or reconciler re-anchor cancels the
whole repair rather than interleaving with half of it, and a re-run finds nothing
left to do.

SAFETY
------
* **Read-only by default.** Nothing is written without ``--apply``, and ``--apply``
  requires ``--confirm-prefix`` equal to ``--project-prefix``.
* ``--agent`` limits a run to named agent ids. ``--settle-unexplained`` requires it.
* Every write is conditional; a refused one is reported, never forced.

Run (a human, not CI; production is read-only from agents)::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/repair_managed_kb_byte_counters.py \\
        --project-prefix dev-boisestateai-v2 --region us-west-2                   # report only
    ... --apply --confirm-prefix dev-boisestateai-v2                                # repair
    ... --settle-unexplained --agent <agent-id>                                     # preview one
    ... --settle-unexplained --agent <agent-id> --apply --confirm-prefix ...       # settle one

The table name is derived from the prefix (``{prefix}-rag-assistants``) and can be
overridden. The report names agent ids, document ids and byte counts only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
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

#: DynamoDB's cap on one TransactWriteItems call. A settle claims every unsettled
#: or un-backfilled document and writes the ``KB#`` record in one transaction.
MAX_TRANSACTION_ITEMS = 100

#: How long a settle waits, from reading the ``KB#`` record, before re-reading the
#: documents. See "THE SHAPE --settle-unexplained EXISTS FOR" above.
DEFAULT_QUIET_SECONDS = 10.0


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
    #: The counters are not explained by the documents; left for review unless
    #: ``--settle-unexplained`` makes ``settle`` true.
    unexplained: bool = False
    settle: bool = False
    #: ``time.monotonic()`` when the ``KB#`` record was read; a settle waits out
    #: its quiet window from here.
    read_at: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def stored_after_adopt(self) -> int:
        return self.stored + sum(n for _, n in self.adopt)

    @property
    def has_work(self) -> bool:
        return bool(self.adopt or self.backfill or self.reanchor or self.settle)

    def fingerprint(self) -> Tuple[Any, ...]:
        """What a settle writes. Two reads that agree on this settle identically."""
        return (self.settle, sorted(self.adopt), sorted(self.backfill), self.ledger, self.in_flight)

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
            "settle": (
                {
                    "storedBytes": [self.stored, self.ledger],
                    "reservedBytes": [self.reserved, 0],
                    "totalBytes": [self.total, self.ledger],
                }
                if self.settle
                else None
            ),
            "notes": self.notes,
        }


def plan_kb(
    agent_id: str,
    record: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    settle_unexplained: bool = False,
    read_at: Optional[float] = None,
) -> KbPlan:
    """What this knowledge base needs, from one read of its record and ``DOC#`` rows."""
    plan = KbPlan(
        agent_id=agent_id,
        stored=_int(record.get("storedBytes")),
        reserved=_int(record.get("reservedBytes")),
        total=_int(record.get("totalBytes")),
        read_at=time.monotonic() if read_at is None else read_at,
    )

    unsettled_complete: List[Tuple[str, int]] = []
    committed = 0
    #: Ledger bytes of documents that never reserved at request time (they carry a
    #: ``sourceAdapterKey``). Before the fix each committed without a reservation.
    import_bytes = 0
    import_documents = 0
    #: Rows whose bytes are in the counters but not in the ledger yet (a consumer
    #: between its commit and ``complete``) or no longer (a delete between
    #: ``deleting`` and its refund). A settle would count them wrong.
    mid_settlement: List[str] = []
    for row in rows:
        status = row.get("status")
        settled = bool(row.get("byteCapSettled"))
        size = _int(row.get("sizeBytes"))
        if status in IN_FLIGHT_STATUSES and not settled:
            plan.in_flight += 1
        if status != "complete":
            if row.get("committedBytes") is not None and not row.get("byteCapRefunded"):
                mid_settlement.append(_document_id(row))
            continue
        ledger_bytes: Optional[int] = None
        if not settled:
            if size > 0:
                unsettled_complete.append((_document_id(row), size))
        elif row.get("committedBytes") is not None:
            if not row.get("byteCapRefunded"):
                ledger_bytes = _int(row.get("committedBytes"))
                committed += ledger_bytes
        elif row.get("retrievableAt"):
            plan.backfill.append((_document_id(row), size))
            ledger_bytes = size
        else:
            plan.notes.append(f"settled complete document {_document_id(row)} has no completion stamp; not counted")
        if ledger_bytes and row.get("sourceAdapterKey"):
            import_bytes += ledger_bytes
            import_documents += 1

    unsettled_bytes = sum(n for _, n in unsettled_complete)
    if unsettled_complete:
        if plan.in_flight == 0 and plan.reserved == unsettled_bytes:
            plan.adopt = unsettled_complete
        else:
            plan.unexplained = True
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
        plan.unexplained = True
        plan.notes.append(
            f"reservedBytes {plan.reserved} with no upload in flight is a leaked reservation; "
            f"left for review"
        )
    elif plan.total != plan.stored + plan.reserved:
        plan.notes.append("totalBytes != storedBytes + reservedBytes, but the record is not quiet; not re-anchored")

    if plan.unexplained and import_bytes and plan.reserved == unsettled_bytes - import_bytes:
        plan.notes.append(
            f"explained exactly by {import_documents} imported/crawled/synced document(s) of "
            f"{import_bytes} bytes committed without a request-time reservation (pre-fix consumer); "
            f"--settle-unexplained repairs it"
        )

    if settle_unexplained and plan.unexplained:
        _plan_settle(plan, unsettled_complete, committed, mid_settlement)
    return plan


def _plan_settle(
    plan: KbPlan,
    unsettled_complete: List[Tuple[str, int]],
    committed: int,
    mid_settlement: List[str],
) -> None:
    """Turn an unexplained plan into a settle, if the knowledge base is quiet."""
    blockers = []
    if plan.in_flight:
        blockers.append(f"{plan.in_flight} upload(s) in flight")
    if mid_settlement:
        blockers.append(f"document(s) part-way through a commit or refund: {', '.join(sorted(mid_settlement))}")
    items = len(unsettled_complete) + len(plan.backfill) + 1
    if items > MAX_TRANSACTION_ITEMS:
        blockers.append(f"{items} writes exceed one {MAX_TRANSACTION_ITEMS}-item transaction")
    if blockers:
        plan.notes.append(f"not settled: {'; '.join(blockers)}")
        return
    plan.settle = True
    plan.adopt = unsettled_complete
    plan.reanchor = False
    plan.ledger = committed + sum(n for _, n in plan.backfill) + sum(n for _, n in plan.adopt)


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


def _counter_condition(name: str, value: int, values: Dict[str, Any]) -> str:
    """``name`` is exactly ``value``; an absent counter reads as 0, as ``_int`` does."""
    placeholder = f":was_{name}"
    values[placeholder] = Decimal(value)
    if value == 0:
        return f"(attribute_not_exists({name}) OR {name} = {placeholder})"
    return f"{name} = {placeholder}"


def settle_transaction(table_name: str, plan: KbPlan) -> List[Dict[str, Any]]:
    """The settle as one TransactWriteItems request.

    Plain Python values: it is sent through the table resource's own client
    (``table.meta.client``), which serializes them the way the resource does.
    """
    agent_id = plan.agent_id

    def update(key: Dict[str, str], expression: str, condition: str, values: Dict[str, Any],
               names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "TableName": table_name,
            "Key": key,
            "UpdateExpression": expression,
            "ConditionExpression": condition,
            "ExpressionAttributeValues": values,
        }
        if names:
            item["ExpressionAttributeNames"] = names
        return {"Update": item}

    items = []
    for document_id, n_bytes in plan.adopt:
        items.append(update(
            {"PK": f"AST#{agent_id}", "SK": f"DOC#{document_id}"},
            "SET byteCapSettled = :true, committedBytes = :n",
            "attribute_exists(PK) AND attribute_not_exists(byteCapSettled) "
            "AND #s = :complete AND sizeBytes = :n",
            {":true": True, ":n": Decimal(n_bytes), ":complete": "complete"},
            {"#s": "status"},
        ))
    for document_id, n_bytes in plan.backfill:
        items.append(update(
            {"PK": f"AST#{agent_id}", "SK": f"DOC#{document_id}"},
            "SET committedBytes = :n",
            "attribute_exists(PK) AND attribute_exists(byteCapSettled) "
            "AND attribute_exists(retrievableAt) AND attribute_not_exists(committedBytes) "
            "AND attribute_not_exists(byteCapRefunded) AND #s = :complete AND sizeBytes = :n",
            {":n": Decimal(n_bytes), ":complete": "complete"},
            {"#s": "status"},
        ))
    values: Dict[str, Any] = {":ledger": Decimal(plan.ledger), ":zero": Decimal(0)}
    condition = " AND ".join(
        ["attribute_exists(PK)"]
        + [
            _counter_condition(name, value, values)
            for name, value in (
                ("storedBytes", plan.stored),
                ("reservedBytes", plan.reserved),
                ("totalBytes", plan.total),
            )
        ]
    )
    items.append(update(
        {"PK": f"AST#{agent_id}", "SK": f"KB#{agent_id}"},
        "SET storedBytes = :ledger, reservedBytes = :zero, totalBytes = :ledger",
        condition,
        values,
    ))
    return items


def apply_settle(table, plan: KbPlan, quiet_seconds: float = DEFAULT_QUIET_SECONDS) -> Dict[str, Any]:
    """Carry out a settle: re-check after the quiet window, then one transaction."""
    from botocore.exceptions import ClientError

    wait = plan.read_at + quiet_seconds - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    record = {"storedBytes": plan.stored, "reservedBytes": plan.reserved, "totalBytes": plan.total}
    again = plan_kb(plan.agent_id, record, document_rows(table, plan.agent_id), settle_unexplained=True)
    if again.fingerprint() != plan.fingerprint():
        return {"settle": "refused: the documents changed during the quiet window; re-run"}

    try:
        table.meta.client.transact_write_items(TransactItems=settle_transaction(table.name, plan))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])]
        return {"settle": f"refused: something changed since the read ({reasons}); re-run"}
    return {"settle": "done", "adopted": len(plan.adopt), "backfilled": len(plan.backfill)}


def apply_plan(table, plan: KbPlan, quiet_seconds: float = DEFAULT_QUIET_SECONDS) -> Dict[str, Any]:
    """Carry out one plan. Every write is conditional; nothing is forced."""
    from botocore.exceptions import ClientError

    from apis.shared.kb_backend import byte_cap

    if plan.settle:
        return apply_settle(table, plan, quiet_seconds)

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
    p.add_argument(
        "--settle-unexplained",
        action="store_true",
        help="For the --agent knowledge bases only: settle counters no document explains to the ledger",
    )
    p.add_argument(
        "--quiet-seconds",
        type=float,
        default=DEFAULT_QUIET_SECONDS,
        help="How long a settle waits after reading a record before re-checking its documents",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.apply and args.confirm_prefix != args.project_prefix:
        print("--apply requires --confirm-prefix equal to --project-prefix", file=sys.stderr)
        return 2
    if args.settle_unexplained and not args.agent:
        print("--settle-unexplained requires --agent: it is decided one knowledge base at a time", file=sys.stderr)
        return 2
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    table_name = args.table or f"{args.project_prefix}-rag-assistants"
    # byte_cap resolves its table from the environment.
    os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"] = table_name
    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)

    import boto3

    table = boto3.resource("dynamodb", region_name=args.region).Table(table_name)

    read_at = time.monotonic()
    records = managed_records(table)
    if args.agent:
        wanted = set(args.agent)
        records = [r for r in records if str(r["PK"]).split("#", 1)[1] in wanted]

    print(f"{'APPLY' if args.apply else 'REPORT ONLY'}: {table_name}, {len(records)} managed knowledge base(s)")
    results = []
    for record in records:
        agent_id = str(record["PK"]).split("#", 1)[1]
        plan = plan_kb(
            agent_id, record, document_rows(table, agent_id),
            settle_unexplained=args.settle_unexplained, read_at=read_at,
        )
        summary = plan.summary()
        if args.apply and plan.has_work:
            summary["result"] = apply_plan(table, plan, args.quiet_seconds)
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
