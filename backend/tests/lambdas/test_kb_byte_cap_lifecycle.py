"""A managed document's bytes, end to end: reserve, commit, delete, refund.

Feature: managed-kb-migration Requirements 12.4–12.6, 12.9.

Each settlement path was tested on its own, and each was right on its own. The
defects were in the seams between them, which is why these tests drive the real
functions in sequence against one moto table:

* **A completed document's bytes never came back on delete.** Verified in dev: a
  98-byte document uploaded, completed and deleted left ``storedBytes`` and
  ``totalBytes`` 98 higher for good. The delete path only released reservations,
  and a completed document no longer has one.
* **A migrated corpus was never committed.** The snapshot reservation taken in
  ``shadow`` sat in ``reservedBytes`` for ever: in prod every migrated knowledge
  base had ``storedBytes=0`` and its whole corpus reserved.
* **Deleting a ``failed`` document released bytes it never reserved.** Prod has
  unsettled ``failed`` rows from before their knowledge base migrated, with
  multi-megabyte ``sizeBytes``; each delete credited that much allowance back.
* **An import was committed out of a reservation it never made.** A file-source
  import writes its real ``sizeBytes`` before its ``PUT`` but reserves nothing, and
  the consumer read that size as the reservation. In prod two Drive imports left a
  migrated knowledge base's ``reservedBytes`` and ``totalBytes`` short by exactly
  their 499,835 bytes, so they never counted against the cap.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.documents.services.document_service import soft_delete_document
from apis.app_api.kb_migration import ingestion_consumer as ic
from apis.app_api.kb_migration import worker
from apis.shared.kb_backend import byte_cap
from apis.shared.kb_backend import records as r

REGION = "us-east-1"
TABLE = "test-byte-cap-lifecycle"
BUCKET = "test-byte-cap-docs"
ASSISTANT_ID = "ast-bytes0001"
OWNER = "user-bytes0001"
CAP = 1_000_000


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", BUCKET)

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
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def owned():
    """``soft_delete_document`` checks ownership through the assistants service,
    which this suite is not about."""
    with patch(
        "apis.shared.assistants.service.get_assistant",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(assistant_id=ASSISTANT_ID, owner_id=OWNER),
    ):
        yield


# ── helpers ──────────────────────────────────────────────────────────────────
def _seed_kb(table, engine="managed", **fields):
    item = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"KB#{ASSISTANT_ID}",
        "appKbId": ASSISTANT_ID,
        "ownerUserId": OWNER,
    }
    if engine:
        item["retrievalEngine"] = engine
    item.update({k: Decimal(v) if isinstance(v, int) else v for k, v in fields.items()})
    table.put_item(Item=item)


def _seed_doc(table, document_id, status="uploading", size=0, **extra):
    table.put_item(
        Item={
            "PK": f"AST#{ASSISTANT_ID}",
            "SK": f"DOC#{document_id}",
            "documentId": document_id,
            "assistantId": ASSISTANT_ID,
            "filename": f"{document_id}.txt",
            "contentType": "text/plain",
            "sizeBytes": Decimal(size),
            "s3Key": _key(document_id),
            "status": status,
            "createdAt": "2026-09-25T00:00:00Z",
            "updatedAt": "2026-09-25T00:00:00Z",
            **extra,
        }
    )


def _key(document_id):
    return f"assistants/{ASSISTANT_ID}/documents/{document_id}/{document_id}.txt"


def _put_object(document_id, size):
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key=_key(document_id), Body=b"x" * size
    )


def _kb(table):
    return table.get_item(Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}"}).get("Item")


def _doc(table, document_id):
    return table.get_item(Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{document_id}"}).get("Item")


def _counters(table):
    kb = _kb(table) or {}
    return tuple(int(kb.get(k) or 0) for k in ("storedBytes", "reservedBytes", "totalBytes"))


def _upload_and_complete(table, document_id, declared, real):
    """The interactive path: reserve the declared size at request time, then the
    ingestion consumer settles against the real S3 size."""
    _seed_doc(table, document_id, size=declared)
    byte_cap.reserve(ASSISTANT_ID, ASSISTANT_ID, declared, CAP)
    _put_object(document_id, real)
    ic._reconcile_bytes_on_complete(
        ASSISTANT_ID, document_id, BUCKET, _key(document_id), _kb(table), declared
    )
    ic.set_document_terminal(ASSISTANT_ID, document_id, ic.STATUS_COMPLETE)


def _import_and_complete(table, document_id, size, adapter="google-drive"):
    """A file-source import (or crawl, or sync): nothing reserved at request time,
    and ``sizeBytes`` already real by the time the consumer reads the row."""
    _seed_doc(table, document_id, size=size, sourceAdapterKey=adapter)
    _put_object(document_id, size)
    ic._reconcile_bytes_on_complete(
        ASSISTANT_ID, document_id, BUCKET, _key(document_id), _kb(table),
        ic._declared_bytes(_doc(table, document_id)),
    )
    ic.set_document_terminal(ASSISTANT_ID, document_id, ic.STATUS_COMPLETE)


def _delete(document_id):
    return asyncio.run(soft_delete_document(ASSISTANT_ID, document_id, OWNER))


# ── Deleting a completed document ────────────────────────────────────────────
class TestDeletingACompletedDocumentRefundsIt:
    def test_upload_complete_delete_returns_every_byte(self, table, owned):
        """The dev repro. MUTATION GUARD: drop the refund from
        ``settle_bytes_on_delete`` and this ends at (98, 0, 98) — 98 bytes of
        allowance gone for a document that no longer exists."""
        _seed_kb(table)
        _upload_and_complete(table, "DOC-98", declared=98, real=98)
        assert _counters(table) == (98, 0, 98)

        _delete("DOC-98")

        assert _counters(table) == (0, 0, 0)

    def test_the_refund_is_the_committed_size_not_the_declared_one(self, table, owned):
        """The client over-declared: 4096 reserved, 2048 really stored. Only the
        2048 that were committed may come back, or the delete credits the owner
        2048 bytes they never used."""
        _seed_kb(table)
        _upload_and_complete(table, "DOC-over", declared=4096, real=2048)
        assert _doc(table, "DOC-over")["committedBytes"] == 2048

        _delete("DOC-over")

        assert _counters(table) == (0, 0, 0)

    def test_the_refund_is_exactly_once(self, table, owned):
        """Re-deleting is idempotent by design; a second refund would let an owner
        exceed their cap by deleting the same document twice."""
        _seed_kb(table)
        _upload_and_complete(table, "DOC-a", declared=100, real=100)
        _upload_and_complete(table, "DOC-b", declared=50, real=50)

        _delete("DOC-a")
        _delete("DOC-a")

        assert _counters(table) == (50, 0, 50)
        assert _doc(table, "DOC-a")["byteCapRefunded"] is True

    def test_a_delete_during_ingestion_releases_instead_of_committing(self, table, owned):
        """The race the stamp-before-commit order exists for. The consumer has won
        ``settle_once`` and is measuring the object when the owner deletes. The
        delete finds nothing committed to refund and no reservation left to claim,
        so if the consumer then committed, those bytes would be stored for ever.
        The stamp is refused on the ``deleting`` row and the consumer releases."""
        _seed_kb(table)
        _seed_doc(table, "DOC-race", size=300)
        byte_cap.reserve(ASSISTANT_ID, ASSISTANT_ID, 300, CAP)
        _put_object("DOC-race", 300)

        real_head = byte_cap.object_size_bytes

        def _head_then_delete(bucket, key):
            size = real_head(bucket, key)
            _delete("DOC-race")
            return size

        with patch.object(byte_cap, "object_size_bytes", side_effect=_head_then_delete):
            ic._reconcile_bytes_on_complete(
                ASSISTANT_ID, "DOC-race", BUCKET, _key("DOC-race"), _kb(table), 300
            )

        assert _counters(table) == (0, 0, 0)
        assert "committedBytes" not in _doc(table, "DOC-race")

    def test_deleting_an_in_flight_upload_still_releases_its_reservation(self, table, owned):
        """The path that already worked must keep working."""
        _seed_kb(table)
        _seed_doc(table, "DOC-flight", size=700)
        byte_cap.reserve(ASSISTANT_ID, ASSISTANT_ID, 700, CAP)

        _delete("DOC-flight")

        assert _counters(table) == (0, 0, 0)


class TestDeletingAFailedDocumentReleasesNothing:
    def test_an_unsettled_failed_row_never_reserved(self, table, owned):
        """Every managed failure path settles before it writes ``failed``, so an
        unsettled ``failed`` row was written while the knowledge base was legacy.
        MUTATION GUARD: without the status check this releases 3000 bytes out of
        another document's reservation and the counters end at (0, 2000, 2000)."""
        _seed_kb(table, storedBytes=0, reservedBytes=5000, totalBytes=5000)
        _seed_doc(table, "DOC-migrated", status="complete", size=5000)
        _seed_doc(table, "DOC-legacyfail", status="failed", size=3000)

        _delete("DOC-legacyfail")

        assert _counters(table) == (0, 5000, 5000)


