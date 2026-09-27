"""KB sync worker tests — Drive-file path (moto DynamoDB, stubbed Drive/token).

Documents are created through the REAL document service (with import
provenance) so the worker's raw record reads/writes are cross-checked
against the actual storage schema. The Drive adapter and token
resolution are stubbed at the worker's seams.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apis.app_api.documents.models import DocumentProvenance
from apis.app_api.documents.services.document_service import create_document
from apis.app_api.file_sources.models import (
    DownloadedFile,
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
)
from apis.app_api.kb_sync import worker
from apis.shared.assistants.service import create_assistant
from apis.shared.sync_policies.service import create_sync_policy, get_sync_policy

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
FILE_ID = "drive-file-1"
CONNECTOR_ID = "google-workspace"


class FakeDriveAdapter:
    """Stub with the two methods the worker uses."""

    def __init__(self, metadata=None, content=b"", metadata_error=None, download_error=None):
        self.metadata = metadata or {}
        self.content = content
        self.metadata_error = metadata_error
        self.download_error = download_error
        self.download_calls = 0

    async def get_file_metadata(self, access_token, file_id):
        if self.metadata_error:
            raise self.metadata_error
        return self.metadata

    async def download(self, access_token, file_id):
        self.download_calls += 1
        if self.download_error:
            raise self.download_error
        return DownloadedFile(content=self.content, filename="report.pdf", content_type="application/pdf")


@pytest.fixture()
def staged(monkeypatch):
    """Capture S3 staging calls."""
    calls = []
    monkeypatch.setattr(worker, "_stage_to_s3", lambda *args: calls.append(args))
    return calls


@pytest.fixture()
def token_ok(monkeypatch):
    async def fake_resolve(provider, user_id):
        return "test-access-token"

    monkeypatch.setattr(worker, "_resolve_access_token", fake_resolve)


@pytest.fixture()
def provider_ok(monkeypatch):
    provider = SimpleNamespace(
        provider_id=CONNECTOR_ID,
        provider_type=SimpleNamespace(value="google"),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
        custom_parameters=None,
    )

    async def fake_get_provider(self, provider_id):
        return provider if provider_id == CONNECTOR_ID else None

    monkeypatch.setattr(
        "apis.shared.oauth.provider_repository.OAuthProviderRepository.get_provider", fake_get_provider
    )
    return provider


def _use_adapter(monkeypatch, adapter):
    monkeypatch.setattr(worker.registry, "get", lambda key: adapter if key == "google-drive" else None)


async def _setup(assistants_table, *, etag="41", content_hash=None, chunk_count=7):
    assistant = await create_assistant(
        owner_id=USER_ID, owner_name="U", name="A", description="d",
        instructions="i", vector_index_id="assistants-index",
    )
    assistant_id = assistant.assistant_id
    doc = await create_document(
        assistant_id=assistant_id,
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=10,
        s3_key=f"assistants/{assistant_id}/documents/doc-1/report.pdf",
        document_id="doc-1",
        provenance=DocumentProvenance(
            source_connector_id=CONNECTOR_ID,
            source_adapter_key="google-drive",
            source_file_id=FILE_ID,
            imported_by_user_id=USER_ID,
            source_etag=etag,
        ),
    )
    extra = {":cc": chunk_count}
    expression = "SET chunkCount = :cc"
    if content_hash:
        expression += ", contentHash = :ch"
        extra[":ch"] = content_hash
    assistants_table.update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"},
        UpdateExpression=expression,
        ExpressionAttributeValues=extra,
    )
    policy = await create_sync_policy(
        assistant_id=assistant_id, source_type="drive_file", source_ref="doc-1",
        interval="daily", created_by_user_id=USER_ID,
    )
    return assistant_id, doc, policy


def _payload(assistant_id, policy):
    return {
        "policyId": policy.policy_id,
        "assistantId": assistant_id,
        "sourceType": "drive_file",
        "sourceRef": "doc-1",
    }


class TestChangeDetection:
    async def test_unchanged_etag_skips_download(self, assistants_table, staged, token_ok, provider_ok, monkeypatch):
        adapter = FakeDriveAdapter(metadata={"version": "41", "trashed": False})
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table, etag="41")

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "unchanged"
        assert adapter.download_calls == 0
        assert staged == []

    async def test_same_bytes_advances_etag_without_staging(
        self, assistants_table, staged, token_ok, provider_ok, monkeypatch
    ):
        content = b"same bytes"
        adapter = FakeDriveAdapter(metadata={"version": "42", "trashed": False}, content=content)
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(
            assistants_table, etag="41", content_hash=worker._sha256(content)
        )

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "unchanged"
        assert adapter.download_calls == 1
        assert staged == []
        doc = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"})["Item"]
        assert doc["sourceEtag"] == "42"  # gate 1 passes next run
        assert "stagedContentHash" not in doc  # nothing staged, nothing to re-ingest

    async def test_changed_bytes_staged_with_stash(self, assistants_table, staged, token_ok, provider_ok, monkeypatch):
        adapter = FakeDriveAdapter(metadata={"version": "42", "trashed": False}, content=b"new bytes")
        _use_adapter(monkeypatch, adapter)
        assistant_id, doc, policy = await _setup(assistants_table, etag="41", chunk_count=7)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "changed"
        assert len(staged) == 1
        s3_key, content, content_type = staged[0]
        assert s3_key == doc.s3_key
        assert content == b"new bytes"
        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"})["Item"]
        assert item["sourceEtag"] == "42"
        assert item["contentHash"] == worker._sha256(b"new bytes")
        assert item["previousChunkCount"] == 7
        # Marks the version a managed KB's consumer still has to re-ingest.
        assert item["stagedContentHash"] == worker._sha256(b"new bytes")
        assert "lastSyncedAt" in item
        updated_policy = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated_policy.last_result == "changed"
        assert updated_policy.consecutive_failures == 0

    async def test_trashed_is_grace_skip(self, assistants_table, staged, token_ok, provider_ok, monkeypatch):
        adapter = FakeDriveAdapter(metadata={"version": "42", "trashed": True})
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "skipped"
        assert adapter.download_calls == 0


class TestStageFailure:
    """A failed S3 stage must not leave the change-detection gates advanced —
    otherwise the next run matches them and the change is never staged."""

    @pytest.fixture()
    def flaky_stage(self, monkeypatch):
        """First stage raises (S3 put failure), later ones succeed."""
        calls = []

        def stage(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("S3 PutObject failed")

        monkeypatch.setattr(worker, "_stage_to_s3", stage)
        return calls

    def _doc(self, assistants_table, assistant_id):
        return assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"})["Item"]

    async def test_failed_stage_is_restaged_next_run(
        self, assistants_table, flaky_stage, token_ok, provider_ok, monkeypatch
    ):
        adapter = FakeDriveAdapter(metadata={"version": "42", "trashed": False}, content=b"new bytes")
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table, etag="41", chunk_count=7)

        first = await worker.run_sync(_payload(assistant_id, policy))

        assert first["result"] == "failed"
        item = self._doc(assistants_table, assistant_id)
        assert item["sourceEtag"] == "41"
        assert "contentHash" not in item
        assert "stagedContentHash" not in item
        assert "lastSyncedAt" not in item

        # Same Drive version, same bytes: both gates must still miss.
        second = await worker.run_sync(_payload(assistant_id, policy))

        assert second["result"] == "changed"
        assert len(flaky_stage) == 2
        assert flaky_stage[1][1] == b"new bytes"
        item = self._doc(assistants_table, assistant_id)
        assert item["sourceEtag"] == "42"
        assert item["contentHash"] == worker._sha256(b"new bytes")
        assert item["stagedContentHash"] == worker._sha256(b"new bytes")

    async def test_failed_stage_restores_previous_sync_values(
        self, assistants_table, flaky_stage, token_ok, provider_ok, monkeypatch
    ):
        adapter = FakeDriveAdapter(metadata={"version": "42", "trashed": False}, content=b"new bytes")
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table, etag="41", content_hash="old-hash")
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"},
            UpdateExpression="SET stagedContentHash = :h, lastSyncedAt = :t",
            ExpressionAttributeValues={":h": "old-hash", ":t": "2026-01-01T00:00:00Z"},
        )

        await worker.run_sync(_payload(assistant_id, policy))

        item = self._doc(assistants_table, assistant_id)
        assert item["sourceEtag"] == "41"
        assert item["contentHash"] == "old-hash"
        assert item["stagedContentHash"] == "old-hash"
        assert item["lastSyncedAt"] == "2026-01-01T00:00:00Z"

    async def test_rollback_leaves_a_newer_write_alone(self, assistants_table):
        from apis.app_api.kb_sync import records

        assistant_id, _, _ = await _setup(assistants_table, etag="41")
        written = {"sourceEtag": "42", "contentHash": "h2", "stagedContentHash": "h2"}
        records.update_document_sync_fields(
            assistant_id, "doc-1", source_etag="42", content_hash="h2", staged_content_hash="h2"
        )
        # A later run staged a newer version before this rollback landed.
        records.update_document_sync_fields(
            assistant_id, "doc-1", source_etag="43", content_hash="h3", staged_content_hash="h3"
        )

        rolled_back = records.rollback_document_sync_fields(
            assistant_id, "doc-1", written=written, previous={"sourceEtag": "41"}
        )

        assert rolled_back is False
        item = self._doc(assistants_table, assistant_id)
        assert (item["sourceEtag"], item["contentHash"], item["stagedContentHash"]) == ("43", "h3", "h3")

    async def test_rollback_does_not_resurrect_gates_the_consumer_cleared(self, assistants_table):
        """The managed consumer's over-cap refusal REMOVEs the gates to force a
        re-stage; a late rollback must not write them back."""
        from apis.app_api.kb_sync import records

        assistant_id, _, _ = await _setup(assistants_table, etag="41", content_hash="h1")
        records.update_document_sync_fields(
            assistant_id, "doc-1", source_etag="42", content_hash="h2", staged_content_hash="h2"
        )
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"},
            UpdateExpression="REMOVE sourceEtag, contentHash",
        )

        rolled_back = records.rollback_document_sync_fields(
            assistant_id,
            "doc-1",
            written={"sourceEtag": "42", "contentHash": "h2", "stagedContentHash": "h2"},
            previous={"sourceEtag": "41", "contentHash": "h1"},
        )

        assert rolled_back is False
        item = self._doc(assistants_table, assistant_id)
        assert "sourceEtag" not in item and "contentHash" not in item


class TestFailureModes:
    async def test_not_found_strikes_counter(self, assistants_table, staged, token_ok, provider_ok, monkeypatch):
        adapter = FakeDriveAdapter(metadata_error=FileSourceNotFoundError("gone or unshared"))
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "failed"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.consecutive_not_found == 1
        assert updated.consecutive_failures == 1
        assert updated.state == "active"  # dispatcher pauses at the 2-strike breaker

    async def test_auth_error_pauses_reauth(self, assistants_table, staged, token_ok, provider_ok, monkeypatch):
        adapter = FakeDriveAdapter(metadata_error=FileSourceAuthError("401 invalid token"))
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "paused_reauth"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_reauth"
        assert "Reconnect" in updated.state_reason
        assert updated.consecutive_failures == 0  # reauth is not a failure streak
        assert updated.sync_run_started_at is None
        # Marker lets consent completion find this policy without a scan
        marker = assistants_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SYNCREAUTH#{policy.policy_id}"}
        ).get("Item")
        assert marker is not None
        assert marker["providerId"] == CONNECTOR_ID
        assert marker["assistantId"] == assistant_id

    async def test_requires_consent_pauses_reauth(self, assistants_table, staged, provider_ok, monkeypatch):
        async def no_token(provider, user_id):
            return None

        monkeypatch.setattr(worker, "_resolve_access_token", no_token)
        adapter = FakeDriveAdapter(metadata={"version": "42"})
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "paused_reauth"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_reauth"
        marker = assistants_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SYNCREAUTH#{policy.policy_id}"}
        ).get("Item")
        assert marker is not None
        assert marker["providerId"] == CONNECTOR_ID

    async def test_missing_provider_pauses_error(self, assistants_table, staged, token_ok, monkeypatch):
        async def no_provider(self, provider_id):
            return None

        monkeypatch.setattr(
            "apis.shared.oauth.provider_repository.OAuthProviderRepository.get_provider", no_provider
        )
        _use_adapter(monkeypatch, FakeDriveAdapter())
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "skipped"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_error"
        assert "connector" in updated.state_reason

    async def test_drive_error_records_failed(self, assistants_table, staged, token_ok, provider_ok, monkeypatch):
        adapter = FakeDriveAdapter(metadata_error=FileSourceError("500 backend error"))
        _use_adapter(monkeypatch, adapter)
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "failed"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.consecutive_failures == 1
        assert updated.consecutive_not_found == 0

    async def test_unexpected_exception_still_records_run(
        self, assistants_table, staged, token_ok, provider_ok, monkeypatch
    ):
        class ExplodingAdapter(FakeDriveAdapter):
            async def get_file_metadata(self, access_token, file_id):
                raise RuntimeError("boom")

        _use_adapter(monkeypatch, ExplodingAdapter())
        assistant_id, _, policy = await _setup(assistants_table)

        result = await worker.run_sync(_payload(assistant_id, policy))

        assert result["result"] == "failed"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.sync_run_started_at is None  # stamp always clears

    async def test_missing_policy_drops_run(self, assistants_table):
        result = await worker.run_sync(
            {"policyId": "syn-missing", "assistantId": "ast-x", "sourceType": "drive_file", "sourceRef": "doc-1"}
        )
        assert result["result"] == "dropped"

    async def test_web_crawl_still_skips(self, assistants_table):
        assistant = await create_assistant(
            owner_id=USER_ID, owner_name="U", name="A", description="d",
            instructions="i", vector_index_id="assistants-index",
        )
        policy = await create_sync_policy(
            assistant_id=assistant.assistant_id, source_type="web_crawl", source_ref="crawl-1",
            interval="daily", created_by_user_id=USER_ID,
        )

        result = await worker.run_sync(
            {"policyId": policy.policy_id, "assistantId": assistant.assistant_id,
             "sourceType": "web_crawl", "sourceRef": "crawl-1"}
        )

        assert result["result"] == "skipped"


class TestShrinkageCleanup:
    async def test_pop_previous_chunk_count_is_atomic(self, assistants_table, monkeypatch):
        from apis.app_api.documents.ingestion.status import DocumentStatusManager

        assistant = await create_assistant(
            owner_id=USER_ID, owner_name="U", name="A", description="d",
            instructions="i", vector_index_id="assistants-index",
        )
        assistant_id = assistant.assistant_id
        await create_document(
            assistant_id=assistant_id, filename="f.pdf", content_type="application/pdf",
            size_bytes=1, s3_key="k", document_id="doc-1",
        )
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"},
            UpdateExpression="SET previousChunkCount = :p",
            ExpressionAttributeValues={":p": 9},
        )

        manager = DocumentStatusManager(table_name=assistants_table.table_name)
        first = await manager.pop_previous_chunk_count(assistant_id=assistant_id, document_id="doc-1")
        second = await manager.pop_previous_chunk_count(assistant_id=assistant_id, document_id="doc-1")

        assert first == 9
        assert second is None
        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-1"})["Item"]
        assert "previousChunkCount" not in item

    async def test_delete_vector_tail_sends_exact_range(self, monkeypatch):
        from apis.shared.embeddings import bedrock_embeddings

        deleted_batches = []

        class FakeClient:
            def delete_vectors(self, vectorBucketName, indexName, keys):
                deleted_batches.append(keys)

        with patch.object(bedrock_embeddings, "_get_vector_store_bucket", return_value="vb"), patch.object(
            bedrock_embeddings, "_get_vector_store_index", return_value="vi"
        ), patch.object(bedrock_embeddings.boto3, "client", return_value=FakeClient()):
            count = await bedrock_embeddings.delete_vector_tail("doc-1", 3, 7)

        assert count == 4
        assert deleted_batches == [["doc-1#3", "doc-1#4", "doc-1#5", "doc-1#6"]]

    async def test_delete_vector_tail_empty_range_noop(self, monkeypatch):
        from apis.shared.embeddings import bedrock_embeddings

        with patch.object(bedrock_embeddings.boto3, "client") as client:
            count = await bedrock_embeddings.delete_vector_tail("doc-1", 5, 5)

        assert count == 0
        client.assert_not_called()


class TestWebCrawlSync:
    """Worker web re-crawl orchestration (crawler stubbed at the seam;
    crawl job + documents live in moto through the real repositories)."""

    ROOT = "https://example.com/"

    async def _setup_crawl(self, assistants_table, *, page_urls=()):
        from apis.app_api.web_sources.crawl_repository import create_crawl_job, finalize_crawl
        from apis.app_api.web_sources.models import CrawlSettings

        assistant = await create_assistant(
            owner_id=USER_ID, owner_name="U", name="A", description="d",
            instructions="i", vector_index_id="assistants-index",
        )
        assistant_id = assistant.assistant_id
        job = await create_crawl_job(
            assistant_id=assistant_id, root_url=self.ROOT,
            settings=CrawlSettings(max_depth=1, max_pages=10),
            started_by_user_id=USER_ID,
        )
        await finalize_crawl(assistant_id=assistant_id, crawl_id=job.crawl_id, status="complete")

        for i, url in enumerate((self.ROOT, *page_urls)):
            doc_id = f"doc-web-{i}"
            await create_document(
                assistant_id=assistant_id, filename=f"p{i}.md", content_type="text/markdown",
                size_bytes=1, s3_key=f"assistants/{assistant_id}/documents/{doc_id}/p{i}.md",
                document_id=doc_id,
                provenance=DocumentProvenance(
                    source_connector_id="web", source_adapter_key="http",
                    source_file_id=url, imported_by_user_id=USER_ID,
                ),
            )

        policy = await create_sync_policy(
            assistant_id=assistant_id, source_type="web_crawl", source_ref=job.crawl_id,
            interval="daily", created_by_user_id=USER_ID,
        )
        return assistant_id, job, policy

    def _payload(self, assistant_id, policy, job):
        return {
            "policyId": policy.policy_id,
            "assistantId": assistant_id,
            "sourceType": "web_crawl",
            "sourceRef": job.crawl_id,
        }

    @pytest.fixture()
    def fake_crawl(self, monkeypatch):
        """Replace run_crawl with a script: which URLs are seen, which emit
        which outcome."""
        script = {"seen": [], "emit": []}  # emit: (url, outcome, etag, hash)
        captured = {}

        async def fake_run_crawl(**kwargs):
            captured.update(kwargs)
            refresh = kwargs["refresh"]
            for url in script["seen"]:
                refresh.seen_urls.add(url)
            for url, outcome, etag, content_hash in script["emit"]:
                doc = refresh.docs.get(url)
                doc_id = doc.document_id if doc else "doc-new"
                await refresh._emit(url, doc_id, outcome, etag, content_hash)

        from apis.app_api.web_sources import crawler
        monkeypatch.setattr(crawler, "run_crawl", fake_run_crawl)
        script["captured"] = captured
        return script

    async def test_changed_page_stashes_and_records_changed(self, assistants_table, fake_crawl):
        assistant_id, job, policy = await self._setup_crawl(assistants_table)
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-0"},
            UpdateExpression="SET chunkCount = :c",
            ExpressionAttributeValues={":c": 4},
        )
        fake_crawl["seen"] = [self.ROOT]
        fake_crawl["emit"] = [(self.ROOT, "changed", '"e2"', "hash2")]

        result = await worker.run_sync(self._payload(assistant_id, policy, job))

        assert result["result"] == "changed"
        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-0"})["Item"]
        assert item["previousChunkCount"] == 4
        assert item["sourceEtag"] == '"e2"'
        assert item["contentHash"] == "hash2"
        # Marks the version a managed KB's consumer still has to re-ingest.
        assert item["stagedContentHash"] == "hash2"
        # crawler invoked in refresh mode without TTL finalization
        assert fake_crawl["captured"]["finalize_with_ttl"] is False
        assert fake_crawl["captured"]["settings"].max_pages == 10

    async def test_recrawl_reset_removes_job_ttl(self, assistants_table, fake_crawl):
        assistant_id, job, policy = await self._setup_crawl(assistants_table)
        # finalize_crawl(set_ttl=True) above put a ttl on the terminal job
        before = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{job.crawl_id}"})["Item"]
        assert "ttl" in before
        fake_crawl["seen"] = [self.ROOT]

        await worker.run_sync(self._payload(assistant_id, policy, job))

        after = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{job.crawl_id}"})["Item"]
        assert "ttl" not in after
        assert after["discoveredCount"] == 0 and after["fetchedCount"] == 0

    async def test_all_unchanged_records_unchanged(self, assistants_table, fake_crawl):
        assistant_id, job, policy = await self._setup_crawl(assistants_table)
        fake_crawl["seen"] = [self.ROOT]
        fake_crawl["emit"] = [(self.ROOT, "unchanged", '"e1"', None)]

        result = await worker.run_sync(self._payload(assistant_id, policy, job))

        assert result["result"] == "unchanged"

    async def test_miss_counts_then_deletes_on_second_run(self, assistants_table, fake_crawl, monkeypatch):
        from unittest.mock import AsyncMock

        cleanup = AsyncMock()
        monkeypatch.setattr(
            "apis.app_api.documents.services.cleanup_service.cleanup_document_resources", cleanup
        )
        gone_url = "https://example.com/gone"
        assistant_id, job, policy = await self._setup_crawl(assistants_table, page_urls=(gone_url,))
        # Root seen, /gone absent
        fake_crawl["seen"] = [self.ROOT]

        await worker.run_sync(self._payload(assistant_id, policy, job))
        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-1"})["Item"]
        assert item["consecutiveMisses"] == 1
        assert item["status"] != "deleting"
        cleanup.assert_not_awaited()

        # Policy re-armed a day out by the first run's rearm... but the fake
        # crawl doesn't touch the policy; run directly again.
        result = await worker.run_sync(self._payload(assistant_id, policy, job))

        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-1"})["Item"]
        assert item["status"] == "deleting"
        cleanup.assert_awaited_once()
        assert result["result"] == "changed"  # a deletion is a change

    async def test_seen_again_resets_miss_counter(self, assistants_table, fake_crawl):
        flaky_url = "https://example.com/flaky"
        assistant_id, job, policy = await self._setup_crawl(assistants_table, page_urls=(flaky_url,))
        fake_crawl["seen"] = [self.ROOT]
        await worker.run_sync(self._payload(assistant_id, policy, job))

        fake_crawl["seen"] = [self.ROOT, flaky_url]
        await worker.run_sync(self._payload(assistant_id, policy, job))

        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-1"})["Item"]
        assert item["consecutiveMisses"] == 0
        assert item["status"] != "deleting"

    async def test_missing_job_pauses_policy(self, assistants_table, fake_crawl):
        assistant_id, job, policy = await self._setup_crawl(assistants_table)
        assistants_table.delete_item(Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{job.crawl_id}"})

        result = await worker.run_sync(self._payload(assistant_id, policy, job))

        assert result["result"] == "skipped"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_error"

    async def test_missing_root_doc_is_recreated(self, assistants_table, fake_crawl):
        from apis.app_api.web_sources.crawl_repository import create_crawl_job
        from apis.app_api.web_sources.models import CrawlSettings

        assistant = await create_assistant(
            owner_id=USER_ID, owner_name="U", name="A", description="d",
            instructions="i", vector_index_id="assistants-index",
        )
        assistant_id = assistant.assistant_id
        job = await create_crawl_job(
            assistant_id=assistant_id, root_url=self.ROOT,
            settings=CrawlSettings(), started_by_user_id=USER_ID,
        )
        policy = await create_sync_policy(
            assistant_id=assistant_id, source_type="web_crawl", source_ref=job.crawl_id,
            interval="daily", created_by_user_id=USER_ID,
        )
        fake_crawl["seen"] = [self.ROOT]

        await worker.run_sync(self._payload(assistant_id, policy, job))

        root_doc_id = fake_crawl["captured"]["root_document_id"]
        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{root_doc_id}"})["Item"]
        assert item["sourceFileId"] == self.ROOT
        assert item["sourceConnectorId"] == "web"

    async def test_failed_stage_rolls_back_page_gates(self, assistants_table, fake_crawl):
        assistant_id, job, policy = await self._setup_crawl(assistants_table)
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-0"},
            UpdateExpression="SET sourceEtag = :e, contentHash = :h",
            ExpressionAttributeValues={":e": '"e1"', ":h": "hash1"},
        )
        fake_crawl["seen"] = [self.ROOT]
        fake_crawl["emit"] = [
            (self.ROOT, "changed", '"e2"', "hash2"),
            (self.ROOT, "stage_failed", '"e2"', "hash2"),
        ]

        result = await worker.run_sync(self._payload(assistant_id, policy, job))

        # Nothing reached the knowledge base, so the run changed nothing.
        assert result["result"] == "unchanged"
        item = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-web-0"})["Item"]
        assert item["sourceEtag"] == '"e1"'
        assert item["contentHash"] == "hash1"
        assert "stagedContentHash" not in item

        # The next re-crawl still sees the old gates, so it will re-stage.
        fake_crawl["emit"] = []
        await worker.run_sync(self._payload(assistant_id, policy, job))
        refresh_doc = fake_crawl["captured"]["refresh"].docs[self.ROOT]
        assert (refresh_doc.source_etag, refresh_doc.content_hash) == ('"e1"', "hash1")


class TestWebCrawlStageFailureEndToEnd:
    """Worker + the REAL crawler (httpx on a MockTransport, S3 put stubbed):
    a page whose stage fails is staged again by the next re-crawl, for both
    a changed page and a page new to the crawl."""

    ROOT = "https://example.com/"
    NEW = "https://example.com/new"

    @pytest.fixture()
    def site(self, monkeypatch):
        import httpx

        from apis.app_api.web_sources import crawler

        pages = {
            self.ROOT: '<html><body><p>fresh root words</p><a href="/new">n</a></body></html>',
            self.NEW: "<html><body><p>a brand new page</p></body></html>",
        }

        def handler(request):
            url = str(request.url)
            if url in pages:
                return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"})
            return httpx.Response(404, text="")

        monkeypatch.setattr(
            crawler, "_default_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        puts = {"fail": True, "ok": []}

        async def put_markdown(*, assistant_id, document_id, markdown, filename):
            if puts["fail"]:
                raise RuntimeError("S3 PutObject failed")
            puts["ok"].append(document_id)
            return f"assistants/{assistant_id}/documents/{document_id}/{filename}"

        monkeypatch.setattr(crawler, "_put_markdown", put_markdown)
        return puts

    async def test_failed_stages_are_restaged_next_run(self, assistants_table, site):
        from apis.app_api.web_sources.crawl_repository import create_crawl_job, finalize_crawl
        from apis.app_api.web_sources.models import CrawlSettings

        assistant = await create_assistant(
            owner_id=USER_ID, owner_name="U", name="A", description="d",
            instructions="i", vector_index_id="assistants-index",
        )
        assistant_id = assistant.assistant_id
        job = await create_crawl_job(
            assistant_id=assistant_id, root_url=self.ROOT,
            settings=CrawlSettings(max_depth=1, max_pages=10, min_delay_seconds=0, max_delay_seconds=0),
            started_by_user_id=USER_ID,
        )
        await finalize_crawl(assistant_id=assistant_id, crawl_id=job.crawl_id, status="complete")
        await create_document(
            assistant_id=assistant_id, filename="root.md", content_type="text/markdown",
            size_bytes=1, s3_key=f"assistants/{assistant_id}/documents/doc-root/root.md",
            document_id="doc-root",
            provenance=DocumentProvenance(
                source_connector_id="web", source_adapter_key="http",
                source_file_id=self.ROOT, imported_by_user_id=USER_ID,
            ),
        )
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-root"},
            UpdateExpression="SET contentHash = :h",
            ExpressionAttributeValues={":h": "old-root-hash"},
        )
        policy = await create_sync_policy(
            assistant_id=assistant_id, source_type="web_crawl", source_ref=job.crawl_id,
            interval="daily", created_by_user_id=USER_ID,
        )
        payload = {
            "policyId": policy.policy_id, "assistantId": assistant_id,
            "sourceType": "web_crawl", "sourceRef": job.crawl_id,
        }

        first = await worker.run_sync(payload)

        assert first["result"] == "unchanged"
        assert site["ok"] == []
        root = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-root"})["Item"]
        assert root["contentHash"] == "old-root-hash"
        assert "stagedContentHash" not in root
        new_docs = [
            item for item in _document_items(assistants_table, assistant_id)
            if item.get("sourceFileId") == self.NEW
        ]
        assert len(new_docs) == 1
        assert "contentHash" not in new_docs[0]
        assert new_docs[0]["status"] == "failed"

        site["fail"] = False
        second = await worker.run_sync(payload)

        assert second["result"] == "changed"
        assert sorted(site["ok"]) == sorted(["doc-root", new_docs[0]["documentId"]])
        root = assistants_table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "DOC#doc-root"})["Item"]
        assert root["contentHash"] != "old-root-hash"
        assert root["stagedContentHash"] == root["contentHash"]


def _document_items(assistants_table, assistant_id):
    from boto3.dynamodb.conditions import Key

    return assistants_table.query(
        KeyConditionExpression=Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("DOC#")
    )["Items"]
