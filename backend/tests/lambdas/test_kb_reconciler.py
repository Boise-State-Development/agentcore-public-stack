"""Daily reconciler — the join, the age gate, and the disarmed default.

Feature: managed-kb-migration, task 10.3.
Requirements: 24.4, 14.1-14.8, 19.7, 19.8.

Three assertions here are the reason the file exists, and each guards a mistake
that a passing test suite would otherwise hide:

**The age gate reads AWS's ``createdAt``, never discovery time.** Asserted from
both ends. An orphan that AWS says is eight days old is deletable on the *very
first* run that ever sees it — an implementation that started a 24-hour clock at
discovery would leave it, and would then leave it again after any reconciler
outage. And a knowledge base AWS says is 30 seconds old is left alone even though
it is equally newly discovered, because that one is an in-flight create.

**Record-only marks and never deletes.** A KB_Record whose AWS knowledge base has
gone means the *vectors* are gone. The uploaded bytes are still in S3 and the
``DOC#`` rows still describe them, so the corpus rebuilds on the next ingest and
the owner re-uploads nothing. The record is the only pointer to that corpus, so
deleting it is the single action in this module that would lose user data.

**Report-only really is a no-op.** The shipped mode logs intended deletions and
issues none, and the arming flag treats an empty string as off — an unset GitHub
Actions variable expands to ``""``.

No test contacts AWS. DynamoDB is moto; ``bedrock-agent`` is a stub
(Requirement 24.11).
"""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from apis.app_api.kb_migration import reconciler as rec
from apis.shared.kb_backend import tombstones as tomb
from tests.shared.test_kb_tombstones import FakeBedrockAgent

REGION = "us-east-1"
TABLE = "test-kb-reconciler"
PREFIX = "testprefix"
ENV = "testenv"
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROJECT_PREFIX", PREFIX)
    monkeypatch.setenv("ENVIRONMENT", ENV)
    # Never inherited from the developer's shell: the whole point of the flag is
    # that the reconciler is disarmed unless something says otherwise.
    monkeypatch.delenv(rec.FLAG_RECONCILER_ARMED, raising=False)

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
    monkeypatch.setattr(rec, "emit_count", lambda *a, **k: None)
    monkeypatch.setattr(tomb, "emit_count", lambda *a, **k: None)


def _arn(kb_id):
    return f"arn:aws:bedrock:{REGION}:123456789012:knowledge-base/{kb_id}"


def _aws_kb(kb_id, created_at, status="ACTIVE", app_kb_id=None):
    """One knowledge base as AWS reports it, with AWS's own ``createdAt``."""
    return {
        "knowledgeBaseId": kb_id,
        "name": f"{PREFIX}-kb-{app_kb_id or kb_id}",
        "status": status,
        "knowledgeBaseArn": _arn(kb_id),
        "roleArn": "arn:aws:iam::123456789012:role/kb",
        "createdAt": created_at,
    }


def _ours(kb_id, app_kb_id):
    return {
        _arn(kb_id): {
            "prefix": PREFIX,
            "env": ENV,
            "appKbId": app_kb_id,
            "ownerUserId": "u-1",
        }
    }


def _seed_record(table, assistant_id, aws_kb_id=None, **extra):
    item = {
        "PK": f"AST#{assistant_id}",
        "SK": f"KB#{assistant_id}",
        "appKbId": assistant_id,
        "retrievalEngine": "managed",
    }
    if aws_kb_id:
        item["awsKbId"] = aws_kb_id
        item["awsDataSourceId"] = f"DS{aws_kb_id}"
    item.update(extra)
    table.put_item(Item=item)
    return item


def _record(table, assistant_id):
    return table.get_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"KB#{assistant_id}"}
    ).get("Item")


