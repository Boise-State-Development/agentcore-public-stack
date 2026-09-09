"""Dead-letter document reconciler — task 16.5, HANDOFF §5.37.

The reconciler is the missing *second* writer of ``DOC#`` status. The ingestion
consumer is the only writer today, and when its event dead-letters (Lambda async
retry is capped at 2) a document Bedrock finished indexing is left parked
non-terminal forever — and the retrieval filter serves only ``complete``, so its
content is in the knowledge base and invisible to every query.

Four assertions here are the reason the file exists, and each guards a mistake a
green suite would otherwise hide:

**A stranded-but-retrievable document is driven to ``complete`` — but only when it
is genuinely retrievable.** The §5.37 fix. Marking it complete on ``INDEXED``
alone, without confirming a filtered retrieval returns it, recreates the exact
"upload worked but the assistant cannot see it" report the consumer was built to
prevent. Both halves are asserted.

**Report-only really is a no-op.** The shipped mode plans every action and writes
nothing; the arming flag treats an empty string as off.

**The grace gate reads the row's own ``updatedAt``, never discovery time**, and
fails closed when it cannot be read — so an in-flight upload is never marked from
under the consumer.

**Terminal and soft-deleted rows are untouchable.** ``complete``/``failed`` are
done; ``deleting`` is being removed on purpose and must never be resurrected.

No test contacts AWS. DynamoDB is moto; the managed backend is a stub that models
Bedrock's document view, mirroring ``test_kb_ingestion_consumer``.
"""

import types
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from apis.app_api.kb_migration import document_reconciler as dr

REGION = "us-east-1"
TABLE = "test-doc-reconciler"
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# Ages relative to NOW.
OLD = _iso(NOW - timedelta(hours=2))          # comfortably past the 60-minute gate
YOUNG = _iso(NOW - timedelta(minutes=5))      # still plausibly in flight


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    # Never inherited from the developer's shell: the reconciler is disarmed unless
    # something says otherwise.
    monkeypatch.delenv(dr.FLAG_DOC_RECONCILER_ARMED, raising=False)

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


@pytest.fixture(autouse=True)
def no_metrics(monkeypatch):
    monkeypatch.setattr(dr, "emit_count", lambda *a, **k: None)


# ── Fixtures for the table ────────────────────────────────────────────────────
def _seed_kb(table, assistant_id, *, engine="managed", aws_kb_id="KB1"):
    item = {
        "PK": f"AST#{assistant_id}",
        "SK": f"KB#{assistant_id}",
        "appKbId": assistant_id,
    }
    if engine:
        item["retrievalEngine"] = engine
    if aws_kb_id:
        item["awsKbId"] = aws_kb_id
        item["awsDataSourceId"] = f"DS{aws_kb_id}"
    table.put_item(Item=item)


def _seed_doc(table, assistant_id, document_id, *, status, updated_at=OLD, s3_key=None, filename=None):
    item = {
        "PK": f"AST#{assistant_id}",
        "SK": f"DOC#{document_id}",
        "documentId": document_id,
        "status": status,
        "updatedAt": updated_at,
    }
    if s3_key is not None:
        item["s3Key"] = s3_key
    if filename is not None:
        item["filename"] = filename
    table.put_item(Item=item)


def _doc(table, assistant_id, document_id):
    return table.get_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"}
    ).get("Item")


