"""The one-off cleanup of rows deleted agents left behind (scripts/cleanup_orphaned_agent_rows.py)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cleanup_orphaned_agent_rows as cleanup  # noqa: E402

REGION = "us-east-1"
TABLE = "test-rag-assistants"
BUCKET = "test-rag-documents"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
AGENT = "ast-a1b2c3d4e5f6"


def _row(agent, sk, **fields):
    return {"PK": f"AST#{agent}", "SK": sk, **fields}


class TestFindOrphans:
    def test_only_partitions_without_metadata_old_enough(self):
        items = [
            _row("ast-live00000001", "METADATA", updatedAt="2026-09-01T00:00:00Z"),
            _row("ast-live00000001", "DOC#DOC-1", updatedAt="2026-09-01T00:00:00Z"),
            _row("ast-old000000001", "DOC#DOC-2", updatedAt="2026-09-01T00:00:00Z"),
            # The legacy "+00:00Z" spelling still sorts correctly on its first 19 chars.
            _row("ast-old000000002", "SHARE#someone", updatedAt="2026-07-07T17:10:40.748562+00:00Z"),
            _row("ast-young0000001", "DOC#DOC-3", updatedAt="2026-09-25T11:30:00Z"),
            {"PK": "KBWORK#teardown", "SK": "x"},
        ]
        ready, young = cleanup.find_orphans(items, NOW, min_age_hours=24)

        assert [o.agent_id for o in ready] == ["ast-old000000001", "ast-old000000002"]
        assert [o.agent_id for o in young] == ["ast-young0000001"]

    def test_the_summary_reports_types_and_never_share_emails(self):
        orphan = cleanup.Orphan(AGENT, [
            _row(AGENT, "DOC#DOC-1", status="complete", sizeBytes=Decimal(10)),
            _row(AGENT, "SHARE#prof@example.com"),
            _row(AGENT, "WEIRD#x"),
        ])
        summary = orphan.summary()
        assert summary["rows"] == {"DOC": 1, "SHARE": 1, "WEIRD": 1}
        assert summary["documentBytes"] == 10
        assert summary["unhandled"] == ["WEIRD"]
        assert "prof@example.com" not in str(summary)


@pytest.fixture()
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
        "DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE, "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": BUCKET,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        yield boto3.resource("dynamodb").Table(TABLE), s3


def _seed(table, s3):
    rows = [
        _row(AGENT, "KB#" + AGENT, appKbId=AGENT, awsKbId="KBTEST0001", migrationState="retain",
             migrationGeneration=Decimal(0)),
        _row(AGENT, "DOC#DOC-1", status="complete", chunkCount=Decimal(3),
             s3Key=f"assistants/{AGENT}/documents/DOC-1/plan.pdf", sizeBytes=Decimal(5)),
        _row(AGENT, "DOC#icons", status="failed"),  # a stray row: no s3Key, never indexed
        _row(AGENT, "SHARE#prof@example.com", GSI3_PK="SHARE#prof@example.com"),
        _row(AGENT, "CRAWL#CRAWL-1", status="complete"),
        _row(AGENT, "WEIRD#x"),
    ]
    for row in rows:
        table.put_item(Item=row)
    for key in (f"assistants/{AGENT}/documents/DOC-1/plan.pdf", f"assistants/{AGENT}/icons/icon.png"):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
    return rows


def _left(table):
    return sorted(i["SK"] for i in table.query(KeyConditionExpression=Key("PK").eq(f"AST#{AGENT}"))["Items"])


class TestCleanOrphan:
    def test_cleans_every_handled_row_type_and_the_s3_prefix(self, aws):
        table, s3 = aws
        rows = _seed(table, s3)

        async def fake_cleanup(**kwargs):
            # The app's cleanup deletes the object, then the row once every phase succeeded.
            s3.delete_object(Bucket=BUCKET, Key=kwargs["s3_key"])
            table.delete_item(Key={"PK": f"AST#{AGENT}", "SK": f"DOC#{kwargs['document_id']}"})
            return True

        with patch("apis.app_api.documents.services.cleanup_service.cleanup_document_resources",
                   side_effect=fake_cleanup) as doc_cleanup:
            result = asyncio.run(cleanup.clean_orphan(cleanup.Orphan(AGENT, rows), table, s3, BUCKET))

        doc_cleanup.assert_awaited_once()
        assert doc_cleanup.await_args.kwargs["chunk_count"] == 3
        assert result["documents"] == {"deleted": 2}
        assert result["kbTeardownQueued"] is True
        assert result["s3ObjectsDeleted"] == 1  # the icon; the document's object was its cleanup's
        assert s3.list_objects_v2(Bucket=BUCKET, Prefix=f"assistants/{AGENT}/").get("KeyCount") == 0
        # KB# stays for the teardown worker; the unknown row type is left alone.
        assert _left(table) == ["KB#" + AGENT, "WEIRD#x"]
        kb = table.get_item(Key={"PK": f"AST#{AGENT}", "SK": "KB#" + AGENT})["Item"]
        assert kb["migrationState"] == "teardown"

    def test_a_document_whose_cleanup_fails_keeps_its_row(self, aws):
        table, s3 = aws
        rows = [r for r in _seed(table, s3) if r["SK"] == "DOC#DOC-1"]
        with patch("apis.app_api.documents.services.cleanup_service.cleanup_document_resources",
                   new_callable=AsyncMock, return_value=False):
            result = asyncio.run(cleanup.clean_orphan(cleanup.Orphan(AGENT, rows), table, s3, BUCKET))
        assert result["documents"] == {"kept": 1}
        assert "DOC#DOC-1" in _left(table)

    def test_an_agent_that_exists_again_is_not_touched(self, aws):
        table, s3 = aws
        rows = _seed(table, s3)
        table.put_item(Item=_row(AGENT, "METADATA"))

        result = asyncio.run(cleanup.clean_orphan(cleanup.Orphan(AGENT, rows), table, s3, BUCKET))

        assert result == {"skipped": "the agent exists now"}
        assert len(_left(table)) == len(rows) + 1


def test_apply_requires_the_prefix_confirmation(capsys):
    assert cleanup.main(["--project-prefix", "dev-x", "--region", REGION, "--apply"]) == 2
    assert cleanup.main(["--project-prefix", "dev-x", "--region", REGION, "--apply", "--confirm-prefix", "prod"]) == 2


# ── --s3-prefixes: prefixes whose agent left no row at all ───────────────────────────
GONE = "ast-0000000000a1"


class TestS3PrefixMode:
    def test_prefixes_are_sorted_by_what_the_table_still_has(self):
        items = [
            _row("ast-live00000001", "METADATA"),
            _row("ast-rows00000001", "SHARE#someone"),
        ]
        groups = cleanup.classify_prefixes(["ast-live00000001", "ast-rows00000001", GONE], items)
        assert groups == {"live": ["ast-live00000001"], "partitionOrphan": ["ast-rows00000001"], "empty": [GONE]}

    def test_a_prefix_with_a_recent_object_is_too_young(self):
        old = cleanup.OrphanPrefix(GONE, [{"Key": f"assistants/{GONE}/icons/a.png",
                                           "LastModified": datetime(2026, 9, 1, tzinfo=timezone.utc)}])
        young = cleanup.OrphanPrefix("ast-0000000000a2", [{"Key": "assistants/ast-0000000000a2/icons/a.png",
                                                           "LastModified": datetime(2026, 9, 25, 11, tzinfo=timezone.utc)}])
        ready, too_young = cleanup.split_by_age([old, young], NOW, min_age_hours=24)
        assert ready == [old] and too_young == [young]

    def test_the_summary_counts_objects_by_folder(self):
        prefix = cleanup.OrphanPrefix(GONE, [
            {"Key": f"assistants/{GONE}/icons/a.png", "Size": 3},
            {"Key": f"assistants/{GONE}/icons/b.jpg", "Size": 4},
            {"Key": f"assistants/{GONE}/documents/DOC-1/plan.pdf", "Size": 5},
            {"Key": f"assistants/{GONE}/stray.txt", "Size": 1},
        ])
        summary = prefix.summary()
        assert summary["objects"] == {"documents": 1, "icons": 2, "other": 1}
        assert summary["bytes"] == 13

    def test_lists_agent_prefixes_and_cleans_one_keeping_referenced_keys(self, aws):
        table, s3 = aws
        referenced = f"assistants/{GONE}/icons/shared.png"
        # A publisher profile hand-pointed at the deleted agent's icon.
        table.put_item(Item={"PK": "AGENT_PUBLISHERS", "SK": "PUB#dept", "iconKey": referenced})
        table.put_item(Item=_row("ast-live00000001", "METADATA"))
        for key in (f"assistants/{GONE}/icons/a.png", f"assistants/{GONE}/icons/b.png", referenced,
                    "assistants/ast-live00000001/icons/c.png"):
            s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")

        assert sorted(cleanup.s3_agent_prefixes(s3, BUCKET)) == [GONE, "ast-live00000001"]
        _, _, names = cleanup.table_index(cleanup.scan_table(table))
        objects = cleanup.s3_prefix_objects(s3, BUCKET, GONE)
        prefix = cleanup.OrphanPrefix(GONE, objects, referenced=[o["Key"] for o in objects if o["Key"] in names])

        assert cleanup.clean_prefix(prefix, table, s3, BUCKET) == {"s3ObjectsDeleted": 2}
        left = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
        assert left == {referenced, "assistants/ast-live00000001/icons/c.png"}

    def test_a_prefix_whose_partition_has_rows_again_is_not_touched(self, aws):
        table, s3 = aws
        s3.put_object(Bucket=BUCKET, Key=f"assistants/{GONE}/icons/a.png", Body=b"x")
        objects = cleanup.s3_prefix_objects(s3, BUCKET, GONE)
        table.put_item(Item=_row(GONE, "METADATA"))

        result = cleanup.clean_prefix(cleanup.OrphanPrefix(GONE, objects), table, s3, BUCKET)

        assert result == {"skipped": "the partition has rows now"}
        assert s3.list_objects_v2(Bucket=BUCKET, Prefix=f"assistants/{GONE}/")["KeyCount"] == 1

    def test_the_mode_reports_without_deleting_unless_applied(self, aws, capsys, monkeypatch):
        table, s3 = aws
        s3.put_object(Bucket=BUCKET, Key=f"assistants/{GONE}/icons/a.png", Body=b"x")
        monkeypatch.setattr(cleanup, "configure", lambda args, account: {
            "DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE, "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": BUCKET})
        base = ["--project-prefix", "dev-x", "--region", REGION, "--s3-prefixes", "--min-age-hours", "0"]

        assert cleanup.main(base) == 0
        assert "REPORT ONLY" in capsys.readouterr().out
        assert s3.list_objects_v2(Bucket=BUCKET, Prefix=f"assistants/{GONE}/")["KeyCount"] == 1

        assert cleanup.main(base + ["--apply", "--confirm-prefix", "dev-x"]) == 0
        assert '"s3ObjectsDeleted": 1' in capsys.readouterr().out
        assert s3.list_objects_v2(Bucket=BUCKET, Prefix=f"assistants/{GONE}/")["KeyCount"] == 0
