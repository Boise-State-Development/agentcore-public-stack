"""A synced source that changed must be re-ingested into its MANAGED knowledge base.

The KB sync worker (``kb_sync/worker.py``) detects a changed Drive file and
overwrites the document's existing S3 object, relying on the bucket's
``ObjectCreated`` event to re-run ingestion. That works for the legacy pipeline.
For a managed knowledge base the event reaches the ingestion consumer, which found
a ``complete`` row with ``byteCapSettled`` and returned "already-settled" — the
same early exit that makes EventBridge redelivery idempotent. The new bytes were
never ingested, and the size change never reached the byte ledger.

These tests drive the real path end to end against moto: the real sync worker
(Drive and the token vault stubbed at its seams) stages to a real S3 object, and
the real consumer handles the resulting event, against a fake Bedrock that models
document status the way the service reports it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest

from apis.app_api.documents.models import DocumentProvenance
from apis.app_api.documents.services.document_service import create_document, soft_delete_document
from apis.app_api.file_sources.models import DownloadedFile
from apis.app_api.kb_migration import ingestion_consumer as ic
from apis.app_api.kb_sync import worker
from apis.shared.assistants.service import create_assistant
from apis.shared.sync_policies.service import create_sync_policy

REGION = "us-east-1"
BUCKET = "test-reingest-docs"
USER_ID = "user-reingest"
FILE_ID = "drive-file-reingest"
CONNECTOR_ID = "google-workspace"
DOCUMENT_ID = "doc-reingest"
FILENAME = "report.pdf"


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeBedrock:
    """ManagedKbBackend's surface, modelling per-document status like the service.

    ``ingest`` reads the object as it is in S3 at submission — Bedrock ingests from
    the S3 location — and re-ingesting an id replaces the document. A submitted
    document reports ``IN_PROGRESS`` on the next probe, and ``INDEXED`` (or
    ``FAILED``) on the one after, stamped with the time it flipped. While ``hold``
    is set it stays ``IN_PROGRESS``. ``stale_probes`` makes the probes straight
    after a submit still report the PREVIOUS version's status and timestamp, which
    is what a document Bedrock already holds can look like.
    """

    def __init__(self, stale_probes=0, fail=False):
        self.ingests: list = []
        self.indexed_content = None
        self.hold = False
        self.fail = fail
        self.stale_probes = stale_probes
        self._status = "NOT_FOUND"
        self._updated_at = None
        self._pending = None
        self._probes_until_flip = 0
        self._stale_left = 0
        self._agent_client = SimpleNamespace(get_knowledge_base_documents=self._get_documents)

    # -- the private surface `document_status` reuses --------------------------
    def _agent(self):
        return self._agent_client

    def _locate(self, kb_ref):
        return ("KB123", "DS456")

    def _get_documents(self, **kwargs):
        if self._stale_left:
            self._stale_left -= 1
        elif self._pending is not None:
            if self.hold or self._probes_until_flip:
                self._probes_until_flip = max(self._probes_until_flip - 1, 0)
                self._status = "IN_PROGRESS"
                self._updated_at = datetime.now(timezone.utc)
            else:
                self._status = "FAILED" if self.fail else "INDEXED"
                self._updated_at = datetime.now(timezone.utc)
                if not self.fail:
                    self.indexed_content = self._pending
                self._pending = None
        if self._status == "NOT_FOUND":
            return {"documentDetails": []}
        return {"documentDetails": [{"status": self._status, "updatedAt": self._updated_at}]}

    # -- the protocol surface --------------------------------------------------
    async def ingest(self, kb_ref, source):
        body = boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key=source.s3_key)
        self._pending = body["Body"].read()
        self.ingests.append(self._pending)
        self._probes_until_flip = 1
        self._stale_left = self.stale_probes

    async def search(self, kb_ref, query, top_k=5, retrieval_filter=None):
        if self.indexed_content is None:
            return []
        chunk = MagicMock()
        chunk.metadata = {"document_id": DOCUMENT_ID}
        return [chunk]


class FakeDriveAdapter:
    def __init__(self, version, content):
        self.version = version
        self.content = content

    async def get_file_metadata(self, access_token, file_id):
        return {"version": self.version, "trashed": False}

    async def download(self, access_token, file_id):
        return DownloadedFile(content=self.content, filename=FILENAME, content_type="application/pdf")


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(ic, "INDEXED_POLL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(ic, "INDEXED_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(ic, "RETRIEVABLE_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ic, "RETRIEVABLE_POLL_INTERVAL_SECONDS", 0.001)


@pytest.fixture()
def drive(monkeypatch):
    """The worker's seams to Google and the vault. Returns a setter for the file."""
    provider = SimpleNamespace(provider_id=CONNECTOR_ID, scopes=[], custom_parameters=None)

    async def fake_get_provider(self, provider_id):
        return provider if provider_id == CONNECTOR_ID else None

    async def fake_resolve(provider, user_id):
        return "test-access-token"

    monkeypatch.setattr(
        "apis.shared.oauth.provider_repository.OAuthProviderRepository.get_provider", fake_get_provider
    )
    monkeypatch.setattr(worker, "_resolve_access_token", fake_resolve)

    def set_file(version, content):
        adapter = FakeDriveAdapter(version, content)
        monkeypatch.setattr(worker.registry, "get", lambda key: adapter if key == "google-drive" else None)

    return set_file