# ── The stub backend: models Bedrock's document view ─────────────────────────
class _FakeBackend:
    """Models the parts of ManagedKbBackend the reconciler leans on.

    ``statuses`` maps document_id -> the status Bedrock's ``GetKnowledgeBaseDocuments``
    reports (INDEXED, FAILED, IN_PROGRESS, TEXT_INDEXED, PARTIALLY_INDEXED,
    NOT_FOUND, or anything unrecognised). ``retrievable`` is the set of document ids
    a *filtered* retrieval returns — modelling that INDEXED does not imply
    retrievable, and that the id-as-query search only works when filtered.
    """

    def __init__(self, statuses=None, retrievable=None, other_documents=("DOC-someone-else",)):
        self._statuses = dict(statuses or {})
        self._retrievable = set(retrievable or [])
        self._other_documents = list(other_documents)
        self.search_filters = []
        self.ingested = []
        self._agent_client = _FakeAgent(self)

    # -- the private surface `ic.document_status` reuses -----------------------
    def _agent(self):
        return self._agent_client

    def _locate(self, kb_ref):
        return ("KB1", "DS1")

    # -- the protocol surface --------------------------------------------------
    async def ingest(self, kb_ref, source):
        self.ingested.append(source.document_id)

    async def search(self, kb_ref, query, top_k=5, retrieval_filter=None):
        """Honours an ``equals`` filter on ``document_id``; otherwise ranks badly.

        The unfiltered branch returns the *other* documents — what the real service
        did (§5.38): an unfiltered search for a document id returns whatever the
        reranker prefers. A reconciler that dropped the filter would confirm the
        wrong document as retrievable.
        """
        self.search_filters.append(retrieval_filter)
        wanted = None
        if retrieval_filter:
            equals = retrieval_filter.get("equals") or {}
            if equals.get("key") == "document_id":
                wanted = equals.get("value")

        if wanted is not None:
            doc_ids = [wanted] if wanted in self._retrievable else []
        else:
            doc_ids = list(self._other_documents)

        return [types.SimpleNamespace(metadata={"document_id": d}) for d in doc_ids]


class _FakeAgent:
    def __init__(self, owner):
        self._owner = owner

    def get_knowledge_base_documents(self, **kwargs):
        identifiers = kwargs.get("documentIdentifiers") or [{}]
        doc_id = (identifiers[0].get("custom") or {}).get("id")
        status = self._owner._statuses.get(doc_id, "NOT_FOUND")
        if status == "NOT_FOUND":
            return {"documentDetails": []}
        return {
            "documentDetails": [
                {
                    "status": status,
                    "identifier": {"dataSourceType": "CUSTOM", "custom": {"id": doc_id}},
                    "updatedAt": datetime(2026, 5, 30, tzinfo=timezone.utc),
                }
            ]
        }


def _run(table, backend, **kwargs):
    """Run a pass with an injected backend factory returning ``backend``."""
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("backend_factory", lambda _assistant_id: backend)
    return dr.reconcile_documents(**kwargs)


# ── The arming flag ──────────────────────────────────────────────────────────
class TestArmingFlag:
    @pytest.mark.parametrize("value", ["", " ", "0", "false", "False", "off", "no", "disabled"])
    def test_falsy_and_empty_values_are_off(self, monkeypatch, value):
        monkeypatch.setenv(dr.FLAG_DOC_RECONCILER_ARMED, value)
        assert dr.doc_reconciler_armed() is False

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(dr.FLAG_DOC_RECONCILER_ARMED, raising=False)
        assert dr.doc_reconciler_armed() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "enabled", " true "])
    def test_affirmative_values_arm(self, monkeypatch, value):
        monkeypatch.setenv(dr.FLAG_DOC_RECONCILER_ARMED, value)
        assert dr.doc_reconciler_armed() is True

    def test_reconcile_defaults_to_the_flag(self, table, monkeypatch):
        monkeypatch.setenv(dr.FLAG_DOC_RECONCILER_ARMED, "")
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading")
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend)  # note: no armed= override, so it reads the flag

        assert report.armed is False
        assert report.to_dict()["mode"] == "report-only"


