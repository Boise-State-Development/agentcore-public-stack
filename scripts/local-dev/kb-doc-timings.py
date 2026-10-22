#!/usr/bin/env python3
"""Print the real ingestion timeline for an assistant's documents, by engine.

Read-only. Answers "how long does this actually take, and which engine did it?"
without waiting on a UI that currently reports the legacy pipeline's progress
even for a promoted knowledge base.

    cd backend
    uv run python ../scripts/local-dev/kb-doc-timings.py ast-1a90784a7f18

WHY THE TWO ENGINES ARE DISTINGUISHABLE FROM THE RECORD ALONE
`chunkCount` and `vectorStoreId` are written only by the legacy pipeline
(`documents/ingestion/handler.py`). `indexedAt` and `retrievableAt` are written
only by the managed ingestion consumer (`kb_migration/ingestion_consumer.py`).
A document carrying both was indexed twice — which is the current behaviour on a
promoted knowledge base, because the legacy S3 notification has no engine gate.

WHAT THE `status ready before managed` COLUMN MEANS
The legacy pipeline finishes first and writes `status=complete`. On a promoted
knowledge base, retrieval is served by the managed backend, which is not
answering yet. That column is how long the UI claimed the document was ready
while the engine that actually serves it had nothing.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "src"))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / "backend" / "src" / ".env", override=True)

import boto3


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _secs(a, b):
    if not a or not b:
        return None
    return (b - a).total_seconds()


def _fmt(seconds):
    return "—" if seconds is None else f"{seconds:6.1f}s"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    assistant_id = sys.argv[1]

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        print("[ERROR] DYNAMODB_ASSISTANTS_TABLE_NAME is not set (backend/src/.env)")
        return 1

    region = os.environ.get("AWS_REGION", "us-west-2")
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    kb = table.get_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"KB#{assistant_id}"}
    ).get("Item") or {}
    engine = kb.get("retrievalEngine", "s3vectors (absent ⇒ legacy)")
    print(f"\nassistant       : {assistant_id}")
    print(f"retrievalEngine : {engine}")
    print(f"migrationState  : {kb.get('migrationState', '—')}")
    print(f"awsKbId         : {kb.get('awsKbId', '—')}")

    docs = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"AST#{assistant_id}",
            ":sk": "DOC#",
        },
    ).get("Items", [])
    if not docs:
        print("\nno documents.")
        return 0

    docs.sort(key=lambda d: d.get("createdAt", ""))

    print(
        f"\n{'document':22} {'status':9} {'legacy':>8} {'mgd idx':>8} "
        f"{'mgd rdy':>8} {'idx→rdy':>8}  engines"
    )
    print("-" * 94)

    for d in docs:
        created = _parse(d.get("createdAt"))
        indexed = _parse(d.get("indexedAt"))
        retrievable = _parse(d.get("retrievableAt"))
        updated = _parse(d.get("updatedAt"))

        did_legacy = "chunkCount" in d or "vectorStoreId" in d
        did_managed = bool(indexed or retrievable)

        # The legacy pipeline's finish time is not stored once the managed
        # consumer overwrites `updatedAt`, so it is only knowable when managed
        # did not run. Reported as `lost` rather than as a dash, which would
        # read as "legacy did not run" — the opposite of the truth here.
        legacy_done = None if did_managed else _secs(created, updated)
        legacy_cell = "  (lost)" if did_managed and did_legacy else _fmt(legacy_done)

        engines = "+".join(
            part for part, on in (("legacy", did_legacy), ("managed", did_managed)) if on
        ) or "none"

        marks = []
        if did_legacy and did_managed:
            marks.append("DOUBLE-INDEXED")

        print(
            f"{str(d.get('documentId'))[:22]:22} "
            f"{str(d.get('status'))[:9]:9} "
            f"{legacy_cell:>8} "
            f"{_fmt(_secs(created, indexed)):>8} "
            f"{_fmt(_secs(created, retrievable)):>8} "
            f"{_fmt(_secs(indexed, retrievable)):>8}  "
            f"{engines}"
            + (f"   [{', '.join(marks)}]" if marks else "")
        )
        if d.get("status") == "failed" and d.get("ingestionError"):
            print(f"{'':22} error: {str(d['ingestionError'])[:78]}")

    print(
        "\nAll times are from createdAt (the moment of upload). Both pipelines are\n"
        "triggered by the same S3 upload and run in PARALLEL, so these are not\n"
        "additive — they are two independent answers to 'when was it usable?'.\n\n"
        "legacy   = upload → status complete, the OLD S3 Vectors pipeline.\n"
        "           `(lost)` means legacy did run, but the managed consumer\n"
        "           finished later and overwrote updatedAt, erasing legacy's\n"
        "           finish time. It is not recoverable after the fact.\n"
        "mgd idx  = upload → indexedAt      (Bedrock reports the doc INDEXED)\n"
        "mgd rdy  = upload → retrievableAt  (a real Retrieve returns it — this is\n"
        "           the honest 'usable' number for the NEW engine)\n"
        "idx→rdy  = the gap INDEXED does not cover; evaluation measured 0.75–1.03 s\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
