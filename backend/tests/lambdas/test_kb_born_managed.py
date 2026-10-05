"""Born-managed: provisioning stacked onto the first document upload.

Feature: managed-kb-migration, ``MANAGED_KB_NEW_DEFAULT`` (rollout ladder step 2).
Spec: ``.kiro/specs/managed-kb-migration/born-managed-provision-then-ingest.md``.

Four failure modes are what this file is actually for. None of them raises an
error on its own, which is why each needs a test that fails when the guard is
removed:

**1. Double indexing.** The legacy pipeline skips a document only when the record
already resolves to ``managed``. Declare the engine after the first upload instead
of before it and that document is indexed on legacy *and* on managed, with two
writers racing over one ``status`` field.

**2. A dead-lettered first document.** The S3 event fires in seconds;
``CreateKnowledgeBase`` takes minutes. Lambda's async retry is capped at 2
attempts, so a consumer that raises "not provisioned" burns the whole window and
dead-letters — the §5.37 shape, where the bytes are fine and the document is
invisible forever.

**3. A stranded agent.** Managed intent with no knowledge base is a dead end in
both directions: legacy skips it because the record says managed, managed cannot
serve because there is nothing to serve from. Every provisioning failure must
REMOVE ``retrievalEngine``, not merely stop.

**4. A rollout ladder that skipped a rung.** Born-managed is served by the
migration dispatcher, so gating that dispatcher on ``MANAGED_KB_MIGRATION_ENABLED``
would make step 2 either useless alone or a back door that migrates the existing
fleet. Each work state is gated on its own flag, and both halves are asserted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.kb_migration import ingestion_consumer as ic
from apis.app_api.kb_migration import provisioner as pv
from apis.app_api.kb_upgrade import born_managed as bm
from apis.shared.kb_backend import records as r

REGION = "us-east-1"
TABLE = "test-born-managed"
ASSISTANT_ID = "ast-born01"
OWNER = "user-born01"
DOCUMENT_ID = "DOC-born01"
BUCKET = "docs-bucket"
KEY = f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/first.pdf"
OBJECT_BYTES = 4096


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", BUCKET)

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
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"x" * OBJECT_BYTES)
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("MANAGED_KB_NEW_DEFAULT", "true")


def _kb(table):
    got = table.get_item(Key={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_sk(ASSISTANT_ID)})
    return got.get("Item")


def _seed_doc(table, status=bm.STATUS_PROVISIONING, document_id=DOCUMENT_ID, **extra):
    item = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"DOC#{document_id}",
        "documentId": document_id,
        "status": status,
        "s3Key": KEY,
        "createdAt": "2026-09-10T00:00:00Z",
    }
    item.update(extra)
    table.put_item(Item=item)


def _doc(table, document_id=DOCUMENT_ID):
    return table.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{document_id}"}
    ).get("Item")


# ── 1. The first-upload trigger ──────────────────────────────────────────────
class TestFirstUploadTrigger:
    @pytest.mark.asyncio
    async def test_flag_off_writes_nothing_at_all(self, table, monkeypatch):
        """MUTATION GUARD: drop the new_default_enabled() gate and this fails —
        every deployment would start creating Bedrock knowledge bases against a
        ~10,000-per-account quota without anybody turning anything on."""
        monkeypatch.delenv("MANAGED_KB_NEW_DEFAULT", raising=False)

        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is False
        assert _kb(table) is None

    @pytest.mark.asyncio
    async def test_first_upload_declares_managed_and_queues_the_job(self, table, flag_on):
        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is True

        record = _kb(table)
        # Declared managed BEFORE the object lands — this is what makes the legacy
        # pipeline stand down for the very first document.
        assert record["retrievalEngine"] == r.ENGINE_MANAGED
        assert record["provisioningState"] == r.PROVISIONING
        assert record["ownerUserId"] == OWNER
        # Queued: the sparse work keys ARE the queue, so their absence would mean a
        # managed-intent record nothing ever provisions.
        assert record["migrationState"] == r.BORN_MANAGED
        assert record["GSI7_PK"] == r.work_pk(r.BORN_MANAGED)
        assert record["GSI7_SK"]
        # Not yet built, so no identifiers to ingest against.
        assert "awsKbId" not in record
        # The parser configuration is captured at creation, from the same factory
        # provision_managed_kb uses — a corpus indexed without image extraction is
        # not comparable to one indexed with it.
        assert record["imageExtraction"] is True
        assert record["parserConfig"]["imageExtractionStatus"]
        # A knowledge base that was never on legacy has nothing to be congratulated
        # about, so the one-time upgrade notice is retired before it can be shown.
        assert record["upgradeNoticeDismissedAt"]

    @pytest.mark.asyncio
    async def test_established_legacy_agent_with_documents_stays_legacy(self, table, flag_on):
        """MUTATION GUARD: the bug this file's fix addresses. An established legacy
        agent has documents but NO ``KB_Record`` — legacy knowledge bases are not
        first-class, share one S3-Vectors index, and never wrote a record. So the
        absence of a record looks identical to a brand-new agent's. Without the
        existing-documents guard, the agent's *next* upload is mistaken for a first
        upload and provisions a managed knowledge base: retrieval flips to the empty
        managed KB and the existing corpus is stranded on the legacy index, never
        re-ingested. Remove the guard and this fails — a managed record appears."""
        _seed_doc(table, document_id="DOC-existing", status="complete")

        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is False
        # Untouched: no record written, so the next upload takes the legacy pipeline.
        assert _kb(table) is None

    @pytest.mark.asyncio
    async def test_probe_that_cannot_tell_keeps_the_agent_on_legacy(self, table, flag_on):
        """Fail-safe direction. When existence cannot be determined, born-managed
        must NOT provision. Wrongly provisioning on a legacy agent loses its corpus
        from retrieval; wrongly staying legacy is benign (Upgrade still works). The
        helper returns True on any error, so this asserts the trigger honours that."""
        from unittest.mock import AsyncMock as _AsyncMock

        with patch(
            "apis.app_api.documents.services.document_service.assistant_has_documents",
            new=_AsyncMock(return_value=True),
        ):
            assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is False
        assert _kb(table) is None
        await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER)
        before = _kb(table)["GSI7_SK"]

        # Same answer, and it must not re-declare the engine or bump anything.
        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is True
        after = _kb(table)
        assert after["GSI7_SK"] == before
        assert after["migrationState"] == r.BORN_MANAGED

    @pytest.mark.asyncio
    async def test_upload_to_a_built_knowledge_base_takes_the_ordinary_path(
        self, table, flag_on
    ):
        """Once the identifiers are attached this is a plain managed upload: the S3
        event and the ingestion consumer own it, the byte cap applies, and the row
        starts at `uploading` like every other."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.ACTIVE,
                "awsKbId": "KB123",
                "awsDataSourceId": "DS456",
            }
        )
        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is False

    @pytest.mark.asyncio
    async def test_legacy_record_is_left_alone(self, table, flag_on):
        """An existing legacy knowledge base is never converted by this path.
        Upgrading an existing corpus is the Upgrade flow's job, and it verifies."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "provisioningState": r.ACTIVE,
            }
        )
        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is False
        assert "retrievalEngine" not in _kb(table)

    @pytest.mark.asyncio
    async def test_managed_intent_with_no_job_is_requeued(self, table, flag_on):
        """The crash-recovery case: _start died between declaring the engine and
        writing the work keys. Without the re-queue the agent could never ingest
        anything again — legacy skips every upload, and no knowledge base exists."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
                "migrationGeneration": 0,
            }
        )
        assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is True

        record = _kb(table)
        assert record["migrationState"] == r.BORN_MANAGED
        assert record["GSI7_PK"] == r.work_pk(r.BORN_MANAGED)

    @pytest.mark.asyncio
    async def test_a_failure_never_fails_the_upload(self, table, flag_on):
        """Provisioning decides WHICH engine serves a knowledge base; it is never a
        precondition for storing a document. So the trigger swallows everything and
        the upload proceeds on legacy."""
        with patch.object(
            r, "create_provisioning", side_effect=RuntimeError("dynamo is having a day")
        ):
            assert await bm.begin_born_managed(ASSISTANT_ID, owner_user_id=OWNER) is False


