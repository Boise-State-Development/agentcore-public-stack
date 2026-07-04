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