class TestImportsReserveAtIngestion:
    def test_an_import_is_counted_against_the_cap(self, table):
        """MUTATION GUARD: read ``sizeBytes`` as the reservation again and this ends
        at (500, -500, 0) — stored, but never counted in ``totalBytes``."""
        _seed_kb(table)

        _import_and_complete(table, "DOC-imp", 500)

        assert _counters(table) == (500, 0, 500)
        assert _doc(table, "DOC-imp")["committedBytes"] == 500

    def test_the_prod_shape_does_not_recur(self, table):
        """A migrated corpus still held as a reservation, then two imports. Before
        the fix this ended at (499835, 1664866, 2164701)."""
        corpus = 2_164_701
        _seed_kb(table, storedBytes=0, reservedBytes=corpus, totalBytes=corpus)
        _seed_doc(table, "DOC-migrated", status="complete", size=corpus)

        _import_and_complete(table, "DOC-imp1", 140_270)
        _import_and_complete(table, "DOC-imp2", 359_565)

        assert _counters(table) == (499_835, corpus, corpus + 499_835)

    def test_a_crawled_page_reserves_whether_or_not_its_size_landed_first(self, table):
        """The crawler writes ``sizeBytes`` after its ``PUT``, so the consumer can
        read either 0 or the real size. Both must end the same way."""
        _seed_kb(table)

        _import_and_complete(table, "DOC-page", 300, adapter="http")

        assert _counters(table) == (300, 0, 300)

    def test_an_import_over_the_cap_fails_instead_of_slipping_past_it(self, table):
        cap = byte_cap.effective_cap()
        _seed_kb(table, storedBytes=cap - 100, reservedBytes=0, totalBytes=cap - 100)
        _seed_doc(table, "DOC-big", size=500, sourceAdapterKey="google-drive")
        _put_object("DOC-big", 500)

        with pytest.raises(byte_cap.ByteCapExceeded):
            ic._reconcile_bytes_on_complete(
                ASSISTANT_ID, "DOC-big", BUCKET, _key("DOC-big"), _kb(table),
                ic._declared_bytes(_doc(table, "DOC-big")),
            )

        assert _counters(table) == (cap - 100, 0, cap - 100)

    def test_an_upload_still_reserves_its_declared_size(self, table):
        assert ic._declared_bytes({"sizeBytes": Decimal(700)}) == 700
        assert ic._declared_bytes({"sizeBytes": Decimal(700), "sourceAdapterKey": "google-drive"}) == 0
        assert ic._declared_bytes(None) == 0

    def test_deleting_an_import_in_flight_releases_nothing(self, table, owned):
        """MUTATION GUARD: without the provenance check the delete releases 400
        bytes out of the other document's reservation: (0, 300, 300)."""
        _seed_kb(table)
        _seed_doc(table, "DOC-upload", size=700)
        byte_cap.reserve(ASSISTANT_ID, ASSISTANT_ID, 700, CAP)
        _seed_doc(table, "DOC-imp", size=400, sourceAdapterKey="google-drive")

        _delete("DOC-imp")

        assert _counters(table) == (0, 700, 700)