def _run(client, table, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("stored_bytes_resolver", lambda _assistant_id: None)
    return rec.reconcile(client=client, **kwargs)


# ── Requirement 19.7, 19.8: the arming flag ──────────────────────────────────
class TestArmingFlag:
    @pytest.mark.parametrize(
        "value",
        ["", " ", "0", "false", "False", "off", "no", "disabled", "maybe"],
    )
    def test_falsy_and_empty_values_are_off(self, monkeypatch, value):
        """An **empty string must read as off** (Requirement 19.8).

        An unset GitHub Actions variable expands to ``""``, so a truthiness test
        on the raw value is the exact bug this guards. ``"false"`` matters too:
        ``bool("false")`` is ``True``.
        """
        monkeypatch.setenv(rec.FLAG_RECONCILER_ARMED, value)
        assert rec.reconciler_armed() is False

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(rec.FLAG_RECONCILER_ARMED, raising=False)
        assert rec.reconciler_armed() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "enabled", " true "])
    def test_affirmative_values_arm(self, monkeypatch, value):
        monkeypatch.setenv(rec.FLAG_RECONCILER_ARMED, value)
        assert rec.reconciler_armed() is True

    def test_reconcile_defaults_to_the_flag(self, table, monkeypatch):
        monkeypatch.setenv(rec.FLAG_RECONCILER_ARMED, "")
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBORPH1", NOW - timedelta(days=8))],
            tags=_ours("KBORPH1", "ast-orph1"),
        )

        report = rec.reconcile(
            client=client, now=NOW, stored_bytes_resolver=lambda _a: None
        )

        assert report.armed is False
        assert report.to_dict()["mode"] == "report-only"


# ── Requirement 14.7: report-only deletes nothing ────────────────────────────
class TestReportOnlyDeletesNothing:
    def test_an_eligible_orphan_is_reported_and_not_deleted(self, table):
        """The shipped mode. It must plan the deletion and perform none of it."""
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBORPH1", NOW - timedelta(days=8))],
            tags=_ours("KBORPH1", "ast-orph1"),
        )

        report = _run(client, table, armed=False)

        assert report.orphans == 1
        assert [p.kb_id for p in report.planned_deletions] == ["KBORPH1"]
        assert report.deletions_performed == 0
        assert client.delete_calls == [], (
            "report-only mode issued a DeleteKnowledgeBase call"
        )

    def test_report_only_makes_no_mutating_call_at_all(self, table):
        """Nothing happens: no AWS delete, and no DynamoDB side effect.

        Asserted on the AWS call log rather than on the end state of the table,
        because the saga cleans up after itself — a run that wrote a tombstone,
        deleted the knowledge base and then cleared the tombstone leaves the table
        looking exactly as untouched as a run that did nothing.
        """
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBORPH1", NOW - timedelta(days=8))],
            tags=_ours("KBORPH1", "ast-orph1"),
        )

        _run(client, table, armed=False)

        performed = [op for op, _probe in client.observations if op.startswith("delete_")]
        assert performed == [], f"report-only mode issued mutating calls: {performed}"
        assert tomb.iter_tombstones("ast-orph1") == []
        assert table.scan()["Items"] == []

    def test_armed_actually_deletes_through_the_saga(self, table):
        """The contrast case, so the report-only assertion means something."""
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBORPH1", NOW - timedelta(days=8), app_kb_id="ast-orph1")],
            tags=_ours("KBORPH1", "ast-orph1"),
        )

        report = _run(client, table, armed=True)

        assert client.delete_calls == ["KBORPH1"]
        assert report.deletions_performed == 1
        assert report.planned_deletions[0].performed is True
        # The saga cleared its own tombstone once AWS confirmed absence.
        assert tomb.iter_tombstones("ast-orph1") == []

    def test_armed_delete_writes_the_tombstone_before_calling_aws(self, table):
        """The orphan path must go through the saga, not a bare delete call."""
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBORPH1", NOW - timedelta(days=8), app_kb_id="ast-orph1")],
            tags=_ours("KBORPH1", "ast-orph1"),
            probe=lambda: table.get_item(
                Key={"PK": "AST#ast-orph1", "SK": "KBTOMB#ast-orph1"}
            ).get("Item")
            is not None,
        )

        _run(client, table, armed=True)

        assert client.probes_for("delete_knowledge_base") == [True], (
            "the orphan was deleted without a tombstone in place first"
        )

    def test_an_orphan_tombstone_declares_its_partition_synthetic(self, table):
        """An orphan has no assistant id, so its ``PK`` is not a real partition.

        The tombstone still has to exist — a delete that fails mid-flight must
        leave a work item either way — but it lands under the ``appKbId`` tag
        rather than an assistant, so ``iter_tombstones(<assistant id>)`` will never
        surface it. Unmarked, that item reads as a tombstone for an assistant that
        does not exist, which sends whoever is triaging it looking for a record
        that was never there. Asserted while the tombstone is still in place,
        i.e. from inside the delete call, because a successful saga clears it.
        """
        seen = {}

        def probe():
            item = table.get_item(
                Key={"PK": "AST#ast-orph1", "SK": "KBTOMB#ast-orph1"}
            ).get("Item")
            if item:
                seen.update(item)
            return item is not None

        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBORPH1", NOW - timedelta(days=8), app_kb_id="ast-orph1")],
            tags=_ours("KBORPH1", "ast-orph1"),
            probe=probe,
        )

        _run(client, table, armed=True)

        assert seen, "no tombstone was ever written for the orphan"
        assert seen.get(tomb.SYNTHETIC_PARTITION) is True, (
            f"the orphan tombstone did not declare its partition synthetic: {dict(seen)}"
        )
        # And it says which identifier the partition was derived from, which is the
        # first thing an operator needs in order to go find the resource.
        assert seen.get("anchorSource") == "tag:appKbId"
        assert seen.get("awsKbId") == "KBORPH1"

    def test_a_tombstone_for_a_real_record_is_not_marked_synthetic(self, table):
        """The contrast case: the marker must distinguish, not decorate everything.

        A record-backed delete anchors on a genuine assistant partition, so the
        flag must be absent there — otherwise it carries no information.
        """
        client = FakeBedrockAgent(knowledge_bases=[], tags={})
        probe = {}

        def spy():
            probe.update(
                table.get_item(Key={"PK": "AST#ast-real", "SK": "KBTOMB#ast-real"}).get("Item")
                or {}
            )
            return True

        client.probe = spy
        tomb.write_kb_tombstone("ast-real", "ast-real", "KBREAL")
        spy()

        assert probe, "the control tombstone was not written"
        assert tomb.SYNTHETIC_PARTITION not in probe, (
            f"a record-backed tombstone was flagged synthetic: {dict(probe)}"
        )