@pytest.fixture()
def kb(assistants_table, monkeypatch):
    """A managed agent holding one Drive-imported document with a daily sync policy."""
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", BUCKET)
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)

    async def _build():
        assistant = await create_assistant(
            owner_id=USER_ID, owner_name="U", name="A", description="d",
            instructions="i", vector_index_id="assistants-index",
        )
        assistant_id = assistant.assistant_id
        # Imports reserve nothing at request time, so the row declares no size.
        document = await create_document(
            assistant_id=assistant_id,
            filename=FILENAME,
            content_type="application/pdf",
            size_bytes=0,
            s3_key=f"assistants/{assistant_id}/documents/{DOCUMENT_ID}/{FILENAME}",
            document_id=DOCUMENT_ID,
            provenance=DocumentProvenance(
                source_connector_id=CONNECTOR_ID,
                source_adapter_key="google-drive",
                source_file_id=FILE_ID,
                imported_by_user_id=USER_ID,
                source_etag="1",
            ),
        )
        policy = await create_sync_policy(
            assistant_id=assistant_id, source_type="drive_file", source_ref=DOCUMENT_ID,
            interval="daily", created_by_user_id=USER_ID,
        )
        return assistant_id, document, policy

    assistant_id, document, policy = asyncio.run(_build())
    return SimpleNamespace(
        table=assistants_table, assistant_id=assistant_id, key=document.s3_key, policy=policy
    )


def _seed_kb_record(kb, engine="managed", **counters):
    item = {
        "PK": f"AST#{kb.assistant_id}",
        "SK": f"KB#{kb.assistant_id}",
        "appKbId": kb.assistant_id,
        "ownerUserId": USER_ID,
        "awsKbId": "KB123",
        "awsDataSourceId": "DS456",
    }
    if engine:
        item["retrievalEngine"] = engine
    item.update({k: Decimal(v) for k, v in counters.items()})
    kb.table.put_item(Item=item)


def _put(kb, content):
    boto3.client("s3", region_name=REGION).put_object(Bucket=BUCKET, Key=kb.key, Body=content)


def _event(kb, bedrock):
    """The ObjectCreated event the S3 overwrite produces, handled by the consumer."""
    with patch("apis.shared.kb_backend.managed_backend.ManagedKbBackend", return_value=bedrock):
        return ic.handle_object(BUCKET, kb.key)


def _sync(kb):
    return asyncio.run(
        worker.run_sync({
            "policyId": kb.policy.policy_id,
            "assistantId": kb.assistant_id,
            "sourceType": "drive_file",
            "sourceRef": DOCUMENT_ID,
        })
    )


def _doc(kb):
    return kb.table.get_item(Key={"PK": f"AST#{kb.assistant_id}", "SK": f"DOC#{DOCUMENT_ID}"}).get("Item")


def _counters(kb):
    item = kb.table.get_item(Key={"PK": f"AST#{kb.assistant_id}", "SK": f"KB#{kb.assistant_id}"}).get("Item")
    return tuple(int((item or {}).get(k) or 0) for k in ("storedBytes", "reservedBytes", "totalBytes"))


def _delete(kb):
    with patch(
        "apis.shared.assistants.service.get_assistant",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(assistant_id=kb.assistant_id, owner_id=USER_ID),
    ):
        asyncio.run(soft_delete_document(kb.assistant_id, DOCUMENT_ID, USER_ID))


def _imported_and_complete(kb, bedrock, content=b"a" * 1000):
    """The import's own first ingestion, to ``complete`` with its bytes committed."""
    _seed_kb_record(kb)
    _put(kb, content)
    result = _event(kb, bedrock)
    assert result["ingested"] is True
    assert _doc(kb)["status"] == "complete"
    assert _counters(kb) == (len(content), 0, len(content))


