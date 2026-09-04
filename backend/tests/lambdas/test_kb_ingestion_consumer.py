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

import logging
from datetime import datetime, timezone
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


class _FakeAgentClient:
    """The control-plane surface the consumer reads document status from."""

    def __init__(self, owner):
        self._owner = owner

    def get_knowledge_base_documents(self, **kwargs):
        self._owner.status_calls += 1
        status = self._owner.next_status()
        if status == "NOT_FOUND":
            return {"documentDetails": []}
        return {
            "documentDetails": [
                {
                    "knowledgeBaseId": "KB123",
                    "dataSourceId": "DS456",
                    "status": status,
                    "identifier": {
                        "dataSourceType": "CUSTOM",
                        "custom": {"id": DOCUMENT_ID},
                    },
                    "updatedAt": datetime(2026, 9, 1, 14, 53, 19, tzinfo=timezone.utc),
                }
            ]
        }


class _FakeBackend:
    """Models the parts of ManagedKbBackend the consumer actually leans on.

    Deliberately models Bedrock's document STATUS, not just the ingest call.
    Ingestion is fire-and-forget: the API returns once the request is accepted and
    says nothing about progress, so a fake that only recorded ingests could not
    express the state the consumer now has to reason about — and a fake that
    reported instant success is what let the 30 s poll window look adequate for
    documents that take minutes.

    ``statuses`` is the sequence returned by successive status probes. The default
    models a small document: unknown, then indexed. Pass a longer sequence to model
    a slow one; the last value repeats forever.
    """

    def __init__(self, statuses=None, other_documents=("DOC-someone-else",)):
        self.ingested = []
        self.status_calls = 0
        self.search_filters = []
        self._statuses = list(statuses or ["NOT_FOUND", "INDEXED"])
        self._agent_client = _FakeAgentClient(self)
        # Models a knowledge base that holds OTHER documents too. Without this a
        # probe that ignores its filter still passes, because the only document
        # present is the one being looked for — which is exactly why the
        # query-by-document-id probe survived until a second document existed.
        self._other_documents = list(other_documents)

    def next_status(self):
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    # -- the private surface `document_status` reuses --------------------------
    def _agent(self):
        return self._agent_client

    def _locate(self, kb_ref):
        return ("KB123", "DS456")

    # -- the protocol surface --------------------------------------------------
    async def ingest(self, kb_ref, source):
        self.ingested.append(source.document_id)
        return None

    async def search(self, kb_ref, query, top_k=5, retrieval_filter=None):
        """Honours an ``equals`` filter on ``document_id``; otherwise ranks badly.

        The unfiltered branch returns the *other* documents, which is what the real
        service did: a document id is meaningless to an embedding model, so an
        unfiltered search returns whatever the reranker prefers. Measured in dev
        with two documents, querying one id returned five chunks that all belonged
        to the other.
        """
        self.search_filters.append(retrieval_filter)

        wanted = None
        if retrieval_filter:
            equals = retrieval_filter.get("equals") or {}
            if equals.get("key") == "document_id":
                wanted = equals.get("value")

        if wanted is not None:
            doc_ids = [wanted] if wanted == DOCUMENT_ID else []
        else:
            doc_ids = list(self._other_documents)

        chunks = []
        for doc_id in doc_ids:
            chunk = MagicMock()
            chunk.metadata = {"document_id": doc_id}
            chunk.document_id = doc_id
            chunks.append(chunk)
        return chunks


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    """Never wait production durations in a unit test.

    The consumer's budgets are deliberately long — INDEXED_POLL_TIMEOUT_SECONDS is
    600 s because Lambda's async retry is capped at 2 attempts, so the wait for
    indexing has to happen inside one invocation. Left unpatched, the handful of
    tests that exercise a document which never finishes indexing would hold this
    file for over twenty minutes.

    This is exactly why those constants are resolved at CALL time rather than bound
    as default arguments: a default argument is evaluated once at import and cannot
    be patched, which an earlier version of this module got wrong and which cost a
    33-second test that silently ignored its own override.
    """
    monkeypatch.setattr(ic, "INDEXED_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ic, "INDEXED_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(ic, "RETRIEVABLE_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ic, "RETRIEVABLE_POLL_INTERVAL_SECONDS", 0.001)


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

    def test_indexed_at_is_bedrocks_timestamp_not_our_clock(self, table):
        """`indexedAt` must be the value Bedrock reports, not the local time.

        The original code set `indexed_at = _now_iso()` immediately after the
        ingest call returned. That call is fire-and-forget — it returns when the
        request is accepted — so the field recorded "when we asked", presented as
        "when indexing finished". For a 1.5 MB PDF in dev those were 5.5 minutes
        apart, and because the field was always populated the error was invisible:
        the pre-existing test asserted only that the key existed and was truthy,
        which a fabricated value satisfies perfectly.
        """
        self._seed_managed(table)
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend",
            return_value=_FakeBackend(),
        ):
            result = ic.handle_object(BUCKET, KEY)

        # The fake reports 2026-09-01T14:53:19+00:00 from GetKnowledgeBaseDocuments.
        assert result["indexedAt"].startswith("2026-09-01T14:53:19"), (
            f"indexedAt is {result['indexedAt']!r}, which is not the timestamp "
            f"Bedrock reported — it looks like a local clock reading"
        )
        assert _doc(table)["indexedAt"].startswith("2026-09-01T14:53:19")

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


# ---------------------------------------------------------------------------
# A slow document must converge, not die
# ---------------------------------------------------------------------------
class TestSlowIndexingConverges:
    """The defect a 1.5 MB PDF exposed in dev on 2026-09-01.

    Ingestion succeeded, but the consumer polled a *retrieval* for 30 s starting
    the instant the ingest call returned — before Bedrock had indexed anything.
    That poll window was sized against the INDEXED -> retrievable gap
    (0.75-1.03 s), while it actually had to cover ingest -> INDEXED -> retrievable,
    which the §5.1 benchmark measured at 37-264 s for PDFs.

    Each of the three redeliveries then RE-INGESTED, discarding progress and
    restarting the clock. The document reached INDEXED 54 s after the final attempt
    was dead-lettered, leaving a fully retrievable document parked at `uploading`
    with nothing left to reconcile it — the legacy pipeline no longer writes status
    for a promoted knowledge base, so there was no second writer to mask it.
    """

    def _seed_managed(self, table):
        _seed_kb(table, retrievalEngine="managed", awsKbId="KB123", awsDataSourceId="DS456")

    def test_a_document_already_being_indexed_is_not_re_ingested(self, table):
        """The core fix. Re-submitting restarts the work we are waiting for."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["IN_PROGRESS"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            with pytest.raises(ic.IngestionRoutingError, match="IN_PROGRESS"):
                ic.handle_object(BUCKET, KEY)

        assert fake.ingested == [], (
            "a document Bedrock was already indexing was submitted again; that "
            "discards the progress this invocation is waiting on"
        )

    def test_a_document_still_indexing_is_left_non_terminal(self, table):
        """Not complete and not failed — the next delivery decides."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["IN_PROGRESS"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            with pytest.raises(ic.IngestionRoutingError):
                ic.handle_object(BUCKET, KEY)

        item = _doc(table)
        assert item["status"] not in ("complete", "failed")
        assert "indexedAt" not in item, "no timestamp may be invented before indexing"

    def test_a_redelivery_completes_the_document_without_a_second_ingest(self, table):
        """Delivery 1 submits and defers; delivery 2 finds it INDEXED and finishes.

        This is the whole convergence property: the document ends up `complete`
        having been handed to Bedrock exactly once.
        """
        self._seed_managed(table)

        # Delivery 1: never seen, then still working for the whole in-invocation wait.
        first = _FakeBackend(statuses=["NOT_FOUND", "IN_PROGRESS"])
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=first
        ):
            with pytest.raises(ic.IngestionRoutingError):
                ic.handle_object(BUCKET, KEY)
        assert first.ingested == [DOCUMENT_ID]
        assert _doc(table)["status"] not in ("complete", "failed")

        # Delivery 2: Bedrock has finished.
        second = _FakeBackend(statuses=["INDEXED"])
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=second
        ):
            ic.handle_object(BUCKET, KEY)

        assert second.ingested == [], "the second delivery must not re-ingest"
        item = _doc(table)
        assert item["status"] == "complete"
        assert item["indexedAt"].startswith("2026-09-01T14:53:19")

    def test_a_small_document_still_finishes_in_one_invocation(self, table):
        """The fast path must not regress into waiting for a retry.

        Deferring every document would add a minute of EventBridge backoff to the
        common case, which is why the in-invocation wait exists at all.
        """
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["NOT_FOUND", "INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            result = ic.handle_object(BUCKET, KEY)

        assert result["ingested"] is True
        assert _doc(table)["status"] == "complete"
        assert fake.ingested == [DOCUMENT_ID]

    def test_a_failed_document_is_marked_failed_and_not_retried(self, table):
        """Terminal on Bedrock's side. Redelivering cannot help."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["FAILED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            result = ic.handle_object(BUCKET, KEY)  # must NOT raise

        assert fake.ingested == []
        item = _doc(table)
        assert item["status"] == "failed"
        assert "FAILED" in item["ingestionError"]
        assert result["status"] == "FAILED"

    def test_a_partially_indexed_document_is_treated_as_usable(self, table):
        """It IS retrievable, so failing it would hide content the user can see."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["PARTIALLY_INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            ic.handle_object(BUCKET, KEY)

        assert fake.ingested == [], "already indexed, even if partially"
        assert _doc(table)["status"] == "complete"

    def test_a_status_probe_failure_does_not_fail_the_document(self, table):
        """An unreadable probe means "no evidence", so ingesting is correct."""
        self._seed_managed(table)

        class _ProbeBroken(_FakeBackend):
            def _agent(self):
                raise RuntimeError("bedrock control plane unavailable")

        fake = _ProbeBroken(statuses=["NOT_FOUND"])
        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            with pytest.raises(ic.IngestionRoutingError):
                ic.handle_object(BUCKET, KEY)

        assert fake.ingested == [DOCUMENT_ID], "a probe failure must not block ingestion"
        assert _doc(table)["status"] != "failed"


# ---------------------------------------------------------------------------
# The retrievability probe must ask an exact question
# ---------------------------------------------------------------------------
class TestTheRetrievabilityProbeIsFiltered:
    """Found in dev on 2026-09-01, with two documents in the knowledge base.

    The probe searched for the document *id as the query text* and checked whether
    that document came back in the top 5. A document id means nothing to an
    embedding model, so the search returned whatever the reranker preferred:
    querying `DOC-40e985680a63` returned five chunks and every one belonged to a
    different document. A perfectly retrievable document was reported as not
    retrievable.

    It scales the wrong way — the more documents a knowledge base holds, the less
    likely the target appears in an unfiltered top-5 — so every upload to a mature
    knowledge base would burn its poll budget and dead-letter. It only ever worked
    while the knowledge base held exactly one document, where anything returned was
    necessarily the right thing.
    """

    def _seed_managed(self, table):
        _seed_kb(table, retrievalEngine="managed", awsKbId="KB123", awsDataSourceId="DS456")

    def test_the_probe_filters_to_the_document_being_confirmed(self, table):
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            ic.handle_object(BUCKET, KEY)

        assert fake.search_filters, "the probe never searched"
        assert all(f is not None for f in fake.search_filters), (
            "the retrievability probe searched WITHOUT a filter; with other "
            "documents present it can return five chunks that all belong to "
            "something else and report a good document as not retrievable"
        )
        assert fake.search_filters[0] == {
            "equals": {"key": "document_id", "value": DOCUMENT_ID}
        }

    def test_the_filter_uses_exact_match_not_a_prefix(self, table):
        """`startsWith` would let DOC-1 confirm DOC-10 as retrievable."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            ic.handle_object(BUCKET, KEY)

        operators = {op for f in fake.search_filters for op in (f or {})}
        assert operators == {"equals"}, f"unsafe filter operator(s): {operators}"

    def test_a_document_confirms_even_when_others_rank_higher(self, table):
        """The regression itself: other documents present must not hide this one."""
        self._seed_managed(table)
        fake = _FakeBackend(
            statuses=["INDEXED"],
            other_documents=("DOC-noise-1", "DOC-noise-2", "DOC-noise-3"),
        )

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            ic.handle_object(BUCKET, KEY)

        assert _doc(table)["status"] == "complete"


# ---------------------------------------------------------------------------
# Statuses the SDK does not declare
# ---------------------------------------------------------------------------
class TestUndeclaredStatusesAreWaitedOut:
    """`TEXT_INDEXED` is returned by the live service and is NOT in the packaged
    model's DocumentStatus enum. Observed in dev on a document with image
    extraction enabled: TEXT_INDEXED first, INDEXED later."""

    def _seed_managed(self, table):
        _seed_kb(table, retrievalEngine="managed", awsKbId="KB123", awsDataSourceId="DS456")

    def test_text_indexed_is_not_treated_as_done(self, table):
        """Marking complete here would claim an image-only page is ready while the
        vision model is still running — the exact report this module prevents."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["TEXT_INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            with pytest.raises(ic.IngestionRoutingError):
                ic.handle_object(BUCKET, KEY)

        assert _doc(table)["status"] != "complete"

    def test_text_indexed_does_not_cause_a_re_ingest(self, table):
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["TEXT_INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            with pytest.raises(ic.IngestionRoutingError):
                ic.handle_object(BUCKET, KEY)

        assert fake.ingested == []

    def test_text_indexed_becoming_indexed_completes_the_document(self, table):
        """The observed real sequence. It must converge, not stall."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["TEXT_INDEXED", "TEXT_INDEXED", "INDEXED"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            ic.handle_object(BUCKET, KEY)

        assert _doc(table)["status"] == "complete"
        assert fake.ingested == [], "already submitted; must not re-ingest"

    def test_a_status_nobody_has_seen_before_is_waited_out_not_failed(self, table):
        """A future AWS status value must not dead-letter documents."""
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["SOME_FUTURE_STATUS"])

        with patch(
            "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
        ):
            with pytest.raises(ic.IngestionRoutingError):
                ic.handle_object(BUCKET, KEY)

        item = _doc(table)
        assert item["status"] != "failed", (
            "an unrecognised status failed the document; unknown values must be "
            "waited out, because the service already returns one the SDK omits"
        )

    def test_text_indexed_is_a_classified_status_not_an_unknown_one(self, table, caplog):
        """Recognition is the only thing that distinguishes it, so test that.

        Dropping TEXT_INDEXED from DOC_STATUSES_IN_FLIGHT is behaviour-equivalent:
        `_still_working` waits on unrecognised statuses too, so the document is
        handled identically either way. A mutation removing it therefore survives
        every behavioural assertion — which means the only honest thing left to
        assert is that we have CLASSIFIED it, and are not merely falling through the
        unknown-status branch and logging a warning on every poll for a state we
        have already seen in production and understand.
        """
        self._seed_managed(table)
        fake = _FakeBackend(statuses=["TEXT_INDEXED"])

        with caplog.at_level(logging.WARNING):
            with patch(
                "apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=fake
            ):
                with pytest.raises(ic.IngestionRoutingError):
                    ic.handle_object(BUCKET, KEY)

        assert "TEXT_INDEXED" in ic.DOC_STATUSES_IN_FLIGHT
        assert not any("unrecognised status" in r.message for r in caplog.records), (
            "TEXT_INDEXED was handled by the unknown-status fallback; it is a state "
            "we have observed in production and it should be classified explicitly"
        )