# ── 2. The legacy pipeline stands down ───────────────────────────────────────
class TestLegacyPipelineSkips:
    def test_legacy_handler_skips_a_document_bound_for_a_provisioning_kb(self, table):
        """The reason the engine is declared BEFORE the object lands. `_resolve_engine`
        reads the same records.resolve_engine the consumer does, so a managed-intent
        record makes this pipeline stand down even though the knowledge base does not
        exist yet — which is exactly when the provisioner wants it to."""
        from apis.app_api.documents.ingestion import handler as legacy

        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
            }
        )
        assert legacy._resolve_engine(ASSISTANT_ID) == r.ENGINE_MANAGED


# ── 3. The consumer defers instead of dead-lettering ─────────────────────────
class TestConsumerDefers:
    def test_defers_while_the_knowledge_base_is_being_provisioned(self, table):
        """MUTATION GUARD: remove the born-managed branch from handle_object and
        this raises IngestionRoutingError — which, capped at 2 Lambda retries,
        dead-letters the first document of every born-managed agent and leaves it
        invisible forever (§5.37)."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
                "migrationState": r.BORN_MANAGED,
            }
        )
        _seed_doc(table)

        result = ic.handle_object(BUCKET, KEY)

        assert result == {
            "routed": "managed",
            "ingested": False,
            "document_id": DOCUMENT_ID,
            "note": "deferred-provisioning",
        }
        # Untouched: the provisioning job owns this row, and a status written here
        # would either lie to the user or race the job.
        assert _doc(table)["status"] == bm.STATUS_PROVISIONING

    def test_still_raises_for_managed_intent_with_no_provisioner_behind_it(self, table):
        """The defer is scoped to born-managed on purpose. A record that is managed
        with no identifiers and NO provisioning job is a genuine fault: falling
        silently back to legacy there would create the dual-index the consumer
        exists to prevent, so it must still fail loudly."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
            }
        )
        _seed_doc(table, status="uploading")

        with pytest.raises(ic.IngestionRoutingError):
            ic.handle_object(BUCKET, KEY)