# ── The grace gate ───────────────────────────────────────────────────────────
class TestGraceGate:
    def test_a_recently_updated_document_is_left_alone(self, table):
        """TRAP: acting on a young row races an ingestion still in flight."""
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-young", status="uploading", updated_at=YOUNG)
        backend = _FakeBackend(statuses={"doc-young": "INDEXED"}, retrievable={"doc-young"})

        report = _run(table, backend, armed=True)

        assert report.skipped_too_young == ["doc-young"]
        assert report.planned_actions == []
        assert _doc(table, "ast-1", "doc-young")["status"] == "uploading"

    def test_a_long_stuck_document_is_reconciled(self, table):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-old", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-old": "INDEXED"}, retrievable={"doc-old"})

        report = _run(table, backend, armed=True)

        assert report.skipped_too_young == []
        assert report.stranded == 1

    def test_missing_or_unparseable_updated_at_fails_closed(self):
        assert dr.document_is_stuck_long_enough(None, now=NOW) is False
        assert dr.document_is_stuck_long_enough("not-a-date", now=NOW) is False

    def test_the_gate_is_a_pure_function_of_updated_at(self):
        old = NOW - timedelta(hours=2)
        young = NOW - timedelta(minutes=5)
        assert dr.document_is_stuck_long_enough(_iso(old), now=NOW) is True
        assert dr.document_is_stuck_long_enough(_iso(young), now=NOW) is False
        # Same answer regardless of when it is asked — the property a discovery-time
        # clock does not have.
        assert dr.document_is_stuck_long_enough(_iso(old), now=NOW + timedelta(days=9)) is True

    def test_min_age_is_read_at_call_time(self, monkeypatch):
        stamped = NOW - timedelta(minutes=30)
        assert dr.document_is_stuck_long_enough(_iso(stamped), now=NOW) is False
        monkeypatch.setattr(dr, "STUCK_MIN_AGE_MINUTES", 10.0)
        assert dr.document_is_stuck_long_enough(_iso(stamped), now=NOW) is True


# ── The §5.37 fix: stranded-but-retrievable → complete ───────────────────────
class TestMarkComplete:
    def test_a_stranded_retrievable_document_is_completed_when_armed(self, table):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="embedding", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=True)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_MARK_COMPLETE]
        assert report.actions_performed == 1
        row = _doc(table, "ast-1", "doc-1")
        assert row["status"] == "complete"
        assert row["retrievableAt"]
        assert row["indexedAt"]

    def test_indexed_but_not_retrievable_is_left_short_of_complete(self, table):
        """MUTATION GUARD: dropping the retrievability check would complete this.

        Bedrock says INDEXED, but a filtered retrieval returns nothing — so the
        content is not yet queryable. Marking it complete here is the exact bug the
        consumer's retrievability poll exists to prevent.
        """
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable=set())

        report = _run(table, backend, armed=True)

        assert report.planned_actions == []
        assert report.skipped_not_retrievable == ["doc-1"]
        assert _doc(table, "ast-1", "doc-1")["status"] == "uploading"

    def test_partially_indexed_and_retrievable_is_completed(self, table):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="chunking", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "PARTIALLY_INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=True)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_MARK_COMPLETE]
        assert _doc(table, "ast-1", "doc-1")["status"] == "complete"

    def test_report_only_plans_but_writes_nothing(self, table):
        """MUTATION GUARD: report-only must plan the action and perform none of it."""
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=False)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_MARK_COMPLETE]
        assert report.actions_performed == 0
        assert _doc(table, "ast-1", "doc-1")["status"] == "uploading"
        assert backend.ingested == []


# ── Bedrock FAILED → failed ──────────────────────────────────────────────────
class TestMarkFailed:
    @pytest.mark.parametrize("bedrock_status", ["FAILED", "METADATA_UPDATE_FAILED"])
    def test_a_failed_document_is_marked_failed_when_armed(self, table, bedrock_status):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="embedding", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": bedrock_status})

        report = _run(table, backend, armed=True)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_MARK_FAILED]
        row = _doc(table, "ast-1", "doc-1")
        assert row["status"] == "failed"
        assert row.get("ingestionError")

    def test_a_failed_document_is_not_confused_for_retrievable(self, table):
        """A FAILED document must never be probed for retrievability and completed."""
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "FAILED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=True)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_MARK_FAILED]
        assert _doc(table, "ast-1", "doc-1")["status"] == "failed"


