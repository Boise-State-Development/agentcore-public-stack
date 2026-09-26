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


# ── --settle-unexplained ─────────────────────────────────────────────────────
#: The production knowledge base this mode was written for, with synthetic ids.
#: A three-document corpus migrated under the old code (reserved as a snapshot,
#: never settled), then two file-source imports the old consumer committed out of
#: reservedBytes they never added to. reservedBytes is short by exactly the
#: imports, which is why the adopt rule refused it.
CORPUS = {"DOC-mig1": 7_759, "DOC-mig2": 144_907, "DOC-mig3": 2_012_035}
IMPORTS = {"DOC-imp1": 140_270, "DOC-imp2": 359_565}
PROD_COUNTERS = (499_835, 1_664_866, 2_164_701)
TRUE_CORPUS = 2_664_536


def _seed_prod_shape(table):
    stored, reserved, total = PROD_COUNTERS
    _kb(table, storedBytes=stored, reservedBytes=reserved, totalBytes=total)
    for document_id, size in CORPUS.items():
        _doc(table, document_id, "complete", size, sourceAdapterKey="google-drive")
    for document_id, size in IMPORTS.items():
        _doc(
            table, document_id, "complete", size,
            sourceAdapterKey="google-drive", byteCapSettled=True, committedBytes=Decimal(size),
            retrievableAt="2026-09-26T01:00:00Z",
        )


def _settle(*extra):
    return _run("--settle-unexplained", "--agent", AGENT, "--quiet-seconds", "0", *extra)


def _plan(table, settle=True):
    record = table.get_item(Key={"PK": f"AST#{AGENT}", "SK": f"KB#{AGENT}"})["Item"]
    return repair.plan_kb(AGENT, record, repair.document_rows(table, AGENT), settle_unexplained=settle)


class TestTheProdShape:
    def test_the_counters_add_up_to_what_the_report_saw(self):
        stored, reserved, total = PROD_COUNTERS
        assert stored == sum(IMPORTS.values())
        assert reserved == sum(CORPUS.values()) - sum(IMPORTS.values())
        assert total == stored + reserved == sum(CORPUS.values())
        assert TRUE_CORPUS == sum(CORPUS.values()) + sum(IMPORTS.values())

    def test_without_the_flag_it_is_left_for_review_with_the_diagnosis(self, table):
        _seed_prod_shape(table)

        plan = _plan(table, settle=False)
        assert _apply() == 0

        assert not plan.has_work
        assert any("left for review" in note for note in plan.notes)
        assert any("explained exactly by 2 imported" in note and "499835 bytes" in note for note in plan.notes)
        assert _counters(table) == PROD_COUNTERS
        assert "byteCapSettled" not in _row(table, "DOC-mig1")

    def test_settling_it_counts_the_whole_corpus_once(self, table):
        _seed_prod_shape(table)

        assert _settle("--apply", "--confirm-prefix", "test") == 0

        assert _counters(table) == (TRUE_CORPUS, 0, TRUE_CORPUS)
        for document_id, size in CORPUS.items():
            assert _row(table, document_id)["byteCapSettled"] is True
            assert _row(table, document_id)["committedBytes"] == size
        for document_id, size in IMPORTS.items():
            assert _row(table, document_id)["committedBytes"] == size

    def test_a_second_run_finds_nothing_to_do(self, table):
        _seed_prod_shape(table)
        _settle("--apply", "--confirm-prefix", "test")

        plan = _plan(table)
        _settle("--apply", "--confirm-prefix", "test")
        _apply()

        assert not plan.has_work
        assert plan.notes == []
        assert _counters(table) == (TRUE_CORPUS, 0, TRUE_CORPUS)

    def test_the_reconciler_agrees_with_the_settled_state(self, table):
        """The reconciler anchors storedBytes to S3 over the ledger's own members
        (committedBytes set, not refunded). After the settle that is all five
        documents, so its next pass is a no-op rather than a second correction."""
        from apis.app_api.kb_migration import reconciler

        _seed_prod_shape(table)
        before = reconciler.counted_document_ids(AGENT, table=table)
        _settle("--apply", "--confirm-prefix", "test")
        after = reconciler.counted_document_ids(AGENT, table=table)

        assert before == set(IMPORTS)
        assert after == set(IMPORTS) | set(CORPUS)
        assert sum({**CORPUS, **IMPORTS}[d] for d in after) == _counters(table)[0]

    def test_a_reconciler_pass_that_changes_nothing_does_not_block_it(self, table):
        """Today the reconciler's anchor for this shape is the two imports (its
        ledger members), which is what storedBytes already holds, and totalBytes is
        already stored + reserved. So a pass between the read and the write writes
        the same numbers and the settle still lands."""
        from apis.app_api.kb_migration import reconciler

        _seed_prod_shape(table)
        plan = _plan(table)
        assert reconciler.refresh_stored_bytes(AGENT, AGENT, sum(IMPORTS.values()))

        result = repair.apply_plan(table, plan, quiet_seconds=0)

        assert result["settle"] == "done"
        assert _counters(table) == (TRUE_CORPUS, 0, TRUE_CORPUS)

    def test_a_reconciler_pass_that_moves_a_counter_cancels_all_of_it(self, table):
        """All or nothing: the counter condition fails, so none of the three
        claims lands either, and a re-run starts from a clean read."""
        from apis.app_api.kb_migration import reconciler

        _seed_prod_shape(table)
        plan = _plan(table)
        assert reconciler.refresh_stored_bytes(AGENT, AGENT, 500_000)

        result = repair.apply_plan(table, plan, quiet_seconds=0)

        assert result["settle"].startswith("refused")
        assert _counters(table) == (500_000, 1_664_866, 2_164_866)
        for document_id in CORPUS:
            assert "byteCapSettled" not in _row(table, document_id)