# ── Requirement 14.3, 14.4: the age gate ─────────────────────────────────────
class TestAgeGateUsesAwsCreatedAt:
    def test_an_orphan_aws_calls_old_is_deletable_on_its_first_discovery(self, table):
        """TRAP: age-gating on discovery time would skip this.

        The reconciler has never seen this knowledge base before — this is its
        first ever run. AWS says the resource is eight days old, so it is
        immediately eligible. An implementation that stamped a ``firstSeenAt`` and
        waited 24 hours from there would report zero planned deletions here, and
        would do so again after every reconciler outage.
        """
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBOLD", NOW - timedelta(days=8))],
            tags=_ours("KBOLD", "ast-old"),
        )

        report = _run(client, table, armed=False)

        assert [p.kb_id for p in report.planned_deletions] == ["KBOLD"], (
            "an 8-day-old orphan was not eligible on first discovery, which is "
            "what age-gating on discovery time looks like"
        )
        assert report.skipped_too_young == []

    def test_a_freshly_created_knowledge_base_is_left_alone(self, table):
        """The other half of the trap: newly discovered is not newly created.

        30 seconds old by AWS's clock — an in-flight create whose record has not
        been attached yet. Deleting this is the failure mode that loses a user's
        upload mid-provisioning.
        """
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBNEW", NOW - timedelta(seconds=30))],
            tags=_ours("KBNEW", "ast-new"),
        )

        report = _run(client, table, armed=True)

        assert report.planned_deletions == []
        assert report.skipped_too_young == ["KBNEW"]
        assert client.delete_calls == [], "an in-flight create was deleted"

    def test_the_boundary_is_twenty_four_hours(self, table):
        """23 h 59 m survives; 24 h 01 m does not."""
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBJUSTUNDER", NOW - timedelta(hours=23, minutes=59)),
                _aws_kb("KBJUSTOVER", NOW - timedelta(hours=24, minutes=1)),
            ],
            tags={**_ours("KBJUSTUNDER", "a1"), **_ours("KBJUSTOVER", "a2")},
        )

        report = _run(client, table, armed=False)

        assert [p.kb_id for p in report.planned_deletions] == ["KBJUSTOVER"]
        assert report.skipped_too_young == ["KBJUSTUNDER"]

    def test_the_gate_is_a_pure_function_of_the_aws_timestamp(self):
        eight_days = NOW - timedelta(days=8)
        thirty_seconds = NOW - timedelta(seconds=30)

        assert rec.orphan_is_deletable(eight_days, now=NOW) is True
        assert rec.orphan_is_deletable(thirty_seconds, now=NOW) is False
        # Identical answer regardless of when it is asked, which is the property a
        # discovery-time clock does not have.
        assert rec.orphan_is_deletable(eight_days, now=NOW + timedelta(days=30)) is True

    def test_a_missing_created_at_fails_closed(self):
        """No timestamp from AWS means no deletion. Never a guess."""
        assert rec.orphan_is_deletable(None, now=NOW) is False
        assert rec.orphan_is_deletable("not-a-date", now=NOW) is False

    def test_an_orphan_without_a_created_at_is_not_deleted(self, table):
        kb = _aws_kb("KBNODATE", None)
        kb.pop("createdAt")
        client = FakeBedrockAgent(knowledge_bases=[kb], tags=_ours("KBNODATE", "a3"))

        report = _run(client, table, armed=True)

        assert report.planned_deletions == []
        assert report.skipped_too_young == ["KBNODATE"]
        assert client.delete_calls == []

    @pytest.mark.parametrize(
        "created",
        [
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            "2026-05-01T00:00:00Z",
            datetime(2026, 5, 1).timestamp(),
        ],
    )
    def test_aws_timestamp_shapes_all_parse(self, created):
        """boto3 gives a datetime; a stub or a JSON round-trip gives the others."""
        assert rec.parse_aws_timestamp(created) is not None

    def test_min_age_is_read_at_call_time(self, monkeypatch):
        """The threshold must be patchable, not frozen into a default argument."""
        created = NOW - timedelta(hours=2)
        assert rec.orphan_is_deletable(created, now=NOW) is False

        monkeypatch.setattr(rec, "ORPHAN_MIN_AGE_HOURS", 1.0)
        assert rec.orphan_is_deletable(created, now=NOW) is True