# ── Guards ───────────────────────────────────────────────────────────────────
class TestCountersNeverGoNegative:
    def test_a_release_larger_than_the_reservations_is_refused(self, table):
        _seed_kb(table, storedBytes=10, reservedBytes=100, totalBytes=110)

        with patch.object(byte_cap, "emit_count") as emit:
            assert byte_cap.release(ASSISTANT_ID, ASSISTANT_ID, 101) is False

        assert _counters(table) == (10, 100, 110)
        emit.assert_called_once_with(byte_cap.METRIC_BYTE_CAP_SKEW)

    def test_a_refund_larger_than_the_stored_bytes_is_refused(self, table):
        _seed_kb(table, storedBytes=10, reservedBytes=100, totalBytes=110)

        assert byte_cap.refund(ASSISTANT_ID, ASSISTANT_ID, 11) is False
        assert _counters(table) == (10, 100, 110)

    def test_a_record_that_never_counted_anything_refuses_too(self, table):
        _seed_kb(table)

        assert byte_cap.refund(ASSISTANT_ID, ASSISTANT_ID, 1) is False
        assert byte_cap.release(ASSISTANT_ID, ASSISTANT_ID, 1) is False
        assert _kb(table).get("totalBytes") is None

    def test_settling_against_a_torn_down_record_does_not_recreate_it(self, table):
        assert byte_cap.release(ASSISTANT_ID, ASSISTANT_ID, 5) is False
        assert byte_cap.refund(ASSISTANT_ID, ASSISTANT_ID, 5) is False
        assert _kb(table) is None