# ── 4. The engine-declaration write ──────────────────────────────────────────
class TestAdoptManagedEngine:
    def test_declares_managed_on_a_provisioning_record(self, table):
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "provisioningState": r.PROVISIONING,
            }
        )
        r.adopt_managed_engine(ASSISTANT_ID, ASSISTANT_ID, "2026-09-10T12:00:00Z")

        record = _kb(table)
        assert record["retrievalEngine"] == r.ENGINE_MANAGED
        assert record["bornManagedAt"] == "2026-09-10T12:00:00Z"

    def test_refuses_to_redeclare_an_already_managed_record(self, table):
        """Guards the promotion timestamps of a knowledge base that got to managed
        the other way — and makes a concurrent second first-upload lose cleanly."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "provisioningState": r.PROVISIONING,
                "retrievalEngine": r.ENGINE_MANAGED,
                "promotedAt": "2026-01-01T00:00:00Z",
            }
        )
        with pytest.raises(r.TransitionLost):
            r.adopt_managed_engine(ASSISTANT_ID, ASSISTANT_ID, "2026-09-10T12:00:00Z")
        assert _kb(table)["promotedAt"] == "2026-01-01T00:00:00Z"

    def test_refuses_a_record_that_is_not_being_provisioned(self, table):
        """A torn-down record must not be resurrected as managed with no knowledge
        base behind it."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "provisioningState": r.DELETING,
            }
        )
        with pytest.raises(r.TransitionLost):
            r.adopt_managed_engine(ASSISTANT_ID, ASSISTANT_ID, "2026-09-10T12:00:00Z")

    def test_born_managed_is_work_eligible_and_not_terminal(self):
        """The state has to be in both sets correctly or the queue misbehaves in one
        of two silent ways: not work-eligible and the job is never dispatched;
        terminal and its work keys are stripped the moment it is written."""
        assert r.BORN_MANAGED in r.WORK_ELIGIBLE_STATES
        assert r.BORN_MANAGED in r.ALL_MIGRATION_STATES
        assert r.BORN_MANAGED not in r.TERMINAL_STATES