# ── Requirement 14.5: record-only never deletes the record ───────────────────
class TestRecordOnlyMarksMissing:
    def test_a_stale_pointer_is_marked_not_removed(self, table):
        """TRAP: the record is the only pointer to a recoverable corpus.

        The vectors are gone; the documents are not. Deleting the record would
        destroy the mapping the rebuild depends on, and the owner would have to
        re-upload.
        """
        _seed_record(table, "ast-stale", aws_kb_id="KBGONE")
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        report = _run(client, table, armed=True)

        assert report.marked_missing == ["ast-stale"]
        record = _record(table, "ast-stale")
        assert record is not None, (
            "the KB_Record was deleted; its documents are still valid and the "
            "knowledge base rebuilds from them on the next ingest"
        )
        assert record["vectorState"] == rec.VECTOR_STATE_MISSING
        assert record["vectorStateObservedAt"]

    def test_the_documents_and_identifiers_are_left_intact(self, table):
        """Nothing else about the record is touched, including its ``DOC#`` rows."""
        _seed_record(table, "ast-stale", aws_kb_id="KBGONE")
        table.put_item(
            Item={"PK": "AST#ast-stale", "SK": "DOC#doc-1", "status": "complete"}
        )
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        _run(client, table, armed=True)

        record = _record(table, "ast-stale")
        assert record["awsKbId"] == "KBGONE"
        assert record["retrievalEngine"] == "managed"
        doc = table.get_item(Key={"PK": "AST#ast-stale", "SK": "DOC#doc-1"})["Item"]
        assert doc["status"] == "complete"

    def test_marking_missing_is_not_a_deletion_even_when_armed(self, table):
        """Being armed licenses deleting *orphans*, never records."""
        _seed_record(table, "ast-stale", aws_kb_id="KBGONE")
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        report = _run(client, table, armed=True)

        assert report.deletions_performed == 0
        assert client.delete_calls == []
        assert _record(table, "ast-stale") is not None

    def test_an_unprovisioned_record_is_not_marked_missing(self, table):
        """No ``awsKbId`` means provisioning has not finished, not that AWS lost it."""
        _seed_record(table, "ast-provisioning", aws_kb_id=None)
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        report = _run(client, table, armed=True)

        assert report.marked_missing == []
        assert _record(table, "ast-provisioning").get("vectorState") is None

    def test_a_tombstone_row_is_not_mistaken_for_a_record(self, table):
        """``KBTOMB#`` must not be swept up by the ``KB#`` prefix scan."""
        tomb.write_kb_tombstone("ast-t", "ast-t", "KBX", "DSX")
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        report = _run(client, table, armed=False)

        assert report.records == 0
        assert report.marked_missing == []


