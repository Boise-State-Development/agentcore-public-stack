"""Routing exclusivity for the managed-KB ingestion consumer.

Feature: managed-kb-migration, task 9.2.

The failure this file exists to prevent is **double indexing**. The legacy pipeline
is driven by its own pre-existing S3 notification on the same bucket, so for a legacy
document the correct behaviour of this consumer is to do nothing whatsoever. If it
ingested as well, the same bytes would be embedded twice: two sets of vectors,
doubled ingestion cost, and duplicate chunks competing inside one result list. None
of that raises an error, which is exactly why it needs a test.

The routing is therefore deliberately asymmetric, and both halves are asserted:
legacy must ingest NOTHING here, managed must ingest here and NOT fall back.
"""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.kb_migration import ingestion_consumer as ic

REGION = "us-east-1"
TABLE = "test-ingestion-consumer"
ASSISTANT_ID = "ast-ing01"
DOCUMENT_ID = "doc-ing01"
BUCKET = "docs-bucket"
KEY = f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/report.pdf"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
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
        t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        t.put_item(
            Item={
                "PK": f"AST#{ASSISTANT_ID}",
                "SK": f"DOC#{DOCUMENT_ID}",
                "status": "uploading",
            }
        )
        yield t


def _seed_kb(table, **overrides):
    """A KB_Record for this assistant. No retrievalEngine unless asked."""
    item = {"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}", "appKbId": ASSISTANT_ID}
    item.update(overrides)
    table.put_item(Item=item)


def _doc(table):
    return table.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{DOCUMENT_ID}"}
    )["Item"]


def _eventbridge_event(key=KEY):
    return {"detail": {"bucket": {"name": BUCKET}, "object": {"key": key}}}


class _FakeBackend:
    """Records ingest calls; reports the document retrievable immediately."""

    def __init__(self):
        self.ingested = []

    async def ingest(self, kb_ref, source):
        self.ingested.append(source.document_id)
        return None

    async def search(self, kb_ref, query, top_k=5):
        chunk = MagicMock()
        chunk.metadata = {"document_id": DOCUMENT_ID}
        return [chunk]


