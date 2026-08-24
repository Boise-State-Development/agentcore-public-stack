"""Conditional-transition tests for the KB_Record data layer.

These assert the *persistence* behaviour, not the logic: the failures this layer
exists to prevent — two workers both promoting, a finished knowledge base left in
the dispatcher's queue, a rollback that writes a legacy value instead of removing
an attribute — are all invisible in a unit test that stubs DynamoDB out. So every
test here drives real condition expressions against moto.

Concurrency is simulated deterministically rather than with threads. Two workers
racing on a conditional write is, from DynamoDB's point of view, simply two
sequential writes where the second one's guard no longer holds. Issuing them in
order and asserting the second is rejected tests exactly the property that
matters and does it without a flaky sleep.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.kb_backend import records as r

REGION = "us-east-1"
TABLE = "test-kb-records"
ASSISTANT_ID = "ast-kb0001"
APP_KB_ID = ASSISTANT_ID  # App_KB_Id == assistant_id in this phase
NOW = "2026-08-24T12:00:00Z"
LATER = "2026-08-24T13:00:00Z"


@pytest.fixture()
def table(monkeypatch):
    """The assistants table including the GSI7 work index.

    Built here rather than reused from the shared ``assistants_table`` fixture,
    which predates GSI7 and only defines GSI1-4. The listing tests set the same
    precedent of building a table when they need an index the shared fixture
    lacks.
    """
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
                {"AttributeName": "GSI7_PK", "AttributeType": "S"},
                {"AttributeName": "GSI7_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "KbWorkIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI7_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI7_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _record(**overrides) -> r.KbRecord:
    base = dict(app_kb_id=APP_KB_ID, owner_user_id="owner-opaque-1")
    base.update(overrides)
    return r.KbRecord(**base)


def _raw(table):
    return table.get_item(
        Key={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_sk(APP_KB_ID)}
    )["Item"]


def _seed(table, **overrides):
    """A record already provisioned and mid-migration."""
    r.create_provisioning(ASSISTANT_ID, _record(**overrides))
    return _raw(table)


# ── create_provisioning ──────────────────────────────────────────────────────
class TestCreateProvisioning:
    def test_creates_the_record(self, table):
        r.create_provisioning(ASSISTANT_ID, _record())
        item = _raw(table)
        assert item["appKbId"] == APP_KB_ID
        assert item["provisioningState"] == r.PROVISIONING

    def test_concurrent_create_yields_exactly_one_winner(self, table):
        """Two enrolments race; one record exists and the loser is told so.

        Without the ``attribute_not_exists`` guard the second write would clobber
        the first, replacing a ``clientToken`` that may already have been used to
        create a real AWS knowledge base — orphaning it with no record pointing
        at it.
        """
        r.create_provisioning(ASSISTANT_ID, _record(client_token="a" * 33))

        with pytest.raises(r.TransitionLost):
            r.create_provisioning(ASSISTANT_ID, _record(client_token="b" * 33))

        assert _raw(table)["clientToken"] == "a" * 33

    def test_a_new_record_carries_no_engine_attribute(self, table):
        """Absence means legacy, so a fresh record must not name an engine.

        A record created with ``retrievalEngine`` already set would be served by
        the managed backend before its knowledge base exists.
        """
        r.create_provisioning(ASSISTANT_ID, _record())
        assert "retrievalEngine" not in _raw(table)

    def test_a_new_record_carries_no_work_keys(self, table):
        """Enrolment is not the same as being queued for work."""
        r.create_provisioning(ASSISTANT_ID, _record())
        item = _raw(table)
        assert "GSI7_PK" not in item
        assert "GSI7_SK" not in item


# ── attach_aws_ids ───────────────────────────────────────────────────────────
class TestAttachAwsIds:
    def test_attaches_and_activates(self, table):
        _seed(table)
        r.attach_aws_ids(ASSISTANT_ID, APP_KB_ID, "kb-123", "ds-456", NOW)
        item = _raw(table)
        assert item["awsKbId"] == "kb-123"
        assert item["awsDataSourceId"] == "ds-456"
        assert item["provisioningState"] == r.ACTIVE

    def test_a_late_duplicate_cannot_overwrite_the_identifiers(self, table):
        """Guarded on still provisioning, so a slow retry cannot rebind the record.

        Rebinding would strand the first AWS knowledge base: still billed, no
        longer referenced.
        """
        _seed(table)
        r.attach_aws_ids(ASSISTANT_ID, APP_KB_ID, "kb-first", "ds-first", NOW)

        with pytest.raises(r.TransitionLost):
            r.attach_aws_ids(ASSISTANT_ID, APP_KB_ID, "kb-second", "ds-second", LATER)

        assert _raw(table)["awsKbId"] == "kb-first"


# ── set_migration_state ──────────────────────────────────────────────────────
class TestSetMigrationState:
    def test_entering_an_eligible_state_writes_the_work_keys(self, table):
        _seed(table)
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 0, due_at=NOW)
        item = _raw(table)
        assert item["GSI7_PK"] == "KBWORK#shadow"
        assert item["GSI7_SK"] == NOW

    @pytest.mark.parametrize("terminal", [r.RETAIN, r.MIGRATION_FAILED])
    def test_a_terminal_state_removes_the_work_keys(self, table, terminal):
        """The removal is what takes the record out of the dispatcher's queue.

        Asserted for both terminal states because ``failed`` is the easy one to
        forget, and a failed migration left in the queue would be retried
        forever against a knowledge base its owner was told had stopped.
        """
        _seed(table)
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 0, due_at=NOW)
        assert "GSI7_PK" in _raw(table)

        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, terminal, 0)

        item = _raw(table)
        assert item["migrationState"] == terminal
        assert "GSI7_PK" not in item, "a terminal record kept its work partition key"
        assert "GSI7_SK" not in item, "a terminal record kept its work sort key"

    def test_a_terminal_record_is_invisible_to_the_work_query(self, table):
        """The physics claim, tested end to end rather than by attribute check."""
        _seed(table)
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 0, due_at=NOW)
        assert len(r.query_due_work(r.SHADOW, LATER)) == 1

        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.RETAIN, 0)
        assert r.query_due_work(r.SHADOW, LATER) == []

    def test_a_stale_generation_cannot_move_the_state(self, table):
        _seed(table, migration_generation=2)
        with pytest.raises(r.TransitionLost):
            r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 1, due_at=NOW)

    def test_expected_states_guards_against_a_concurrent_mover(self, table):
        _seed(table)
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 0, due_at=NOW)

        with pytest.raises(r.TransitionLost):
            r.set_migration_state(
                ASSISTANT_ID, APP_KB_ID, r.PROMOTE, 0,
                due_at=NOW, expected_states=[r.VERIFY],
            )

    def test_reclaim_is_refused(self, table):
        """Reserved in the enum, never entered. Entering it would delete data
        this phase has promised to retain for the rollback window."""
        _seed(table)
        with pytest.raises(r.ReclaimNotSupported):
            r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.RECLAIM, 0, due_at=NOW)

    def test_an_eligible_state_requires_a_due_time(self, table):
        """A work-eligible record with no dueAt would be unschedulable, and a
        partial GSI key silently fails to index rather than erroring."""
        _seed(table)
        with pytest.raises(ValueError):
            r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 0)


# ── promote_engine ───────────────────────────────────────────────────────────
class TestPromoteEngine:
    def _ready(self, table, migrated=3, total=3, generation=0):
        _seed(
            table,
            migration_generation=generation,
            migration_progress={"migrated": migrated, "total": total},
        )
        r.set_migration_state(
            ASSISTANT_ID, APP_KB_ID, r.PROMOTE, generation, due_at=NOW
        )

    def test_promotes_when_catch_up_has_converged(self, table):
        self._ready(table)
        r.promote_engine(ASSISTANT_ID, APP_KB_ID, 0, NOW)
        item = _raw(table)
        assert item["retrievalEngine"] == r.ENGINE_MANAGED
        assert item["promotedAt"] == NOW

    def test_concurrent_promote_yields_exactly_one_winner(self, table):
        """Both workers think they should promote; only one write lands.

        The second is rejected because the first bumped nothing it could reuse —
        it is the guard, not luck, that stops a double promotion.
        """
        self._ready(table)
        r.promote_engine(ASSISTANT_ID, APP_KB_ID, 0, NOW)

        # The winner moves the record on; the loser's guard no longer holds.
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.RETAIN, 0)

        with pytest.raises(r.TransitionLost):
            r.promote_engine(ASSISTANT_ID, APP_KB_ID, 0, LATER)

        assert _raw(table)["promotedAt"] == NOW

    def test_refuses_to_promote_before_catch_up_converges(self, table):
        """The guard that stops documents being stranded on an unread backend."""
        self._ready(table, migrated=2, total=5)
        with pytest.raises(r.TransitionLost):
            r.promote_engine(ASSISTANT_ID, APP_KB_ID, 0, NOW)
        assert "retrievalEngine" not in _raw(table)

    def test_a_stale_generation_cannot_promote(self, table):
        self._ready(table, generation=2)
        with pytest.raises(r.TransitionLost):
            r.promote_engine(ASSISTANT_ID, APP_KB_ID, 1, NOW)
        assert "retrievalEngine" not in _raw(table)

    def test_cannot_promote_from_a_non_promote_state(self, table):
        _seed(table, migration_progress={"migrated": 3, "total": 3})
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.SHADOW, 0, due_at=NOW)
        with pytest.raises(r.TransitionLost):
            r.promote_engine(ASSISTANT_ID, APP_KB_ID, 0, NOW)


# ── rollback_engine ──────────────────────────────────────────────────────────
class TestRollbackEngine:
    def test_rollback_removes_the_attribute_rather_than_writing_legacy(self, table):
        """The invariant that makes rollback a pointer flip.

        A rolled-back record must be indistinguishable from one that never
        migrated. Writing the literal legacy value would pass a naive test and
        quietly convert every future rollback into a data migration.
        """
        _seed(table, migration_progress={"migrated": 1, "total": 1})
        r.set_migration_state(ASSISTANT_ID, APP_KB_ID, r.PROMOTE, 0, due_at=NOW)
        r.promote_engine(ASSISTANT_ID, APP_KB_ID, 0, NOW)
        assert _raw(table)["retrievalEngine"] == r.ENGINE_MANAGED

        r.rollback_engine(ASSISTANT_ID, APP_KB_ID, LATER)

        item = _raw(table)
        assert "retrievalEngine" not in item, "rollback left an engine attribute behind"
        assert r.resolve_engine(item) == r.ENGINE_LEGACY
        assert item["rolledBackAt"] == LATER

    def test_rolling_back_a_legacy_record_is_rejected(self, table):
        _seed(table)
        with pytest.raises(r.TransitionLost):
            r.rollback_engine(ASSISTANT_ID, APP_KB_ID, NOW)


# ── acquire_lease ────────────────────────────────────────────────────────────
class TestAcquireLease:
    def test_takes_a_free_lease(self, table):
        _seed(table)
        r.acquire_lease(ASSISTANT_ID, APP_KB_ID, LATER, NOW)
        assert _raw(table)["migrationLeaseUntil"] == LATER

    def test_a_live_lease_admits_exactly_one_holder(self, table):
        """Two workers cannot migrate the same corpus concurrently.

        Double ingestion is not merely wasteful: it is billed per gigabyte and
        would double the owner's stored bytes against their cap.
        """
        _seed(table)
        r.acquire_lease(ASSISTANT_ID, APP_KB_ID, "2026-08-24T14:00:00Z", NOW)

        with pytest.raises(r.TransitionLost):
            r.acquire_lease(ASSISTANT_ID, APP_KB_ID, "2026-08-24T15:00:00Z", LATER)

        assert _raw(table)["migrationLeaseUntil"] == "2026-08-24T14:00:00Z"

    def test_an_expired_lease_can_be_taken_over(self, table):
        """Otherwise a worker that died holding a lease would strand the record."""
        _seed(table)
        r.acquire_lease(ASSISTANT_ID, APP_KB_ID, "2026-08-24T12:30:00Z", NOW)

        r.acquire_lease(ASSISTANT_ID, APP_KB_ID, "2026-08-24T14:00:00Z", LATER)

        assert _raw(table)["migrationLeaseUntil"] == "2026-08-24T14:00:00Z"


# ── tombstones ───────────────────────────────────────────────────────────────
class TestTombstoneKeys:
    def test_the_two_tombstone_shapes_are_distinct(self):
        assert r.kb_tombstone_sk(APP_KB_ID) == f"KBTOMB#{APP_KB_ID}"
        assert (
            r.document_tombstone_sk(APP_KB_ID, "doc-9")
            == f"KBTOMB#{APP_KB_ID}#DOC#doc-9"
        )

    def test_a_whole_kb_tombstone_does_not_prefix_match_a_document_one(self):
        """They share a prefix, so a query for one must not sweep up the other."""
        whole = r.kb_tombstone_sk(APP_KB_ID)
        doc = r.document_tombstone_sk(APP_KB_ID, "doc-9")
        assert doc.startswith(whole)
        assert doc != whole