class TestSettleRefusals:
    def test_an_upload_in_flight(self, table):
        _seed_prod_shape(table)
        _doc(table, "DOC-up", "uploading", 40)

        plan = _plan(table)
        _settle("--apply", "--confirm-prefix", "test")

        assert not plan.settle
        assert any("in flight" in note for note in plan.notes)
        assert _counters(table) == PROD_COUNTERS

    def test_a_delete_between_deleting_and_its_refund(self, table):
        """Its bytes are still in storedBytes, but not in a complete-only ledger;
        settling now and refunding after would take them off twice."""
        _seed_prod_shape(table)
        _doc(table, "DOC-going", "deleting", 10, byteCapSettled=True, committedBytes=Decimal(10))

        plan = _plan(table)

        assert not plan.settle
        assert any("part-way through" in note and "DOC-going" in note for note in plan.notes)

    def test_a_refunded_delete_does_not_block_it(self, table):
        _seed_prod_shape(table)
        _doc(table, "DOC-gone", "deleting", 10, byteCapSettled=True, committedBytes=Decimal(10),
             byteCapRefunded=True)

        assert _plan(table).settle

    def test_a_document_that_changes_in_the_quiet_window(self, table):
        """An upload reserves before its DOC# row exists, so a reservation can be
        read with no row behind it yet. The re-read after the quiet window is what
        catches the row arriving."""
        _seed_prod_shape(table)
        plan = _plan(table)
        _doc(table, "DOC-late", "uploading", 40)

        result = repair.apply_plan(table, plan, quiet_seconds=0)

        assert result["settle"].startswith("refused: the documents changed")
        assert _counters(table) == PROD_COUNTERS

    def test_a_claimed_document_deleted_at_the_last_moment_cancels_all_of_it(self, table):
        _seed_prod_shape(table)
        plan = _plan(table)
        table.update_item(
            Key={"PK": f"AST#{AGENT}", "SK": "DOC#DOC-mig2"},
            UpdateExpression="SET #s = :d",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "deleting"},
        )

        with pytest.MonkeyPatch.context() as mp:
            # Past the quiet-window re-read, so the transaction's own row condition
            # is what has to catch it.
            mp.setattr(repair, "plan_kb", lambda *a, **k: plan)
            result = repair.apply_settle(table, plan, quiet_seconds=0)

        assert result["settle"].startswith("refused")
        assert _counters(table) == PROD_COUNTERS
        assert "byteCapSettled" not in _row(table, "DOC-mig1")

    def test_it_requires_naming_the_knowledge_base(self, table):
        _seed_prod_shape(table)

        assert _run("--settle-unexplained", "--apply", "--confirm-prefix", "test") == 2

        assert _counters(table) == PROD_COUNTERS

    def test_it_previews_without_apply(self, table):
        _seed_prod_shape(table)

        assert _settle() == 0

        assert _counters(table) == PROD_COUNTERS
        assert "byteCapSettled" not in _row(table, "DOC-mig1")

    def test_a_knowledge_base_not_named_is_not_touched(self, table):
        _seed_prod_shape(table)
        other = "ast-repair0002"
        _kb(table, agent=other, storedBytes=0, reservedBytes=42, totalBytes=42)

        _settle("--apply", "--confirm-prefix", "test")

        assert _counters(table, agent=other) == (0, 42, 42)

    def test_a_consistent_knowledge_base_is_left_alone(self, table):
        """The flag only widens what counts as repairable; a knowledge base the
        ordinary rules handle is handled by them."""
        _kb(table, storedBytes=0, reservedBytes=300, totalBytes=300)
        _doc(table, "DOC-1", "complete", 300)

        plan = _plan(table)

        assert not plan.settle
        assert plan.adopt == [("DOC-1", 300)]


class TestOtherShapesTheSettleRepairs:
    def test_imports_on_a_knowledge_base_with_nothing_reserved_went_negative(self, table):
        """A born-managed or already-adopted knowledge base: each pre-fix import
        committed out of an empty reservedBytes."""
        _kb(table, storedBytes=500, reservedBytes=-500, totalBytes=0)
        _doc(table, "DOC-imp", "complete", 500, sourceAdapterKey="google-drive",
             byteCapSettled=True, committedBytes=Decimal(500))

        plan = _plan(table)
        _settle("--apply", "--confirm-prefix", "test")

        assert any("explained exactly by 1 imported" in note for note in plan.notes)
        assert _counters(table) == (500, 0, 500)

    def test_a_leaked_reservation_on_a_quiet_knowledge_base(self, table):
        """The dev shape the ordinary run reports and leaves: 42 bytes reserved,
        nothing in flight. The quiet-window re-read is what makes zeroing it safe."""
        _kb(table, storedBytes=0, reservedBytes=42, totalBytes=42)
        _doc(table, "DOC-fail", "failed", 42, byteCapSettled=True)

        _settle("--apply", "--confirm-prefix", "test")

        assert _counters(table) == (0, 0, 0)

    def test_a_pre_fix_row_without_committed_bytes_is_backfilled_in_the_same_transaction(self, table):
        _kb(table, storedBytes=150, reservedBytes=100, totalBytes=200)
        _doc(table, "DOC-mig", "complete", 250)
        _doc(table, "DOC-imp", "complete", 150, sourceAdapterKey="http", byteCapSettled=True,
             retrievableAt="2026-09-20T00:00:00Z")

        _settle("--apply", "--confirm-prefix", "test")

        assert _counters(table) == (400, 0, 400)
        assert _row(table, "DOC-imp")["committedBytes"] == 150
        assert _row(table, "DOC-mig")["committedBytes"] == 250