# ── Requirement 14.6: both sides agree ───────────────────────────────────────
class TestBothSidesRefreshStoredBytes:
    def test_stored_bytes_is_re_anchored_from_the_resolver(self, table):
        _seed_record(table, "ast-both", aws_kb_id="KBBOTH", storedBytes=10)
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBBOTH", NOW - timedelta(days=8))],
            tags=_ours("KBBOTH", "ast-both"),
        )

        report = _run(client, table, armed=False, stored_bytes_resolver=lambda _a: 4096)

        assert report.matched == 1
        assert report.orphans == 0
        assert report.refreshed_bytes == ["ast-both"]
        assert int(_record(table, "ast-both")["storedBytes"]) == 4096

    def test_an_unchanged_total_writes_nothing(self, table):
        """A daily no-op write per knowledge base would be pure cost."""
        _seed_record(table, "ast-both", aws_kb_id="KBBOTH", storedBytes=4096)
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBBOTH", NOW - timedelta(days=8))],
            tags=_ours("KBBOTH", "ast-both"),
        )

        report = _run(client, table, armed=False, stored_bytes_resolver=lambda _a: 4096)

        assert report.refreshed_bytes == []

    def test_a_failed_size_lookup_leaves_stored_bytes_alone(self, table):
        """Writing a zero on a failed listing hands the owner their quota back."""
        _seed_record(table, "ast-both", aws_kb_id="KBBOTH", storedBytes=4096)
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBBOTH", NOW - timedelta(days=8))],
            tags=_ours("KBBOTH", "ast-both"),
        )

        _run(client, table, armed=False, stored_bytes_resolver=lambda _a: None)

        assert int(_record(table, "ast-both")["storedBytes"]) == 4096

    def test_a_recovered_knowledge_base_clears_a_stale_missing_marker(self, table):
        """Otherwise the UI keeps reporting a knowledge base broken after the fix."""
        _seed_record(
            table,
            "ast-both",
            aws_kb_id="KBBOTH",
            storedBytes=4096,
            vectorState=rec.VECTOR_STATE_MISSING,
        )
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBBOTH", NOW - timedelta(days=8))],
            tags=_ours("KBBOTH", "ast-both"),
        )

        _run(client, table, armed=False, stored_bytes_resolver=lambda _a: 4096)

        assert _record(table, "ast-both").get("vectorState") is None

    def test_stored_bytes_from_s3_totals_the_prefix(self, table):
        class FakeS3:
            def list_objects_v2(self, **kwargs):
                assert kwargs["Prefix"] == "assistants/ast-s3/documents/"
                return {"Contents": [{"Size": 100}, {"Size": 23}], "IsTruncated": False}

        assert rec.stored_bytes_from_s3("ast-s3", bucket="b", s3_client=FakeS3()) == 123

    def test_stored_bytes_from_s3_returns_none_on_failure(self, table):
        class Boom:
            def list_objects_v2(self, **kwargs):
                raise RuntimeError("access denied")

        assert rec.stored_bytes_from_s3("ast-s3", bucket="b", s3_client=Boom()) is None