# ── 5. The provisioning job ──────────────────────────────────────────────────
class TestProvisioningJob:
    @pytest.mark.asyncio
    async def test_provisions_then_ingests_the_waiting_document(self, table):
        """The whole point of the design: the JOB triggers ingestion, so correctness
        never depends on the two-try S3 redelivery window."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "ownerUserId": OWNER,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
                "migrationState": r.BORN_MANAGED,
                "migrationGeneration": 0,
                "GSI7_PK": r.work_pk(r.BORN_MANAGED),
                "GSI7_SK": "2026-09-10T00:00:00Z",
            }
        )
        _seed_doc(table)

        record = _kb(table)
        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            ic, "handle_object", return_value={"routed": "managed"}
        ) as handled:
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, record)

        # Reuses the consumer outright rather than reimplementing ingest →
        # wait-indexed → wait-retrievable → terminal, which is also what carries the
        # authoritative S3-HEAD byte-cap reconcile (Requirement 12.3).
        handled.assert_called_once_with(BUCKET, KEY)
        assert result.documents_migrated == 1
        assert result.to_state == r.RETAIN

        # Terminal, so the work keys are gone: the record has left the queue by
        # physics rather than by filter.
        done = _kb(table)
        assert done["migrationState"] == r.RETAIN
        assert "GSI7_PK" not in done
        # The row left "Provisioning knowledge base…" before the indexing wait, so
        # the author sees an ordinary upload rather than a stuck one.
        assert _doc(table)["status"] == "uploading"

    @pytest.mark.asyncio
    async def test_a_document_deleted_while_provisioning_is_not_recreated(self, table):
        """The handoff's ``provisioning → uploading`` write is an upsert. A document
        deleted while its knowledge base was being built must not come back as a
        ghost ``uploading`` row, and there is nothing left to ingest it for."""
        _seed_doc(table)
        waiting = [_doc(table)]
        table.delete_item(Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{DOCUMENT_ID}"})

        with patch.object(ic, "handle_object") as handled:
            done = await pv._ingest_waiting(ASSISTANT_ID, waiting)

        assert done == 0
        handled.assert_not_called()
        assert _doc(table) is None

    @pytest.mark.asyncio
    async def test_more_documents_than_one_invocation_can_finish_are_requeued(self, table):
        """The in-process FALLBACK (no consumer configured) does one document per
        invocation: the worker's timeout is 15 minutes and one document's indexing
        budget is already 10.5, so a second could not finish and being killed
        mid-wait costs a whole dispatcher interval."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
                "migrationState": r.BORN_MANAGED,
                "migrationGeneration": 0,
            }
        )
        _seed_doc(table, document_id="DOC-a", createdAt="2026-09-10T00:00:00Z")
        _seed_doc(table, document_id="DOC-b", createdAt="2026-09-10T00:00:01Z")

        record = _kb(table)
        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            ic, "handle_object", return_value={}
        ) as handled:
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, record)

        assert handled.call_count == 1
        assert result.to_state == r.BORN_MANAGED
        # Still queued, so the dispatcher brings it back for the second document.
        assert _kb(table)["GSI7_PK"] == r.work_pk(r.BORN_MANAGED)

    @pytest.mark.asyncio
    async def test_provisioning_failure_falls_back_to_legacy(self, table):
        """MUTATION GUARD: drop the rollback and this fails with retrievalEngine
        still set — managed intent, no knowledge base, legacy skipping every upload
        and nothing left in the queue. The agent could never ingest again."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
                "migrationState": r.BORN_MANAGED,
                "migrationGeneration": 0,
                "GSI7_PK": r.work_pk(r.BORN_MANAGED),
                "GSI7_SK": "2026-09-10T00:00:00Z",
            }
        )
        _seed_doc(table, sizeBytes=OBJECT_BYTES)

        record = _kb(table)
        with patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb",
            new=AsyncMock(side_effect=RuntimeError("CreateKnowledgeBase refused")),
        ):
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, record)

        assert result.to_state == r.MIGRATION_FAILED

        after = _kb(table)
        # Legacy by ABSENCE again, byte-identical to a record that never tried — so
        # the next upload takes the legacy pipeline and simply works.
        assert "retrievalEngine" not in after
        assert after["rolledBackAt"]
        assert after["migrationState"] == r.MIGRATION_FAILED
        assert "GSI7_PK" not in after

        # The waiting document is failed with copy the author can act on, rather
        # than left spinning on a knowledge base that will never exist.
        document = _doc(table)
        assert document["status"] == "failed"
        assert "upload it again" in document["ingestionError"]

    @pytest.mark.asyncio
    async def test_a_record_already_rolled_back_is_closed_out_not_reprovisioned(
        self, table
    ):
        """A previous attempt fell back to legacy but was killed before clearing the
        work keys. Provisioning a knowledge base for a record that has stopped
        pointing at one would be a paid resource nothing uses."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "provisioningState": r.PROVISIONING,
                "migrationState": r.BORN_MANAGED,
                "migrationGeneration": 0,
                "GSI7_PK": r.work_pk(r.BORN_MANAGED),
                "GSI7_SK": "2026-09-10T00:00:00Z",
            }
        )
        record = _kb(table)
        with patch.object(pv, "_provision", new=AsyncMock()) as provision:
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, record)

        provision.assert_not_awaited()
        assert result.to_state == r.MIGRATION_FAILED
        assert "GSI7_PK" not in _kb(table)

    @pytest.mark.asyncio
    async def test_a_concurrent_provisioner_makes_this_one_step_aside(self, table):
        """Losing the provisioning race must NOT be read as a failure: rolling back
        to legacy while another worker is successfully building the knowledge base
        would undo good work."""
        table.put_item(
            Item={
                "PK": r.kb_pk(ASSISTANT_ID),
                "SK": r.kb_sk(ASSISTANT_ID),
                "appKbId": ASSISTANT_ID,
                "retrievalEngine": r.ENGINE_MANAGED,
                "provisioningState": r.PROVISIONING,
                "migrationState": r.BORN_MANAGED,
                "migrationGeneration": 0,
            }
        )
        from apis.shared.kb_backend.provisioning import ProvisioningInProgress

        record = _kb(table)
        with patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb",
            new=AsyncMock(side_effect=ProvisioningInProgress("someone else has it")),
        ):
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, record)

        assert result.to_state == r.BORN_MANAGED
        after = _kb(table)
        assert after["retrievalEngine"] == r.ENGINE_MANAGED
        assert after["GSI7_PK"] == r.work_pk(r.BORN_MANAGED)

    @pytest.mark.asyncio
    async def test_a_requeued_job_is_picked_up_by_the_very_next_tick(self, table):
        """B11 (dev, 2026-10-05), through the real lease. The lease is 15 minutes,
        the same as the dispatcher interval, and was never released, so the tick
        after a re-arm found it live and the job waited a second interval.

        Both invocations run back to back here, well inside the first lease's
        window. MUTATION GUARD: drop the release in run_step and the second one
        raises LeaseLost."""
        from apis.app_api.kb_migration import worker

        table.put_item(Item=_born_managed_record())
        _seed_doc(table, document_id="DOC-a", createdAt="2026-09-10T00:00:00Z")
        _seed_doc(table, document_id="DOC-b", createdAt="2026-09-10T00:00:01Z")

        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            ic, "handle_object", return_value={}
        ) as handled:
            first = await worker.run_step(ASSISTANT_ID, ASSISTANT_ID)
            assert first.to_state == r.BORN_MANAGED
            assert "migrationLeaseUntil" not in _kb(table)

            second = await worker.run_step(ASSISTANT_ID, ASSISTANT_ID)

        assert second.to_state == r.RETAIN
        assert handled.call_count == 2

    def test_pending_documents_only_sees_the_waiting_ones(self, table):
        _seed_doc(table, document_id="DOC-waiting")
        _seed_doc(table, document_id="DOC-done", status="complete")
        _seed_doc(table, document_id="DOC-gone", status="deleting")

        found = [d["documentId"] for d in pv.pending_documents(ASSISTANT_ID)]
        assert found == ["DOC-waiting"]


