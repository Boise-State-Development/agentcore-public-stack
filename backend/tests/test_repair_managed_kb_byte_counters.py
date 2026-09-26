"""The one-off repair of managed-KB byte counters (scripts/repair_managed_kb_byte_counters.py).

The shapes seeded here are the ones found in dev and prod before the fix, with
synthetic ids: a promoted knowledge base whose whole corpus sits in
``reservedBytes``; the same plus bytes leaked by an earlier delete; pre-fix
consumer-completed rows with no ``committedBytes``; and shapes the script must
refuse to touch.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import repair_managed_kb_byte_counters as repair  # noqa: E402

REGION = "us-east-1"
TABLE = "test-repair-rag-assistants"
AGENT = "ast-repair0001"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _kb(table, agent=AGENT, state="retain", engine="managed", **counters):
    item = {"PK": f"AST#{agent}", "SK": f"KB#{agent}", "appKbId": agent, "migrationState": state}
    if engine:
        item["retrievalEngine"] = engine
    item.update({k: Decimal(v) for k, v in counters.items()})
    table.put_item(Item=item)


def _doc(table, document_id, status, size, agent=AGENT, **extra):
    table.put_item(
        Item={
            "PK": f"AST#{agent}",
            "SK": f"DOC#{document_id}",
            "status": status,
            "sizeBytes": Decimal(size),
            **extra,
        }
    )


def _counters(table, agent=AGENT):
    kb = table.get_item(Key={"PK": f"AST#{agent}", "SK": f"KB#{agent}"})["Item"]
    return tuple(int(kb.get(k) or 0) for k in ("storedBytes", "reservedBytes", "totalBytes"))


def _row(table, document_id, agent=AGENT):
    return table.get_item(Key={"PK": f"AST#{agent}", "SK": f"DOC#{document_id}"})["Item"]


def _run(*extra):
    return repair.main(["--project-prefix", "test", "--region", REGION, "--table", TABLE, *extra])


def _apply():
    return _run("--apply", "--confirm-prefix", "test")


class TestAdoptingAStuckMigrationReservation:
    def test_the_prod_shape_moves_to_stored(self, table):
        """Every promoted prod knowledge base: storedBytes=0, the corpus reserved,
        its documents unsettled."""
        _kb(table, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 100)
        _doc(table, "DOC-2", "complete", 200)

        assert _apply() == 0

        assert _counters(table) == (300, 0, 300)
        assert _row(table, "DOC-1")["committedBytes"] == 100
        assert _row(table, "DOC-1")["byteCapSettled"] is True

    def test_bytes_leaked_by_an_earlier_delete_are_returned(self, table):
        """The dev shape: a migrated corpus plus 98 bytes a deleted document never
        gave back."""
        _kb(table, storedBytes=98, reservedBytes=300, totalBytes=398)
        _doc(table, "DOC-1", "complete", 300)

        _apply()

        assert _counters(table) == (300, 0, 300)

    def test_unsettled_failed_rows_are_neither_adopted_nor_counted(self, table):
        _kb(table, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 300)
        _doc(table, "DOC-legacyfail", "failed", 9_999)

        _apply()

        assert _counters(table) == (300, 0, 300)
        assert "byteCapSettled" not in _row(table, "DOC-legacyfail")


class TestBackfill:
    def test_a_pre_fix_consumer_completed_row_gets_committed_bytes(self, table):
        _kb(table, storedBytes=150, reservedBytes=0, totalBytes=150)
        _doc(table, "DOC-1", "complete", 150, byteCapSettled=True, retrievableAt="2026-09-20T00:00:00Z")

        _apply()

        assert _row(table, "DOC-1")["committedBytes"] == 150
        assert _counters(table) == (150, 0, 150)

    def test_an_already_committed_row_is_not_rewritten(self, table):
        _kb(table, storedBytes=150, reservedBytes=0, totalBytes=150)
        _doc(table, "DOC-1", "complete", 999, byteCapSettled=True, committedBytes=150)

        _apply()

        assert _row(table, "DOC-1")["committedBytes"] == 150
        assert _counters(table) == (150, 0, 150)


class TestShapesItRefusesToTouch:
    def test_a_reservation_the_documents_do_not_explain(self, table):
        _kb(table, storedBytes=0, reservedBytes=500, totalBytes=500)
        _doc(table, "DOC-1", "complete", 300)

        _apply()

        assert _counters(table) == (0, 500, 500)
        assert "byteCapSettled" not in _row(table, "DOC-1")

    def test_an_upload_in_flight(self, table):
        _kb(table, storedBytes=0, reservedBytes=340, totalBytes=340)
        _doc(table, "DOC-1", "complete", 300)
        _doc(table, "DOC-up", "uploading", 40)

        _apply()

        assert _counters(table) == (0, 340, 340)

    def test_a_leaked_reservation_is_reported_not_zeroed(self, table):
        """A reservation with no upload behind it could also be one whose DOC# row
        is milliseconds from being written; zeroing it could race that upload."""
        _kb(table, storedBytes=0, reservedBytes=42, totalBytes=42)
        _doc(table, "DOC-fail", "failed", 42, byteCapSettled=True)

        plan = repair.plan_kb(AGENT, table.get_item(Key={"PK": f"AST#{AGENT}", "SK": f"KB#{AGENT}"})["Item"],
                              repair.document_rows(table, AGENT))
        _apply()

        assert not plan.has_work
        assert any("leaked reservation" in note for note in plan.notes)
        assert _counters(table) == (0, 42, 42)

    def test_a_legacy_row_stuck_in_chunking_does_not_block_adoption(self, table):
        """``chunking`` is a legacy-pipeline status; it never reserved on the managed
        engine. Dev has exactly this: a migrated corpus plus one stuck legacy row."""
        _kb(table, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 300)
        _doc(table, "DOC-stuck", "chunking", 6_000)

        _apply()

        assert _counters(table) == (300, 0, 300)

    def test_a_record_the_worker_is_part_way_through(self, table):
        _kb(table, state="shadow", engine=None, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 300)

        _apply()

        assert _counters(table) == (0, 300, 300)


class TestSafety:
    def test_report_only_by_default(self, table):
        _kb(table, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 300)

        assert _run() == 0

        assert _counters(table) == (0, 300, 300)
        assert "byteCapSettled" not in _row(table, "DOC-1")

    def test_apply_requires_the_prefix_confirmed(self, table):
        _kb(table, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 300)

        assert _run("--apply", "--confirm-prefix", "wrong") == 2

        assert _counters(table) == (0, 300, 300)

    def test_a_second_run_changes_nothing(self, table):
        _kb(table, storedBytes=98, reservedBytes=300, totalBytes=398)
        _doc(table, "DOC-1", "complete", 300)

        _apply()
        _apply()

        assert _counters(table) == (300, 0, 300)

    def test_the_re_anchor_refuses_when_the_counters_moved(self, table):
        """A delete or upload between the read and the write must win."""
        _kb(table, storedBytes=98, reservedBytes=0, totalBytes=98)
        _doc(table, "DOC-1", "complete", 50, byteCapSettled=True, committedBytes=50)
        plan = repair.plan_kb(AGENT, table.get_item(Key={"PK": f"AST#{AGENT}", "SK": f"KB#{AGENT}"})["Item"],
                              repair.document_rows(table, AGENT))
        assert plan.reanchor
        table.update_item(
            Key={"PK": f"AST#{AGENT}", "SK": f"KB#{AGENT}"},
            UpdateExpression="ADD reservedBytes :n, totalBytes :n",
            ExpressionAttributeValues={":n": Decimal(10)},
        )

        result = repair.apply_plan(table, plan)

        assert result["reanchor"].startswith("refused")
        assert _counters(table) == (98, 10, 108)