# ---------------------------------------------------------------------------
# Legacy must not be touched
# ---------------------------------------------------------------------------
class TestLegacyRouting:
    def test_a_legacy_document_is_not_ingested_here(self, table):
        """No retrievalEngine means legacy, and legacy is somebody else's job."""
        _seed_kb(table)
        fake = _FakeBackend()

        with patch("apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake):
            result = ic.handle_object(BUCKET, KEY)

        assert result["routed"] == "legacy"
        assert result["ingested"] is False
        assert fake.ingested == [], "a legacy document was ingested into the managed backend"

    def test_a_document_with_no_kb_record_at_all_is_legacy(self, table):
        """The overwhelmingly common case today: no record has ever been written."""
        result = ic.handle_object(BUCKET, KEY)
        assert result["routed"] == "legacy"
        assert result["ingested"] is False

    def test_a_legacy_document_status_is_left_alone(self, table):
        """The legacy pipeline owns the terminal transition for its documents.

        Writing `complete` here would race the other Lambda and could mark a
        document ready before its vectors exist.
        """
        _seed_kb(table)
        ic.handle_object(BUCKET, KEY)
        assert _doc(table)["status"] == "uploading"

    @pytest.mark.parametrize("engine", ["s3vectors", "S3Vectors", "MANAGED", "managed ", "", "wat"])
    def test_only_the_exact_managed_literal_routes_to_managed(self, table, engine):
        """Exact-match, so a casing slip fails safe.

        Failing safe matters asymmetrically: routing to legacy when it should be
        managed leaves the existing pipeline handling it correctly, while routing to
        managed when the record is not really migrated ingests into a knowledge base
        that may not exist.
        """
        _seed_kb(table, retrievalEngine=engine)
        fake = _FakeBackend()
        with patch("apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake):
            result = ic.handle_object(BUCKET, KEY)
        assert result["routed"] == "legacy"
        assert fake.ingested == []


# ---------------------------------------------------------------------------
# Managed must be ingested here, exactly once
# ---------------------------------------------------------------------------
class TestManagedRouting:
    def _seed_managed(self, table):
        _seed_kb(
            table,
            retrievalEngine="managed",
            awsKbId="KB123",
            awsDataSourceId="DS456",
        )

    def test_a_managed_document_is_ingested_directly(self, table):
        self._seed_managed(table)
        fake = _FakeBackend()

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            result = ic.handle_object(BUCKET, KEY)

        assert result["routed"] == "managed"
        assert result["ingested"] is True
        assert fake.ingested == [DOCUMENT_ID]

    def test_a_managed_document_is_ingested_exactly_once(self, table):
        """One invocation, one ingest. Duplicate chunks would compete in retrieval."""
        self._seed_managed(table)
        fake = _FakeBackend()

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            ic.lambda_handler(_eventbridge_event(), None)

        assert fake.ingested == [DOCUMENT_ID]

    def test_the_document_reaches_complete(self, table):
        self._seed_managed(table)
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend",
            return_value=_FakeBackend(),
        ):
            ic.handle_object(BUCKET, KEY)

        assert _doc(table)["status"] == "complete"

    def test_indexed_and_retrievable_are_recorded_separately(self, table):
        """Two timestamps, not one.

        Bedrock reports INDEXED up to a second before a document can actually be
        retrieved (measured 0.75-1.03 s). Collapsing them would erase the only
        evidence of that gap, which is what makes "my upload finished but the
        assistant cannot see it" diagnosable.
        """
        self._seed_managed(table)
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend",
            return_value=_FakeBackend(),
        ):
            result = ic.handle_object(BUCKET, KEY)

        item = _doc(table)
        assert "indexedAt" in item
        assert "retrievableAt" in item
        assert result["indexedAt"] and result["retrievableAt"]

    def test_a_managed_document_never_falls_back_to_legacy(self, table):
        """Managed engine but unprovisioned must FAIL, not silently degrade.

        A quiet fallback would hand the document to the legacy pipeline as well,
        producing the dual index this whole file guards against.
        """
        _seed_kb(table, retrievalEngine="managed")  # no awsKbId / awsDataSourceId

        with pytest.raises(ic.IngestionRoutingError, match="not provisioned"):
            ic.handle_object(BUCKET, KEY)

    def test_a_failed_ingestion_marks_the_document_failed_and_raises(self, table):
        """The record is the retry anchor, so a failure must be visible in both
        places: on the document and to the event source."""
        self._seed_managed(table)

        class _Failing(_FakeBackend):
            async def ingest(self, kb_ref, source):
                raise RuntimeError("bedrock unavailable")

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=_Failing()
        ):
            with pytest.raises(RuntimeError):
                ic.handle_object(BUCKET, KEY)

        item = _doc(table)
        assert item["status"] == "failed"
        assert "bedrock unavailable" in item["ingestionError"]

    def test_a_document_that_never_becomes_retrievable_is_not_marked_complete(self, table):
        """Indexed is not retrievable. Claiming success here is the bug."""
        self._seed_managed(table)

        class _NeverRetrievable(_FakeBackend):
            async def search(self, kb_ref, query, top_k=5):
                return []

        # Shrink the poll window: the real 30s default is correct in production
        # (the observed gap is ~1s and waiting is cheap) but would add 30s to every
        # run of this suite.
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend",
            return_value=_NeverRetrievable(),
        ), patch.object(ic, "RETRIEVABLE_POLL_TIMEOUT_SECONDS", 0.05), patch.object(
            ic, "RETRIEVABLE_POLL_INTERVAL_SECONDS", 0.01
        ):
            with pytest.raises(ic.IngestionRoutingError, match="not retrievable"):
                ic.handle_object(BUCKET, KEY)

        assert _doc(table)["status"] != "complete"


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------
class TestEventParsing:
    def test_eventbridge_shape_is_understood(self):
        records = ic.extract_records(_eventbridge_event())
        assert records == [{"bucket": BUCKET, "key": KEY}]

    def test_raw_s3_notification_shape_is_understood(self):
        """Both shapes are accepted so a wiring change cannot silently stop
        ingestion — the bucket carries two producers."""
        event = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": KEY}}}]}
        assert ic.extract_records(event) == [{"bucket": BUCKET, "key": KEY}]

    def test_an_empty_event_is_a_no_op(self):
        assert ic.lambda_handler({}, None)["processed"] == 0

    def test_a_url_encoded_key_is_decoded(self):
        a, d, f = ic.parse_object_key(
            "assistants/ast-1/documents/doc-2/my+report+%282024%29.pdf"
        )
        assert (a, d) == ("ast-1", "doc-2")
        assert f == "my report (2024).pdf"

    def test_a_filename_containing_slashes_is_preserved(self):
        _, _, f = ic.parse_object_key("assistants/a/documents/d/sub/dir/file.pdf")
        assert f == "sub/dir/file.pdf"

    @pytest.mark.parametrize(
        "key",
        [
            "wrong/ast-1/documents/doc-2/f.pdf",
            "assistants/ast-1/wrong/doc-2/f.pdf",
            "assistants/ast-1/documents/doc-2",
            "",
        ],
    )
    def test_a_malformed_key_is_refused(self, key):
        """Guessing at a malformed key could ingest one assistant's document into
        another's knowledge base."""
        with pytest.raises(ic.IngestionRoutingError):
            ic.parse_object_key(key)


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------
class TestNoInProcessOrchestration:
    def test_the_module_does_not_use_ensure_future(self):
        """Requirement 10.8. A background task is killed when the Lambda handler
        returns, converting a reported success into a half-finished ingestion."""
        import ast
        import inspect

        # Parsed, not grepped. A substring check trips on this module's own
        # docstring, which explains at length WHY it does not orchestrate in
        # process — the first version of this test failed on the prose describing
        # the very thing it was verifying the absence of.
        tree = ast.parse(inspect.getsource(ic))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "ensure_future" not in called
        assert "create_task" not in called