# ── 5b. Every waiting document goes to the ingestion consumer at once ──────
CONSUMER = "kb-ingestion-consumer-fn"


def _born_managed_record(**extra):
    item = {
        "PK": r.kb_pk(ASSISTANT_ID),
        "SK": r.kb_sk(ASSISTANT_ID),
        "appKbId": ASSISTANT_ID,
        "ownerUserId": OWNER,
        "retrievalEngine": r.ENGINE_MANAGED,
        "provisioningState": r.PROVISIONING,
        "migrationState": r.BORN_MANAGED,
        "migrationGeneration": 0,
        "GSI7_PK": r.work_pk(r.BORN_MANAGED),
        "GSI7_SK": "2026-09-10T00:00:00Z",
    }
    item.update(extra)
    return item


def _seed_waiting(table, count):
    ids = [f"DOC-{i}" for i in range(count)]
    for i, document_id in enumerate(ids):
        _seed_doc(
            table,
            document_id=document_id,
            s3Key=f"assistants/{ASSISTANT_ID}/documents/{document_id}/f{i}.pdf",
            createdAt=f"2026-09-10T00:00:0{i}Z",
        )
    return ids


class TestBacklogHandoff:
    """B5 (dev, 2026-10-05). A Shared Project seeds several files at once. One
    document per 15-minute tick made four files ready at +2.5, +31, ~+60 and
    ~+90 minutes, while a file uploaded after the knowledge base existed took 3
    minutes through the ingestion consumer. Once the knowledge base exists, a
    waiting document is just an ordinary upload whose event came too early."""

    @pytest.fixture()
    def consumer(self, monkeypatch):
        monkeypatch.setenv(pv.ENV_INGESTION_CONSUMER, CONSUMER)
        sent = []
        with patch.object(
            pv, "_invoke_consumer", side_effect=lambda fn, bucket, key: sent.append((fn, bucket, key))
        ):
            yield sent

    @pytest.mark.asyncio
    async def test_every_waiting_document_is_handed_off_in_one_invocation(self, table, consumer):
        """MUTATION GUARD: go back to one document per invocation and this fails:
        three are left waiting and the job re-arms instead of finishing."""
        table.put_item(Item=_born_managed_record())
        ids = _seed_waiting(table, 4)

        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            ic, "handle_object"
        ) as handled:
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, _kb(table))

        assert consumer == [
            (CONSUMER, BUCKET, f"assistants/{ASSISTANT_ID}/documents/{d}/f{i}.pdf")
            for i, d in enumerate(ids)
        ]
        # Nothing is ingested in-process: no 10-minute wait inside the worker.
        handled.assert_not_called()
        assert result.to_state == r.RETAIN
        assert result.documents_migrated == 4
        assert "GSI7_PK" not in _kb(table)
        assert [_doc(table, d)["status"] for d in ids] == ["uploading"] * 4

    def test_the_event_is_one_the_consumer_routes(self):
        """The handoff is only as good as the consumer's reading of it. Sent through
        the real invoke and parsed by the real ``extract_records``."""
        import json
        from unittest.mock import MagicMock

        client = MagicMock()
        with patch("boto3.client", return_value=client):
            pv._invoke_consumer(CONSUMER, BUCKET, KEY)

        kwargs = client.invoke.call_args.kwargs
        assert kwargs["FunctionName"] == CONSUMER
        # Asynchronous, so the consumer's own retries and DLQ apply.
        assert kwargs["InvocationType"] == "Event"
        assert ic.extract_records(json.loads(kwargs["Payload"])) == [
            {"bucket": BUCKET, "key": KEY}
        ]

    @pytest.mark.asyncio
    async def test_a_refused_invocation_falls_back_to_in_process_ingestion(self, table, monkeypatch):
        """A document the consumer could not be sent is not dropped and not
        stranded at ``uploading``: it stays ``provisioning`` and takes the old path."""
        monkeypatch.setenv(pv.ENV_INGESTION_CONSUMER, CONSUMER)
        table.put_item(Item=_born_managed_record())
        ids = _seed_waiting(table, 3)
        refused_key = f"assistants/{ASSISTANT_ID}/documents/{ids[1]}/f1.pdf"

        def _invoke(fn, bucket, key):
            if key == refused_key:
                raise RuntimeError("TooManyRequestsException")

        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            pv, "_invoke_consumer", side_effect=_invoke
        ), patch.object(ic, "handle_object", return_value={}) as handled:
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, _kb(table))

        handled.assert_called_once_with(BUCKET, refused_key)
        assert result.documents_migrated == 3
        assert result.to_state == r.RETAIN

    @pytest.mark.asyncio
    async def test_unsent_documents_past_the_fallback_limit_are_requeued(self, table, monkeypatch):
        monkeypatch.setenv(pv.ENV_INGESTION_CONSUMER, CONSUMER)
        table.put_item(Item=_born_managed_record())
        _seed_waiting(table, 3)

        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            pv, "_invoke_consumer", side_effect=RuntimeError("AccessDenied")
        ), patch.object(ic, "handle_object", return_value={}) as handled:
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, _kb(table))

        assert handled.call_count == 1
        assert result.to_state == r.BORN_MANAGED
        assert _kb(table)["GSI7_PK"] == r.work_pk(r.BORN_MANAGED)
        # The two left are still waiting, so the next tick finds them.
        assert len(pv.pending_documents(ASSISTANT_ID)) == 2

    @pytest.mark.asyncio
    async def test_a_document_whose_row_could_not_be_marked_is_still_handed_off(
        self, table, consumer
    ):
        """The consumer has it and writes the terminal state either way. Re-arming
        would only send it a second time."""
        table.put_item(Item=_born_managed_record())
        _seed_waiting(table, 2)

        with patch.object(pv, "_provision", new=AsyncMock(return_value=True)), patch.object(
            pv, "_mark_handed_off", side_effect=RuntimeError("ProvisionedThroughputExceeded")
        ):
            result = await pv.run_born_managed(ASSISTANT_ID, ASSISTANT_ID, _kb(table))

        assert len(consumer) == 2
        assert result.to_state == r.RETAIN

    @pytest.mark.parametrize("status", ["complete", "failed", "deleting"])
    def test_the_mark_never_drags_a_finished_row_back(self, table, status):
        """The consumer can fail a document in under a second, before this write
        lands. Overwriting that with ``uploading`` would leave it spinning forever."""
        _seed_doc(table, status=status)

        assert pv._mark_handed_off(ASSISTANT_ID, DOCUMENT_ID) is False
        assert _doc(table)["status"] == status

    def test_the_mark_does_not_recreate_a_deleted_row(self, table):
        assert pv._mark_handed_off(ASSISTANT_ID, DOCUMENT_ID) is False
        assert _doc(table) is None