class TestDocumentMarkersNeverCreateRows:
    """``UpdateItem`` is an upsert; each marker write is guarded on the row."""

    def test_record_commit_on_a_gone_row(self, table):
        assert byte_cap.record_commit(ASSISTANT_ID, "DOC-gone", 5) is False
        assert _doc(table, "DOC-gone") is None

    def test_record_commit_on_a_deleting_row(self, table):
        _seed_doc(table, "DOC-del", status="deleting", size=5)

        assert byte_cap.record_commit(ASSISTANT_ID, "DOC-del", 5) is False
        assert "committedBytes" not in _doc(table, "DOC-del")

    def test_refund_once_on_a_gone_row(self, table):
        assert byte_cap.refund_once(ASSISTANT_ID, "DOC-gone") == 0
        assert _doc(table, "DOC-gone") is None

    def test_refund_once_on_an_uncommitted_row(self, table):
        """An in-flight upload has a reservation, not stored bytes."""
        _seed_doc(table, "DOC-up", status="uploading", size=5)

        assert byte_cap.refund_once(ASSISTANT_ID, "DOC-up") == 0
        assert "byteCapRefunded" not in _doc(table, "DOC-up")

    def test_settle_as_committed_only_claims_an_unsettled_complete_row(self, table):
        _seed_doc(table, "DOC-c", status="complete", size=5)
        _seed_doc(table, "DOC-u", status="uploading", size=5)

        assert byte_cap.settle_as_committed(ASSISTANT_ID, "DOC-c", 5) is True
        assert byte_cap.settle_as_committed(ASSISTANT_ID, "DOC-c", 5) is False
        assert byte_cap.settle_as_committed(ASSISTANT_ID, "DOC-u", 5) is False
        assert byte_cap.settle_as_committed(ASSISTANT_ID, "DOC-gone", 5) is False
        assert _doc(table, "DOC-gone") is None