# ── Bedrock NOT_FOUND → re-ingest (task 14.4 overlap) ────────────────────────
class TestReIngest:
    def test_a_not_found_document_is_re_ingested_when_armed(self, table):
        _seed_kb(table, "ast-1")
        _seed_doc(
            table, "ast-1", "doc-1", status="uploading", updated_at=OLD,
            s3_key="assistants/ast-1/documents/doc-1/report.pdf", filename="report.pdf",
        )
        backend = _FakeBackend(statuses={"doc-1": "NOT_FOUND"})

        report = _run(table, backend, armed=True)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_RE_INGEST]
        assert report.actions_performed == 1
        assert backend.ingested == ["doc-1"]
        # Re-ingest re-fires the pipeline; the consumer drives it to complete, so
        # the reconciler leaves the row non-terminal rather than claiming success.
        assert _doc(table, "ast-1", "doc-1")["status"] == "uploading"

    def test_report_only_does_not_re_ingest(self, table):
        _seed_kb(table, "ast-1")
        _seed_doc(
            table, "ast-1", "doc-1", status="uploading", updated_at=OLD,
            s3_key="assistants/ast-1/documents/doc-1/report.pdf", filename="report.pdf",
        )
        backend = _FakeBackend(statuses={"doc-1": "NOT_FOUND"})

        report = _run(table, backend, armed=False)

        assert [a.kind for a in report.planned_actions] == [dr.ACTION_RE_INGEST]
        assert backend.ingested == []

    def test_not_found_without_an_s3_key_is_not_re_ingested(self, table):
        """An old row with no s3Key cannot be re-ingested from here."""
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)  # no s3Key
        backend = _FakeBackend(statuses={"doc-1": "NOT_FOUND"})

        report = _run(table, backend, armed=True)

        assert report.planned_actions == []
        assert backend.ingested == []
        assert report.skipped_not_retrievable == ["doc-1"]


# ── In-flight and unknown statuses are left alone ─────────────────────────────
class TestLeftAlone:
    @pytest.mark.parametrize("bedrock_status", ["STARTING", "PENDING", "IN_PROGRESS", "TEXT_INDEXED"])
    def test_in_flight_documents_are_left_alone(self, table, bedrock_status):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": bedrock_status})

        report = _run(table, backend, armed=True)

        assert report.planned_actions == []
        assert report.skipped_in_flight == ["doc-1"]
        assert _doc(table, "ast-1", "doc-1")["status"] == "uploading"

    def test_an_unknown_bedrock_status_is_treated_as_in_flight(self, table):
        """§5.39: the live service returns statuses the SDK enum omits. Unknown
        must mean 'keep waiting', not 'give up'."""
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "SOME_NEW_STATUS_AWS_ADDED"})

        report = _run(table, backend, armed=True)

        assert report.planned_actions == []
        assert report.skipped_in_flight == ["doc-1"]


# ── Terminal and soft-deleted rows are untouchable ───────────────────────────
class TestTerminalRowsIgnored:
    @pytest.mark.parametrize("status", ["complete", "failed", "deleting"])
    def test_terminal_and_deleting_rows_are_never_candidates(self, table, status):
        """MUTATION GUARD: NON_TERMINAL_STATUSES must exclude these.

        ``deleting`` is the dangerous one — a soft-deleted document driven back to
        ``complete`` would resurrect content the user removed.
        """
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status=status, updated_at=OLD)
        # Bedrock would say INDEXED+retrievable, which WOULD complete a candidate.
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=True)

        assert report.stranded == 0
        assert report.planned_actions == []
        assert _doc(table, "ast-1", "doc-1")["status"] == status


# ── Engine and provisioning scoping ──────────────────────────────────────────
class TestScoping:
    def test_legacy_assistant_documents_are_ignored(self, table):
        """A record with no retrievalEngine is legacy; its DOC# status is owned by
        the legacy pipeline, not this reconciler."""
        _seed_kb(table, "ast-legacy", engine=None, aws_kb_id=None)
        _seed_doc(table, "ast-legacy", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=True)

        assert report.managed_records == 0
        assert report.documents_scanned == 0
        assert report.planned_actions == []

    def test_an_unprovisioned_managed_record_is_skipped(self, table):
        """Managed but no awsKbId: there is no knowledge base to probe yet."""
        _seed_kb(table, "ast-prov", engine="managed", aws_kb_id=None)
        _seed_doc(table, "ast-prov", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})

        report = _run(table, backend, armed=True)

        assert report.managed_records == 0
        assert report.planned_actions == []