# ── 6. The rollout ladder's rungs stay independent ───────────────────────────
class TestDispatcherFlagGating:
    def test_born_managed_is_swept_under_new_default_alone(self, monkeypatch):
        from apis.app_api.kb_migration import dispatcher as d

        monkeypatch.setenv("MANAGED_KB_NEW_DEFAULT", "true")
        monkeypatch.delenv("MANAGED_KB_MIGRATION_ENABLED", raising=False)

        assert d.dispatcher_enabled() is True
        assert d._enabled_work_states() == [r.BORN_MANAGED, r.TEARDOWN]

    def test_migration_states_are_not_swept_under_new_default_alone(self, monkeypatch):
        """MUTATION GUARD: gate the states on ``migration_enabled or
        new_default_enabled`` instead of each on its own flag, and this fails —
        turning on step 2 of the ladder would silently start migrating the entire
        existing fleet, which is step 3 and a different blast radius."""
        from apis.app_api.kb_migration import dispatcher as d

        monkeypatch.setenv("MANAGED_KB_NEW_DEFAULT", "true")
        monkeypatch.delenv("MANAGED_KB_MIGRATION_ENABLED", raising=False)

        swept = d._enabled_work_states()
        assert r.SHADOW not in swept
        assert r.VERIFY not in swept
        assert r.PROMOTE not in swept

    def test_born_managed_is_not_swept_under_migration_alone(self, monkeypatch):
        from apis.app_api.kb_migration import dispatcher as d

        monkeypatch.setenv("MANAGED_KB_MIGRATION_ENABLED", "true")
        monkeypatch.delenv("MANAGED_KB_NEW_DEFAULT", raising=False)

        swept = d._enabled_work_states()
        assert r.BORN_MANAGED not in swept
        assert swept == [r.TEARDOWN, r.PROMOTE, r.VERIFY, r.SHADOW]

    def test_born_managed_is_served_first(self, monkeypatch):
        """Somebody is watching an upload spinner for it; the migration states are
        background work."""
        from apis.app_api.kb_migration import dispatcher as d

        monkeypatch.setenv("MANAGED_KB_NEW_DEFAULT", "true")
        monkeypatch.setenv("MANAGED_KB_MIGRATION_ENABLED", "true")

        assert d._enabled_work_states()[0] == r.BORN_MANAGED

    @pytest.mark.asyncio
    async def test_tick_is_a_no_op_with_both_flags_off(self, monkeypatch):
        from apis.app_api.kb_migration import dispatcher as d

        monkeypatch.delenv("MANAGED_KB_NEW_DEFAULT", raising=False)
        monkeypatch.delenv("MANAGED_KB_MIGRATION_ENABLED", raising=False)

        with patch.object(d, "_invoke_worker") as invoke:
            counts = await d.dispatch_once()

        invoke.assert_not_called()
        assert counts == {"Due": 0, "Dispatched": 0, "Failed": 0}


