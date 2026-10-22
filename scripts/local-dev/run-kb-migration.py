#!/usr/bin/env python3
"""Drive a managed-KB migration locally against the dev environment.

Does what the dispatcher + worker Lambdas do, but in-process with your SSO
credentials, so the shadow -> verify -> promote -> retain path can be iterated in
seconds instead of a merge, an image build, a deploy and a 15-minute tick.

    cd backend
    uv run python ../scripts/local-dev/run-kb-migration.py ast-1a90784a7f18
    uv run python ../scripts/local-dev/run-kb-migration.py ast-... --once
    uv run python ../scripts/local-dev/run-kb-migration.py ast-... --show

WHAT THIS DOES AND DOES NOT PROVE

It talks to the real Bedrock, DynamoDB and S3 in dev, so it exercises the actual
API contracts, the real state machine and real documents. What it does NOT
exercise is the worker Lambda's IAM role — your SSO identity is broader — nor the
CDK environment wiring, nor the image contents. Those are deploy-time concerns and
are checked by deploying. Getting the logic right here first is the point.

Writes to real dev records. Point it at a knowledge base you are willing to churn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[2] / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_SRC / ".env", override=True)

REQUIRED = (
    "DYNAMODB_ASSISTANTS_TABLE_NAME",
    "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME",
    "MANAGED_KB_SERVICE_ROLE_ARN",
)


def _preflight() -> None:
    missing = [v for v in REQUIRED if not os.environ.get(v)]
    if missing:
        sys.exit(
            f"missing env: {missing}\nCopy them from the deployed worker Lambda into "
            f"backend/src/.env."
        )


def _show(assistant_id: str) -> None:
    from apis.shared.kb_backend import records as r

    record = r.get_kb_record(assistant_id, assistant_id)
    if not record:
        print("  no KB record")
        return
    interesting = [
        "migrationState", "migrationGeneration", "provisioningState", "awsKbId",
        "awsDataSourceId", "retrievalEngine", "migrationProgress", "migrationError",
        "totalBytes", "retainUntil", "promotedAt", "GSI7_PK",
    ]
    for key in interesting:
        if key in record:
            value = record[key]
            if isinstance(value, dict):
                value = {k: str(v) for k, v in value.items()}
            text = str(value)
            print(f"  {key:22} = {text[:110]}")
    engine = "managed" if record.get("retrievalEngine") == "managed" else "legacy (absent)"
    print(f"  {'-> serving from':22} = {engine}")


async def _drive(assistant_id: str, once: bool, max_steps: int, break_lease: bool) -> int:
    from apis.app_api.kb_migration import worker
    from apis.shared.kb_backend import records as r

    for step in range(1, max_steps + 1):
        record = r.get_kb_record(assistant_id, assistant_id)
        if not record:
            print("  no KB record — enrol from the UI first, or it was torn down")
            return 1
        state = record.get("migrationState")
        print(f"\n── step {step}: state={state} gen={record.get('migrationGeneration')}")

        if state in (r.RETAIN, r.MIGRATION_FAILED):
            print(f"  terminal: {state}")
            if state == r.MIGRATION_FAILED:
                print(f"  error: {str(record.get('migrationError'))[:400]}")
            _show(assistant_id)
            return 0 if state == r.RETAIN else 2

        if break_lease and record.get("migrationLeaseUntil"):
            # Simulates lease expiry so consecutive local steps do not have to wait
            # out the 15-minute window. Safe ONLY because --break-lease is paired
            # with deferring dueAt, which keeps the deployed dispatcher from
            # claiming the same record while we hold it. Never run this against a
            # record a real worker may be mid-step on.
            _table(r).update_item(
                Key={"PK": r.kb_pk(assistant_id), "SK": r.kb_sk(assistant_id)},
                UpdateExpression="REMOVE migrationLeaseUntil",
            )
            print("  (lease cleared for local stepping)")

        try:
            result = await worker.run_step(assistant_id, assistant_id)
            print(f"  result: {json.dumps(result.as_log_fields(), default=str)[:300]}")
        except Exception as exc:  # noqa: BLE001 — this is a diagnostic driver
            print(f"  RAISED {type(exc).__name__}: {str(exc)[:400]}")
            _show(assistant_id)
            return 3

        if once:
            _show(assistant_id)
            return 0

    print(f"\n  stopped after {max_steps} steps without reaching a terminal state")
    _show(assistant_id)
    return 4


def _table(records_module):
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"]
    )


def _defer(assistant_id: str, minutes: int) -> None:
    """Push dueAt out so the deployed dispatcher leaves this record alone.

    The dispatcher sweeps on `GSI7_SK <= now`, so a future dueAt makes the record
    invisible to it without removing it from the index — which means nothing is
    lost if this driver dies part-way.
    """
    from datetime import datetime, timedelta, timezone

    from apis.shared.kb_backend import records as r

    due = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    due = due.replace("+00:00", "Z")
    _table(r).update_item(
        Key={"PK": r.kb_pk(assistant_id), "SK": r.kb_sk(assistant_id)},
        UpdateExpression="SET GSI7_SK = :due",
        ConditionExpression="attribute_exists(GSI7_SK)",
        ExpressionAttributeValues={":due": due},
    )
    print(f"  deferred dueAt to {due} so the deployed dispatcher skips it")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("assistant_id")
    parser.add_argument("--once", action="store_true", help="run a single step")
    parser.add_argument("--show", action="store_true", help="print state and exit")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--break-lease", action="store_true",
                        help="clear the lease between steps (implies --defer)")
    parser.add_argument("--defer", type=int, default=0, metavar="MIN",
                        help="push dueAt out so the deployed dispatcher skips it")
    args = parser.parse_args()

    _preflight()
    print(f"table={os.environ['DYNAMODB_ASSISTANTS_TABLE_NAME']}")
    print(f"kb={args.assistant_id}")

    if args.show:
        _show(args.assistant_id)
        return 0
    minutes = args.defer or (20 if args.break_lease else 0)
    if minutes:
        _defer(args.assistant_id, minutes)
    return asyncio.run(
        _drive(args.assistant_id, args.once, args.max_steps, args.break_lease)
    )


if __name__ == "__main__":
    sys.exit(main())