# ── the defect ───────────────────────────────────────────────────────────────
class TestAChangedFileIsReingested:
    def test_the_new_bytes_reach_the_knowledge_base(self, kb, drive):
        """The repro. MUTATION GUARD: without the re-ingest branch the consumer
        returns "already-settled", Bedrock is never asked, and it keeps serving the
        version the file had at import."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)

        drive("2", b"b" * 1500)
        assert _sync(kb)["result"] == "changed"
        result = _event(kb, bedrock)

        assert result.get("note") != "already-settled"
        assert bedrock.ingests == [b"a" * 1000, b"b" * 1500]
        assert bedrock.indexed_content == b"b" * 1500
        assert _doc(kb)["status"] == "complete"

    def test_the_ledger_grows_by_the_size_delta(self, kb, drive):
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)

        drive("2", b"b" * 1500)
        _sync(kb)
        _event(kb, bedrock)

        assert _counters(kb) == (1500, 0, 1500)
        assert _doc(kb)["committedBytes"] == 1500

    def test_the_ledger_shrinks_by_the_size_delta(self, kb, drive):
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)

        drive("2", b"c" * 400)
        _sync(kb)
        _event(kb, bedrock)

        assert bedrock.indexed_content == b"c" * 400
        assert _counters(kb) == (400, 0, 400)
        assert _doc(kb)["committedBytes"] == 400

    def test_a_delete_after_the_reingest_refunds_the_new_size(self, kb, drive):
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)
        _event(kb, bedrock)

        _delete(kb)

        assert _counters(kb) == (0, 0, 0)


# ── idempotency under EventBridge redelivery ─────────────────────────────────
class TestRedeliveryIsIdempotent:
    def test_a_redelivery_after_the_reingest_does_nothing(self, kb, drive):
        """The early exit the defect came from still does its job: once the new
        version is recorded as ingested, its event is a plain redelivery."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)
        _event(kb, bedrock)

        result = _event(kb, bedrock)

        assert result["note"] == "already-settled"
        assert len(bedrock.ingests) == 2
        assert _counters(kb) == (1500, 0, 1500)

    def test_a_redelivery_while_indexing_neither_resubmits_nor_rereserves(self, kb, drive):
        """Lambda's async retry is capped at 2, so a slow re-ingest spans
        deliveries. Re-submitting would restart Bedrock's work (the dev failure
        the first-ingest path documents); re-reserving would leak the growth."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)

        bedrock.hold = True
        for _ in range(2):
            with pytest.raises(ic.IngestionRoutingError):
                _event(kb, bedrock)
            assert _counters(kb) == (1000, 500, 1500)
        assert _doc(kb)["status"] == "complete", "the previous version stops being served"

        bedrock.hold = False
        result = _event(kb, bedrock)

        assert result["note"] == "reingested"
        assert len(bedrock.ingests) == 2
        assert _counters(kb) == (1500, 0, 1500)
        assert not any(k.startswith("reingest") for k in _doc(kb))

    def test_a_status_from_before_the_submit_is_not_the_new_version(self, kb, drive):
        """Bedrock already holds this id, so the probes straight after a
        re-submit can still describe the previous version. MUTATION GUARD: drop
        ``not_before`` and the consumer records the new version as ingested while
        Bedrock is still serving the old one."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)

        bedrock.stale_probes = 3
        _event(kb, bedrock)

        assert bedrock.indexed_content == b"b" * 1500
        assert _doc(kb)["ingestedContentHash"] == worker._sha256(b"b" * 1500)

    def test_a_newer_change_supersedes_an_unfinished_reingest(self, kb, drive):
        """Two changes, the first never finishing: its growth reservation is
        returned, the newer version is submitted, and only its size is counted."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)
        bedrock.hold = True
        with pytest.raises(ic.IngestionRoutingError):
            _event(kb, bedrock)
        assert _counters(kb) == (1000, 500, 1500)

        drive("3", b"c" * 1200)
        _sync(kb)
        bedrock.hold = False
        result = _event(kb, bedrock)

        assert result["note"] == "reingested"
        assert bedrock.ingests[-1] == b"c" * 1200
        assert bedrock.indexed_content == b"c" * 1200
        assert _counters(kb) == (1200, 0, 1200)
        assert _event(kb, bedrock)["note"] == "already-settled"


# ── the byte cap ─────────────────────────────────────────────────────────────
class TestTheByteCapGatesTheNewVersion:
    @pytest.fixture()
    def cap_1200(self, monkeypatch):
        monkeypatch.setenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", "1200")
        monkeypatch.setenv("MANAGED_KB_PER_KB_CEILING_BYTES", "1200")

    def test_growth_past_the_cap_keeps_the_previous_version(self, kb, drive, cap_1200):
        """Checked BEFORE submitting: once Bedrock has the new bytes the old
        version is gone, and failing then would leave the owner with nothing."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)

        result = _event(kb, bedrock)

        assert result["note"] == "byte-cap-exceeded"
        assert bedrock.ingests == [b"a" * 1000]
        assert bedrock.indexed_content == b"a" * 1000
        doc = _doc(kb)
        assert doc["status"] == "complete"
        assert doc["committedBytes"] == 1000
        assert "storage limit" in doc["ingestionError"]
        assert _counters(kb) == (1000, 0, 1000)

    def test_the_next_sync_retries_once_there_is_room(self, kb, drive, cap_1200, monkeypatch):
        """The refused version is re-staged by the next sync run (its gates were
        cleared) and goes through once the owner has room."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)
        _event(kb, bedrock)

        assert _sync(kb)["result"] == "changed", "the refused version is never retried"

        monkeypatch.setenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", "2000")
        monkeypatch.setenv("MANAGED_KB_PER_KB_CEILING_BYTES", "2000")
        result = _event(kb, bedrock)

        assert result["note"] == "reingested"
        assert _counters(kb) == (1500, 0, 1500)
        assert "ingestionError" not in _doc(kb)

    def test_shrinking_is_never_refused(self, kb, drive, cap_1200):
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock, content=b"a" * 1200)

        drive("2", b"b" * 900)
        _sync(kb)

        assert _event(kb, bedrock)["note"] == "reingested"
        assert _counters(kb) == (900, 0, 900)


# ── terminal outcomes ────────────────────────────────────────────────────────
class TestTerminalOutcomes:
    def test_a_delete_mid_reingest_returns_every_byte(self, kb, drive):
        """The delete refunds the committed old size and releases the growth
        reserved for the new one; the consumer, finding the row deleting, touches
        neither."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)
        bedrock.hold = True
        with pytest.raises(ic.IngestionRoutingError):
            _event(kb, bedrock)

        _delete(kb)
        assert _counters(kb) == (0, 0, 0)

        bedrock.hold = False
        assert _event(kb, bedrock)["note"] == "document-deleted"
        assert _counters(kb) == (0, 0, 0)

    def test_bedrock_failing_the_new_version_releases_its_growth(self, kb, drive):
        """The document is failed like any other Bedrock failure. Its old size
        stays committed — the object is still stored — so a delete refunds it."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)
        drive("2", b"b" * 1500)
        _sync(kb)

        bedrock.fail = True
        result = _event(kb, bedrock)

        assert result["status"] == "FAILED"
        assert _doc(kb)["status"] == "failed"
        assert _counters(kb) == (1000, 0, 1000)

        _delete(kb)
        assert _counters(kb) == (0, 0, 0)

    def test_a_failed_document_recovers_on_a_changed_source(self, kb, drive):
        """New bytes deserve a fresh attempt. A failed import reserved and
        committed nothing, so its whole new size is reserved and committed."""
        bedrock = FakeBedrock(fail=True)
        _seed_kb_record(kb)
        _put(kb, b"a" * 1000)
        _event(kb, bedrock)
        assert _doc(kb)["status"] == "failed"
        assert _counters(kb) == (0, 0, 0)

        bedrock.fail = False
        drive("2", b"b" * 1500)
        _sync(kb)
        result = _event(kb, bedrock)

        assert result["note"] == "reingested"
        assert _doc(kb)["status"] == "complete"
        assert _doc(kb)["committedBytes"] == 1500
        assert _counters(kb) == (1500, 0, 1500)

    def test_an_unsynced_redelivery_keeps_the_settled_early_exit(self, kb):
        """No staged version, no change in behaviour."""
        bedrock = FakeBedrock()
        _imported_and_complete(kb, bedrock)

        assert _event(kb, bedrock)["note"] == "already-settled"
        assert len(bedrock.ingests) == 1


# ── legacy ───────────────────────────────────────────────────────────────────
class TestTheLegacyPathIsUnaffected:
    def test_a_legacy_kb_is_left_to_its_own_pipeline(self, kb, drive):
        """The marker is written regardless of engine; the consumer still routes
        a legacy document away before reading it, and counts nothing."""
        _seed_kb_record(kb, engine=None)
        _put(kb, b"a" * 1000)
        bedrock = FakeBedrock()

        drive("2", b"b" * 1500)
        assert _sync(kb)["result"] == "changed"
        result = _event(kb, bedrock)

        assert result["routed"] == "legacy"
        assert bedrock.ingests == []
        doc = _doc(kb)
        assert doc["stagedContentHash"] == worker._sha256(b"b" * 1500)
        assert doc["previousChunkCount"] == 0, "the legacy shrinkage stash is unchanged"
        assert _counters(kb) == (0, 0, 0)