# ── Requirement 14.8: bounded per-run action limit ───────────────────────────
class TestPerRunActionLimit:
    def _five_orphans(self):
        kbs = [_aws_kb(f"KBORPH{i}", NOW - timedelta(days=8)) for i in range(5)]
        tags = {}
        for i in range(5):
            tags.update(_ours(f"KBORPH{i}", f"ast-orph{i}"))
        return FakeBedrockAgent(knowledge_bases=kbs, tags=tags)

    def test_the_limit_caps_planned_deletions_in_report_only_mode(self, table, monkeypatch):
        """The report must describe what an armed run would really do.

        A report listing five intended deletions from a run that would only ever
        perform two is a misleading artifact, and the report-only period exists
        precisely so the artifact can be trusted.
        """
        monkeypatch.setattr(rec, "MAX_DELETIONS_PER_RUN", 2)
        client = self._five_orphans()

        report = _run(client, table, armed=False)

        assert report.orphans == 5
        assert len(report.planned_deletions) == 2
        assert report.limit_reached is True

    def test_the_limit_caps_actual_deletions_when_armed(self, table, monkeypatch):
        monkeypatch.setattr(rec, "MAX_DELETIONS_PER_RUN", 2)
        client = self._five_orphans()

        report = _run(client, table, armed=True)

        assert len(client.delete_calls) == 2, (
            f"the per-run limit did not bound the deletions: {client.delete_calls}"
        )
        assert report.deletions_performed == 2
        assert report.limit_reached is True

    def test_without_the_limit_being_hit_nothing_is_flagged(self, table, monkeypatch):
        monkeypatch.setattr(rec, "MAX_DELETIONS_PER_RUN", 25)
        client = self._five_orphans()

        report = _run(client, table, armed=False)

        assert len(report.planned_deletions) == 5
        assert report.limit_reached is False

    def test_the_limit_is_read_at_call_time(self, monkeypatch):
        assert rec.max_deletions_per_run() == rec.MAX_DELETIONS_PER_RUN
        monkeypatch.setattr(rec, "MAX_DELETIONS_PER_RUN", 3)
        assert rec.max_deletions_per_run() == 3
        monkeypatch.setenv("MANAGED_KB_RECONCILER_MAX_DELETIONS", "7")
        assert rec.max_deletions_per_run() == 7

    def test_the_environment_can_lower_the_limit_but_not_lift_it(self, monkeypatch):
        """A bound an env var can raise without limit is not a bound.

        This is the only limit whose failure mode is irreversible, so the ceiling
        has to hold against the variable rather than merely default below it.
        """
        monkeypatch.setenv("MANAGED_KB_RECONCILER_MAX_DELETIONS", "3")
        assert rec.max_deletions_per_run() == 3, "the env var could not lower the limit"

        monkeypatch.setenv("MANAGED_KB_RECONCILER_MAX_DELETIONS", "1000000")
        assert rec.max_deletions_per_run() == rec.MAX_DELETIONS_CEILING, (
            "the environment lifted the per-run deletion bound past its ceiling"
        )

    def test_a_negative_limit_does_not_become_unbounded(self, monkeypatch):
        """A negative slice bound would silently mean 'all of them' downstream."""
        monkeypatch.setenv("MANAGED_KB_RECONCILER_MAX_DELETIONS", "-5")
        assert rec.max_deletions_per_run() == 0


# ── Requirement 14.1: paginated and tag-filtered ─────────────────────────────
class TestJoinIsPaginatedAndTagFiltered:
    def test_orphans_on_later_pages_are_still_found(self, table):
        """Reading only page one would make account size decide correctness."""
        kbs = [_aws_kb(f"KBP{i}", NOW - timedelta(days=8)) for i in range(5)]
        tags = {}
        for i in range(5):
            tags.update(_ours(f"KBP{i}", f"ast-p{i}"))
        client = FakeBedrockAgent(knowledge_bases=kbs, tags=tags, page_size=2)

        report = _run(client, table, armed=False)

        assert report.aws_knowledge_bases == 5
        assert len(report.planned_deletions) == 5

    def test_another_projects_knowledge_base_is_invisible(self, table):
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBMINE", NOW - timedelta(days=8)),
                _aws_kb("KBTHEIRS", NOW - timedelta(days=8)),
            ],
            tags={
                **_ours("KBMINE", "ast-mine"),
                _arn("KBTHEIRS"): {"prefix": "other-project", "env": "prod"},
            },
        )

        report = _run(client, table, armed=True)

        assert report.aws_knowledge_bases == 1
        assert client.delete_calls == ["KBMINE"], (
            "the reconciler acted outside its tag scope"
        )

    def test_an_untagged_knowledge_base_is_never_deleted(self, table):
        client = FakeBedrockAgent(
            knowledge_bases=[_aws_kb("KBBARE", NOW - timedelta(days=8))], tags={}
        )

        report = _run(client, table, armed=True)

        assert report.aws_knowledge_bases == 0
        assert client.delete_calls == []

    def test_a_truncated_aws_walk_suppresses_missing_vector_marks(self, table, monkeypatch):
        """An unmatched record on a partial walk may be one we never reached."""
        monkeypatch.setattr(rec, "MAX_KNOWLEDGE_BASES_PER_RUN", 1)
        _seed_record(table, "ast-a", aws_kb_id="KBA")
        _seed_record(table, "ast-b", aws_kb_id="KBB")
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBA", NOW - timedelta(days=8)),
                _aws_kb("KBB", NOW - timedelta(days=8)),
            ],
            tags={**_ours("KBA", "ast-a"), **_ours("KBB", "ast-b")},
        )

        report = _run(client, table, armed=True)

        assert report.limit_reached is True
        assert report.marked_missing == []
        assert _record(table, "ast-b").get("vectorState") is None


