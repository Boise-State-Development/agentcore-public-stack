"""Remove stray ``DOC#`` rows (``DOC#icons`` and the like) from live agents.

Agent icons are stored in the RAG documents bucket at
``assistants/{agentId}/icons/{digest}.{png|jpg}``, and the rag-ingestion Lambda's
S3 notification (prefix ``assistants/``) delivered them to the ingestion pipeline.
Its key parser read the 4-part icon key as ``document_id="icons"``, the pipeline
failed on the image, and it left a ``DOC#icons`` row with ``status=failed`` and no
``s3Key`` in the agent's partition. The parser is fixed; this script clears the rows
it left before the fix.

``cleanup_orphaned_agent_rows.py`` does not cover these: it only acts on agents
whose ``METADATA`` row is gone, and these rows sit on live agents.

WHAT IT DOES
------------
1. Scans the table for ``AST#`` partitions with a ``METADATA`` row (live agents).
2. A **stray** is a ``DOC#`` row whose id does not start with ``DOC-``. Every real
   document id comes from ``document_service._generate_document_id`` (``DOC-`` plus
   12 hex characters), so anything else was written by a mis-parsed S3 key.
3. Reports every stray. With ``--apply``, deletes the strays that have no ``s3Key``
   (the delete is conditional on that, so a row written in between is kept). A stray
   that does have an ``s3Key`` is reported and left alone for a human to look at.

It never touches S3: the icon object the row was parsed from is the agent's live
icon, which ``iconKey`` on the ``METADATA`` row still points at.

SAFETY
------
* **Read-only by default.** Nothing is written without ``--apply``, and ``--apply``
  requires ``--confirm-prefix`` equal to ``--project-prefix``.
* ``--agent`` limits a run to named agent ids.

Run (a human, not CI; production is read-only from agents)::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/cleanup_stray_doc_rows.py \\
        --project-prefix dev-boisestateai-v2 --region us-west-2                   # report only
    ... --apply --confirm-prefix dev-boisestateai-v2                                # delete

The table name is derived from the prefix (``{prefix}-rag-assistants``) and can be
overridden. The report names agent ids and row ids only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

#: Prefix of every id ``document_service._generate_document_id`` produces.
DOCUMENT_ID_PREFIX = "DOC-"


@dataclass
class Stray:
    agent_id: str
    row_id: str
    status: str
    s3_key: Optional[str]

    @property
    def deletable(self) -> bool:
        return not self.s3_key

    def summary(self) -> Dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "sk": f"DOC#{self.row_id}",
            "status": self.status,
            "hasS3Key": bool(self.s3_key),
            "action": "delete" if self.deletable else "leave (has s3Key)",
        }


def find_strays(items: Sequence[Dict[str, Any]]) -> List[Stray]:
    """Stray ``DOC#`` rows on live agents, from a table scan."""
    partitions: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for item in items:
        if str(item.get("PK", "")).startswith("AST#"):
            partitions[item["PK"]].append(item)

    strays: List[Stray] = []
    for pk, rows in sorted(partitions.items()):
        if not any(row["SK"] == "METADATA" for row in rows):
            continue
        for row in rows:
            sk = str(row["SK"])
            if not sk.startswith("DOC#"):
                continue
            row_id = sk.split("#", 1)[1]
            if row_id.startswith(DOCUMENT_ID_PREFIX):
                continue
            strays.append(
                Stray(agent_id=pk.split("#", 1)[1], row_id=row_id, status=str(row.get("status")), s3_key=row.get("s3Key"))
            )
    return strays


# ── AWS ──────────────────────────────────────────────────────────────────────
def scan_table(table) -> List[Dict[str, Any]]:
    """Only the attributes this script reads, and only METADATA and DOC# rows."""
    kwargs: Dict[str, Any] = {
        "ProjectionExpression": "PK, SK, #s, s3Key",
        "FilterExpression": "SK = :meta OR begins_with(SK, :doc)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":meta": "METADATA", ":doc": "DOC#"},
    }
    items: List[Dict[str, Any]] = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def delete_stray(table, stray: Stray) -> bool:
    """Delete one stray, only if it still has no ``s3Key``. True if deleted."""
    from botocore.exceptions import ClientError

    try:
        table.delete_item(
            Key={"PK": f"AST#{stray.agent_id}", "SK": f"DOC#{stray.row_id}"},
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(s3Key)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-prefix", required=True, help="e.g. dev-boisestateai-v2")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--profile", default=None, help="AWS profile name (or set AWS_PROFILE)")
    p.add_argument("--agent", action="append", default=[], help="Only these agent ids (repeatable)")
    p.add_argument("--apply", action="store_true", help="Delete; without it the run only reports")
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

    import boto3

    table_name = args.table or f"{args.project_prefix}-rag-assistants"
    table = boto3.resource("dynamodb", region_name=args.region).Table(table_name)

    strays = find_strays(scan_table(table))
    if args.agent:
        wanted = set(args.agent)
        strays = [s for s in strays if s.agent_id in wanted]

    print(f"{'APPLY' if args.apply else 'REPORT ONLY'}: {table_name}, {len(strays)} stray DOC# rows on live agents")
    outcome = collections.Counter()
    results = []
    for stray in strays:
        summary = stray.summary()
        if args.apply:
            if not stray.deletable:
                summary["result"] = "left"
            else:
                summary["result"] = "deleted" if delete_stray(table, stray) else "kept (changed since scan)"
            outcome[summary["result"]] += 1
        results.append(summary)
        print(json.dumps(summary), flush=True)
    if args.apply:
        print(json.dumps({"outcome": dict(outcome)}))

    if args.out:
        report = {
            "table": table_name,
            "applied": args.apply,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "strays": results,
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