# ── Per-run action limit ─────────────────────────────────────────────────────
class TestPerRunActionLimit:
    def _five_stranded(self, table):
        _seed_kb(table, "ast-1")
        statuses, retrievable = {}, set()
        for i in range(5):
            _seed_doc(table, "ast-1", f"doc-{i}", status="uploading", updated_at=OLD)
            statuses[f"doc-{i}"] = "INDEXED"
            retrievable.add(f"doc-{i}")
        return _FakeBackend(statuses=statuses, retrievable=retrievable)

    def test_the_limit_caps_planned_actions_in_report_only_mode(self, table, monkeypatch):
        monkeypatch.setattr(dr, "MAX_ACTIONS_PER_RUN", 2)
        backend = self._five_stranded(table)

        report = _run(table, backend, armed=False)

        assert len(report.planned_actions) == 2
        assert report.limit_reached is True

    def test_the_limit_caps_actual_actions_when_armed(self, table, monkeypatch):
        monkeypatch.setattr(dr, "MAX_ACTIONS_PER_RUN", 2)
        backend = self._five_stranded(table)

        report = _run(table, backend, armed=True)

        assert report.actions_performed == 2
        assert report.limit_reached is True
        completed = sum(
            1 for i in range(5) if (_doc(table, "ast-1", f"doc-{i}") or {})["status"] == "complete"
        )
        assert completed == 2

    def test_the_environment_can_lower_the_limit_but_not_lift_it(self, monkeypatch):
        monkeypatch.setenv("MANAGED_KB_DOC_RECONCILER_MAX_ACTIONS", "3")
        assert dr.max_actions_per_run() == 3
        monkeypatch.setenv("MANAGED_KB_DOC_RECONCILER_MAX_ACTIONS", "1000000")
        assert dr.max_actions_per_run() == dr.MAX_ACTIONS_CEILING

    def test_a_negative_limit_does_not_become_unbounded(self, monkeypatch):
        monkeypatch.setenv("MANAGED_KB_DOC_RECONCILER_MAX_ACTIONS", "-5")
        assert dr.max_actions_per_run() == 0

    def test_the_limit_is_read_at_call_time(self, monkeypatch):
        assert dr.max_actions_per_run() == dr.MAX_ACTIONS_PER_RUN
        monkeypatch.setattr(dr, "MAX_ACTIONS_PER_RUN", 3)
        assert dr.max_actions_per_run() == 3


# ── Retrievability probe (§5.38) ─────────────────────────────────────────────
class TestRetrievabilityProbe:
    def test_it_filters_on_document_id_by_equals(self, table):
        """MUTATION GUARD: an unfiltered search returns the wrong document (§5.38).

        The backend returns OTHER documents when unfiltered. ``is_retrievable`` must
        pass the ``equals`` filter, so a document not in the retrievable set returns
        False even though the search would otherwise return chunks.
        """
        backend = _FakeBackend(retrievable=set())  # nothing is retrievable

        assert dr.is_retrievable(backend, "ast-1", "doc-1") is False
        assert backend.search_filters == [{"equals": {"key": "document_id", "value": "doc-1"}}]

    def test_it_returns_true_only_for_its_own_document(self, table):
        backend = _FakeBackend(retrievable={"doc-1"})
        assert dr.is_retrievable(backend, "ast-1", "doc-1") is True
        assert dr.is_retrievable(backend, "ast-1", "doc-other") is False

    def test_a_probe_error_is_not_a_positive(self):
        class Boom:
            async def search(self, *a, **k):
                raise RuntimeError("retrieve failed")

        assert dr.is_retrievable(Boom(), "ast-1", "doc-1") is False