# ── Requirement 13.7 seen from the reconciler ────────────────────────────────
class TestDeleteUnsuccessfulOrphan:
    def test_it_is_surfaced_and_not_retried(self, table):
        """Retrying does not help and the resource keeps billing."""
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBSTUCK", NOW - timedelta(days=200), status="DELETE_UNSUCCESSFUL")
            ],
            tags=_ours("KBSTUCK", "ast-stuck"),
        )

        report = _run(client, table, armed=True)

        assert len(report.planned_deletions) == 1
        planned = report.planned_deletions[0]
        assert planned.error == tomb.KB_STATUS_DELETE_UNSUCCESSFUL
        assert planned.performed is False
        assert client.delete_calls == []

    def test_it_appears_in_the_serialized_report(self, table):
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBSTUCK", NOW - timedelta(days=200), status="DELETE_UNSUCCESSFUL")
            ],
            tags=_ours("KBSTUCK", "ast-stuck"),
        )

        payload = _run(client, table, armed=False).to_dict()

        assert payload["plannedDeletions"][0]["status"] == "DELETE_UNSUCCESSFUL"
        assert payload["deletionsPerformed"] == 0


# ── Mixed and degenerate cases ───────────────────────────────────────────────
class TestMixedRun:
    def test_all_three_outcomes_in_one_pass(self, table):
        _seed_record(table, "ast-both", aws_kb_id="KBBOTH", storedBytes=1)
        _seed_record(table, "ast-stale", aws_kb_id="KBVANISHED")
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBBOTH", NOW - timedelta(days=8)),
                _aws_kb("KBORPH", NOW - timedelta(days=8)),
            ],
            tags={**_ours("KBBOTH", "ast-both"), **_ours("KBORPH", "ast-orph")},
        )

        report = _run(client, table, armed=False, stored_bytes_resolver=lambda _a: 99)

        assert report.records == 2
        assert report.matched == 1
        assert report.orphans == 1
        assert report.marked_missing == ["ast-stale"]
        assert report.refreshed_bytes == ["ast-both"]
        assert [p.kb_id for p in report.planned_deletions] == ["KBORPH"]
        assert _record(table, "ast-stale") is not None

    def test_an_empty_account_and_empty_table_is_a_clean_no_op(self, table):
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        report = _run(client, table, armed=True)

        assert report.to_dict() == {
            "armed": True,
            "mode": "armed",
            "awsKnowledgeBases": 0,
            "records": 0,
            "matched": 0,
            "orphans": 0,
            "plannedDeletions": [],
            "deletionsPerformed": 0,
            "skippedTooYoung": [],
            "markedMissing": [],
            "refreshedBytes": [],
            "limitReached": False,
        }

    def test_a_failing_delete_does_not_end_the_run(self, table, monkeypatch):
        """One stuck orphan must not stop the reconciler reaching the others."""
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBA", NOW - timedelta(days=8)),
                _aws_kb("KBB", NOW - timedelta(days=8)),
            ],
            tags={**_ours("KBA", "ast-a"), **_ours("KBB", "ast-b")},
            polls_before_gone=10_000,
        )
        monkeypatch.setattr(tomb, "KB_DELETE_POLL_TIMEOUT_SECONDS", 0.0)
        monkeypatch.setattr(tomb, "KB_DELETE_POLL_INTERVAL_SECONDS", 0.0)

        report = _run(client, table, armed=True)

        assert len(report.planned_deletions) == 2
        assert report.deletions_performed == 0
        assert all(p.error for p in report.planned_deletions)
        # And the tombstones survive as work items for the next run.
        assert tomb.iter_tombstones("ast-a")
        assert tomb.iter_tombstones("ast-b")