# ── Migration ────────────────────────────────────────────────────────────────
def _promote(table):
    """``run_promote`` with its state writes stubbed: this is about the counters."""
    record = _kb(table)
    with patch("apis.shared.kb_backend.records.promote_engine"), patch(
        "apis.shared.kb_backend.records.set_migration_state"
    ), patch.object(worker, "_set_retain_until"), patch(
        "apis.shared.kb_backend.metrics.emit_count"
    ):
        return asyncio.run(worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, record))


def _shadow_reserve(table, sizes):
    """What ``run_shadow`` does to the counters: the corpus as one reservation."""
    for document_id, size in sizes.items():
        _seed_doc(table, document_id, status="complete", size=size)
    byte_cap.reserve_snapshot(ASSISTANT_ID, ASSISTANT_ID, sum(sizes.values()), CAP)


class TestPromotionAdoptsTheCorpus:
    def test_the_corpus_moves_from_reserved_to_stored(self, table):
        """MUTATION GUARD: remove the ``adopt_corpus`` call from ``run_promote`` and
        this ends at (0, 600, 600) — the prod shape, where every migrated knowledge
        base carried its corpus as a reservation nothing ever settled."""
        _seed_kb(table, engine=None, migrationState=r.PROMOTE)
        _shadow_reserve(table, {"DOC-1": 100, "DOC-2": 200, "DOC-3": 300})
        assert _counters(table) == (0, 600, 600)

        _promote(table)

        assert _counters(table) == (600, 0, 600)
        assert "snapshotReservedBytes" not in _kb(table)
        for document_id, size in (("DOC-1", 100), ("DOC-2", 200), ("DOC-3", 300)):
            doc = _doc(table, document_id)
            assert doc["byteCapSettled"] is True
            assert doc["committedBytes"] == size

    def test_drift_during_shadow_is_settled_against_the_corpus_as_it_is(self, table):
        """A document deleted during shadow (a legacy delete touches no counters)
        and one added by catch-up (never reserved). Settling the snapshot as one
        reserved -> stored move would count the deleted one and miss the new one."""
        _seed_kb(table, engine=None, migrationState=r.PROMOTE)
        _shadow_reserve(table, {"DOC-1": 100, "DOC-2": 200})
        table.delete_item(Key={"PK": f"AST#{ASSISTANT_ID}", "SK": "DOC#DOC-2"})
        _seed_doc(table, "DOC-late", status="complete", size=50)

        _promote(table)

        assert _counters(table) == (150, 0, 150)

    def test_a_resumed_promotion_counts_nothing_twice(self, table):
        _seed_kb(table, engine=None, migrationState=r.PROMOTE)
        _shadow_reserve(table, {"DOC-1": 100, "DOC-2": 200})
        stale_record = _kb(table)

        worker.adopt_corpus(ASSISTANT_ID, ASSISTANT_ID, stale_record)
        worker.adopt_corpus(ASSISTANT_ID, ASSISTANT_ID, stale_record)

        assert _counters(table) == (300, 0, 300)

    def test_a_migrated_document_deleted_after_promotion_is_refunded(self, table, owned):
        _seed_kb(table, engine=None, migrationState=r.PROMOTE)
        _shadow_reserve(table, {"DOC-1": 100, "DOC-2": 200})
        _promote(table)
        table.update_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}"},
            UpdateExpression="SET retrievalEngine = :m",
            ExpressionAttributeValues={":m": "managed"},
        )

        _delete("DOC-2")

        assert _counters(table) == (100, 0, 100)

    def test_in_flight_uploads_during_adoption_keep_their_reservation(self, table):
        """Adoption returns only the snapshot's amount, never ``reservedBytes`` as a
        whole, so an upload reserved alongside it is untouched."""
        _seed_kb(table, engine=None, migrationState=r.PROMOTE)
        _shadow_reserve(table, {"DOC-1": 100})
        _seed_doc(table, "DOC-up", status="uploading", size=40)
        byte_cap.reserve(ASSISTANT_ID, ASSISTANT_ID, 40, CAP)

        _promote(table)

        assert _counters(table) == (100, 40, 140)