# ── 7. The document reconciler backstop ──────────────────────────────────────
def test_provisioning_is_a_reconciler_candidate():
    """The long-horizon backstop for a first document whose provisioning job was
    killed between building the knowledge base and handing the document over. Safe
    to include because the sweep already skips any record with no awsKbId, so a
    document is never probed against a knowledge base that does not exist."""
    from apis.app_api.kb_migration.document_reconciler import NON_TERMINAL_STATUSES

    assert bm.STATUS_PROVISIONING in NON_TERMINAL_STATUSES
    assert "complete" not in NON_TERMINAL_STATUSES
    assert "deleting" not in NON_TERMINAL_STATUSES


def test_the_two_halves_agree_on_the_status_string():
    """The trigger writes it, the consumer defers on it, the provisioner clears it.
    They live in different Lambda images, so the constant is defined once and
    re-exported rather than spelled twice."""
    assert bm.STATUS_PROVISIONING == ic.STATUS_PROVISIONING == "provisioning"


# ── 8. The existing-documents existence probe ────────────────────────────────
class TestAssistantHasDocuments:
    """The cheap COUNT probe born-managed uses to tell a new agent apart from an
    established legacy one. It must not consult ownership (the caller already
    authorised the upload) and must fail toward 'has documents' so uncertainty
    keeps an agent on legacy rather than provisioning over its corpus."""

    @pytest.mark.asyncio
    async def test_true_when_a_document_row_exists(self, table):
        from apis.app_api.documents.services.document_service import (
            assistant_has_documents,
        )

        _seed_doc(table, document_id="DOC-x", status="complete")
        assert await assistant_has_documents(ASSISTANT_ID) is True

    @pytest.mark.asyncio
    async def test_false_when_no_document_rows_exist(self, table):
        from apis.app_api.documents.services.document_service import (
            assistant_has_documents,
        )

        # A KB_Record is not a DOC# row: a record-only agent still counts as empty.
        table.put_item(Item={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_sk(ASSISTANT_ID)})
        assert await assistant_has_documents(ASSISTANT_ID) is False

    @pytest.mark.asyncio
    async def test_fails_toward_legacy_when_the_table_is_unconfigured(self, monkeypatch):
        """No table name means the probe cannot answer — it returns True so
        born-managed stays on legacy rather than provisioning blind."""
        from apis.app_api.documents.services.document_service import (
            assistant_has_documents,
        )

        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME", raising=False)
        assert await assistant_has_documents(ASSISTANT_ID) is True