class TestLambdaHandler:
    """The scheduled entry point, and the one input nobody reviews.

    ``lambda_handler`` takes an *event*. An event is not reviewable configuration:
    an EventBridge target can carry a constant payload, and any principal with
    ``lambda:InvokeFunction`` can supply one. So the flag has to be the only way
    to arm (Requirement 19.7) — otherwise deletion of billed user resources is
    reachable while every reviewable setting still reads report-only, and the only
    trace left is an ``Invoke`` in CloudTrail.
    """

    @pytest.fixture()
    def stub_client(self, monkeypatch):
        """Make the un-injected client path safe: no AWS, and a delete log to read.

        ``lambda_handler`` deliberately passes no client, so this patches the
        factory ``reconcile`` reaches for. Without it the test would try to build
        a real ``bedrock-agent`` client (Requirement 24.11).
        """
        from apis.shared.kb_backend import managed_backend

        # 2020: comfortably older than the 24h gate against real wall-clock time,
        # since lambda_handler passes no ``now``.
        client = FakeBedrockAgent(
            knowledge_bases=[
                _aws_kb("KBORPH1", datetime(2020, 1, 1, tzinfo=timezone.utc), app_kb_id="ast-orph1")
            ],
            tags=_ours("KBORPH1", "ast-orph1"),
        )
        monkeypatch.setattr(managed_backend, "bedrock_agent_client", lambda: client)
        return client

    def test_it_returns_the_serialized_report(self, table, monkeypatch):
        monkeypatch.setattr(rec, "reconcile", lambda **kwargs: rec.ReconcileReport(armed=False))

        result = rec.lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["report"]["mode"] == "report-only"

    @pytest.mark.parametrize("payload", [True, "true", 1, "1", "yes"])
    def test_the_event_cannot_arm_the_reconciler(self, table, stub_client, payload):
        """A flag-off invocation carrying ``armed`` deletes nothing.

        Parametrised over a real boolean and the string/int spellings alike,
        because the boolean is the one that would previously have worked: an
        ``isinstance(x, bool)`` override honours ``True`` exactly, so a test that
        only passed ``"true"`` proved nothing about the path that actually armed.
        """
        result = rec.lambda_handler({"armed": payload}, None)

        assert result["report"]["mode"] == "report-only", (
            f"the event payload armed={payload!r} put the reconciler in armed mode"
        )
        assert result["report"]["deletionsPerformed"] == 0
        assert stub_client.delete_calls == [], (
            f"the event payload armed={payload!r} caused a real DeleteKnowledgeBase"
        )
        # And the orphan it declined to delete is still reported, so suppressing
        # the delete has not also suppressed the finding.
        assert result["report"]["orphans"] == 1

    def test_the_flag_is_what_arms_it(self, table, stub_client, monkeypatch):
        """The contrast case: same event, same orphan, flag on — now it deletes.

        Without this, the assertions above would also pass on a reconciler that
        could never delete at all.
        """
        monkeypatch.setenv(rec.FLAG_RECONCILER_ARMED, "true")

        result = rec.lambda_handler({"armed": False}, None)

        assert result["report"]["mode"] == "armed"
        assert stub_client.delete_calls == ["KBORPH1"]
        assert result["report"]["deletionsPerformed"] == 1

    def test_an_ignored_arming_request_is_logged(self, table, stub_client, caplog):
        """Silently dropping the field would hide a misconfigured schedule."""
        import logging

        with caplog.at_level(logging.WARNING):
            rec.lambda_handler({"armed": True}, None)

        assert any(
            "ignoring armed" in r.message and rec.FLAG_RECONCILER_ARMED in r.message
            for r in caplog.records
        ), f"no warning named the ignored override: {[r.message for r in caplog.records]}"