# ── The lambda handler ───────────────────────────────────────────────────────
class TestLambdaHandler:
    @pytest.fixture()
    def one_stranded(self, table, monkeypatch):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-1", status="uploading", updated_at=OLD)
        backend = _FakeBackend(statuses={"doc-1": "INDEXED"}, retrievable={"doc-1"})
        # lambda_handler builds its own backend via the default factory, which we
        # cannot inject through the event — so patch the factory builder.
        monkeypatch.setattr(dr, "_default_backend_factory", lambda client: (lambda _a: backend))
        # And pin 'now' well ahead of the row's OLD stamp is unnecessary: OLD is
        # relative to a fixed 2026 date, comfortably older than real wall-clock.
        return backend

    def test_it_returns_the_serialized_report(self, table, monkeypatch):
        monkeypatch.setattr(
            dr, "reconcile_documents", lambda **kw: dr.DocumentReconcileReport(armed=False)
        )
        result = dr.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert result["report"]["mode"] == "report-only"

    @pytest.mark.parametrize("payload", [True, "true", 1, "1", "yes"])
    def test_the_event_cannot_arm_the_reconciler(self, table, one_stranded, payload):
        result = dr.lambda_handler({"armed": payload}, None)
        assert result["report"]["mode"] == "report-only"
        assert result["report"]["actionsPerformed"] == 0
        assert one_stranded.ingested == []
        assert _doc(table, "ast-1", "doc-1")["status"] == "uploading"
        # The finding is still reported — suppressing the write did not suppress it.
        assert result["report"]["stranded"] == 1

    def test_the_flag_is_what_arms_it(self, table, one_stranded, monkeypatch):
        monkeypatch.setenv(dr.FLAG_DOC_RECONCILER_ARMED, "true")
        result = dr.lambda_handler({"armed": False}, None)
        assert result["report"]["mode"] == "armed"
        assert result["report"]["actionsPerformed"] == 1
        assert _doc(table, "ast-1", "doc-1")["status"] == "complete"

    def test_an_ignored_arming_request_is_logged(self, table, one_stranded, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            dr.lambda_handler({"armed": True}, None)
        assert any(
            "ignoring armed" in r.message and dr.FLAG_DOC_RECONCILER_ARMED in r.message
            for r in caplog.records
        )


# ── Mixed and degenerate cases ───────────────────────────────────────────────
class TestMixedRun:
    def test_all_outcomes_in_one_pass(self, table):
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "d-complete", status="uploading", updated_at=OLD)
        _seed_doc(table, "ast-1", "d-failed", status="embedding", updated_at=OLD)
        _seed_doc(
            table, "ast-1", "d-reingest", status="uploading", updated_at=OLD,
            s3_key="assistants/ast-1/documents/d-reingest/x.pdf", filename="x.pdf",
        )
        _seed_doc(table, "ast-1", "d-inflight", status="chunking", updated_at=OLD)
        _seed_doc(table, "ast-1", "d-young", status="uploading", updated_at=YOUNG)
        _seed_doc(table, "ast-1", "d-done", status="complete", updated_at=OLD)
        backend = _FakeBackend(
            statuses={
                "d-complete": "INDEXED",
                "d-failed": "FAILED",
                "d-reingest": "NOT_FOUND",
                "d-inflight": "IN_PROGRESS",
            },
            retrievable={"d-complete"},
        )

        report = _run(table, backend, armed=True)

        kinds = {a.document_id: a.kind for a in report.planned_actions}
        assert kinds == {
            "d-complete": dr.ACTION_MARK_COMPLETE,
            "d-failed": dr.ACTION_MARK_FAILED,
            "d-reingest": dr.ACTION_RE_INGEST,
        }
        assert report.skipped_in_flight == ["d-inflight"]
        assert report.skipped_too_young == ["d-young"]
        assert _doc(table, "ast-1", "d-complete")["status"] == "complete"
        assert _doc(table, "ast-1", "d-failed")["status"] == "failed"
        assert _doc(table, "ast-1", "d-done")["status"] == "complete"  # untouched

    def test_an_empty_table_is_a_clean_no_op(self, table):
        backend = _FakeBackend()
        report = _run(table, backend, armed=True)
        payload = report.to_dict()
        assert payload["managedRecords"] == 0
        assert payload["stranded"] == 0
        assert payload["plannedActions"] == []
        assert payload["actionsPerformed"] == 0

    def test_a_failing_write_does_not_end_the_run(self, table, monkeypatch):
        """One bad document must not stop the reconciler reaching the others."""
        _seed_kb(table, "ast-1")
        _seed_doc(table, "ast-1", "doc-a", status="uploading", updated_at=OLD)
        _seed_doc(table, "ast-1", "doc-b", status="uploading", updated_at=OLD)
        backend = _FakeBackend(
            statuses={"doc-a": "INDEXED", "doc-b": "INDEXED"},
            retrievable={"doc-a", "doc-b"},
        )

        calls = {"n": 0}
        real = dr.ic.set_document_terminal

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("dynamo throttled")
            return real(*args, **kwargs)

        monkeypatch.setattr(dr.ic, "set_document_terminal", flaky)

        report = _run(table, backend, armed=True)

        assert len(report.planned_actions) == 2
        assert report.actions_performed == 1
        assert any(a.error for a in report.planned_actions)
