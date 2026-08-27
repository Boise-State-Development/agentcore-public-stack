"""Tests for the S3 snapshot-body offload in ShareService.

Covers the regression this feature fixes — a conversation too large to inline
in a DynamoDB item (>400 KB) can now be shared — plus the write shape
(``body_ref``, no inline ``messages``), the S3-backed and legacy-inline read
paths, revoke cleanup, and the storage-unavailable guard.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.shares.service import (
    ShareService,
    ShareStorageUnavailableError,
)
from apis.app_api.shares.snapshot_store import ShareSnapshotStore
from apis.app_api.shares.models import CreateShareRequest
from apis.shared.auth.models import User

AWS_REGION = "us-east-1"
BUCKET = "test-shared-conversations"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def s3_client(aws_env):
    client = boto3.client("s3", region_name=AWS_REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture()
def store(s3_client):
    return ShareSnapshotStore(bucket_name=BUCKET, s3_client=s3_client)


@pytest.fixture()
def service(store, monkeypatch):
    """A ShareService with a real moto S3 store and a mock DynamoDB table."""
    monkeypatch.setenv("SHARED_CONVERSATIONS_TABLE_NAME", "shares-table")
    with patch("boto3.resource"):
        svc = ShareService(snapshot_store=store)
    svc._table = MagicMock()
    return svc


def _owner() -> User:
    return User(email="owner@example.com", user_id="owner-1", name="Owner", roles=["User"])


def _message_dict(text: str, msg_id: str = "m0") -> dict:
    return {
        "id": msg_id,
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "createdAt": "2025-06-01T00:00:00Z",
    }


def _patch_snapshot_sources(metadata_dump: dict, message_dumps: list):
    """Patch get_session_metadata + get_messages used by create_share."""
    meta = MagicMock()
    meta.model_dump.return_value = metadata_dump

    messages = [MagicMock(model_dump=MagicMock(return_value=m)) for m in message_dumps]
    messages_response = MagicMock(messages=messages)

    return (
        patch(
            "apis.app_api.shares.service.get_session_metadata",
            new=AsyncMock(return_value=meta),
        ),
        patch(
            "apis.app_api.shares.service.get_messages",
            new=AsyncMock(return_value=messages_response),
        ),
    )


class TestCreateOffloadsToS3:
    @pytest.mark.asyncio
    async def test_item_carries_body_ref_not_inline_messages(self, service, s3_client):
        meta_patch, msgs_patch = _patch_snapshot_sources(
            {"title": "Hello Chat"}, [_message_dict("hi")]
        )
        with meta_patch, msgs_patch:
            resp = await service.create_share(
                "sess-1", _owner(), CreateShareRequest(accessLevel="public")
            )

        # The DynamoDB item was written with a body_ref and NO inline body.
        item = service._table.put_item.call_args[1]["Item"]
        assert "messages" not in item
        assert "metadata" not in item
        assert item["body_ref"]["bucket_key"].startswith("shares/")
        assert item["body_ref"]["format"] == "json"
        assert item["body_ref"]["byte_size"] > 0
        assert resp.session_id == "sess-1"

        # The S3 object decodes to the full snapshot body.
        obj = s3_client.get_object(Bucket=BUCKET, Key=item["body_ref"]["bucket_key"])
        body = json.loads(obj["Body"].read())
        assert body["metadata"]["title"] == "Hello Chat"
        assert body["messages"][0]["content"][0]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_large_conversation_succeeds(self, service):
        """Regression: a >400 KB snapshot no longer fails on PutItem."""
        big = [_message_dict("x" * 5000, msg_id=f"m{i}") for i in range(120)]
        # Sanity: the inline body would have blown DynamoDB's 400 KB item cap.
        assert len(json.dumps({"metadata": {}, "messages": big})) > 400_000

        meta_patch, msgs_patch = _patch_snapshot_sources({"title": "Big"}, big)
        with meta_patch, msgs_patch:
            resp = await service.create_share(
                "sess-big", _owner(), CreateShareRequest(accessLevel="public")
            )

        assert resp.session_id == "sess-big"
        item = service._table.put_item.call_args[1]["Item"]
        assert item["body_ref"]["byte_size"] > 400_000

    @pytest.mark.asyncio
    async def test_storage_unavailable_raises(self, monkeypatch):
        monkeypatch.setenv("SHARED_CONVERSATIONS_TABLE_NAME", "shares-table")
        # `bucket_name=None` means "no bucket configured", but the store falls back
        # to this variable when the argument is None — so the test only asserted
        # what it meant to while the variable happened to be absent from the
        # environment. Any developer with a populated `backend/src/.env` (which
        # `load_dotenv(override=True)` reads) gave the store a real bucket, and the
        # assertion became a live S3 HeadObject against it. Deleted explicitly so
        # "unavailable" is a property of the test rather than of the machine.
        monkeypatch.delenv("SHARED_CONVERSATIONS_BUCKET_NAME", raising=False)
        with patch("boto3.resource"):
            svc = ShareService(snapshot_store=ShareSnapshotStore(bucket_name=None))
        svc._table = MagicMock()

        meta_patch, msgs_patch = _patch_snapshot_sources({"title": "T"}, [_message_dict("hi")])
        with meta_patch, msgs_patch:
            with pytest.raises(ShareStorageUnavailableError):
                await svc.create_share(
                    "sess-1", _owner(), CreateShareRequest(accessLevel="public")
                )
        # Nothing was written to DynamoDB when storage is unavailable.
        svc._table.put_item.assert_not_called()


class TestReadPaths:
    @pytest.mark.asyncio
    async def test_s3_backed_read_round_trips(self, service, store):
        # Create, then read back through get_shared_conversation.
        meta_patch, msgs_patch = _patch_snapshot_sources(
            {"title": "Round Trip"}, [_message_dict("hello", "m1")]
        )
        with meta_patch, msgs_patch:
            await service.create_share(
                "sess-1", _owner(), CreateShareRequest(accessLevel="public")
            )
        written = service._table.put_item.call_args[1]["Item"]

        with patch.object(service, "_get_share_item", return_value=written):
            result = await service.get_shared_conversation("share-x", _owner())

        assert result.title == "Round Trip"
        assert len(result.messages) == 1
        assert result.messages[0].content[0].text == "hello"

    @pytest.mark.asyncio
    async def test_legacy_inline_read_still_works(self, service):
        """A pre-offload item (inline messages, no body_ref) still resolves."""
        legacy_item = {
            "share_id": "share-legacy",
            "session_id": "sess-old",
            "owner_id": "owner-1",
            "access_level": "public",
            "created_at": "2025-01-01T00:00:00Z",
            "metadata": {"title": "Old One"},
            "messages": [_message_dict("legacy hi", "m0")],
        }
        with patch.object(service, "_get_share_item", return_value=legacy_item):
            result = await service.get_shared_conversation("share-legacy", _owner())

        assert result.title == "Old One"
        assert result.messages[0].content[0].text == "legacy hi"

    @pytest.mark.asyncio
    async def test_malformed_item_raises_not_found(self, service):
        from apis.app_api.shares.service import ShareNotFoundError

        bad = {
            "share_id": "share-bad",
            "session_id": "s",
            "owner_id": "owner-1",
            "access_level": "public",
            "created_at": "2025-01-01T00:00:00Z",
        }
        with patch.object(service, "_get_share_item", return_value=bad):
            with pytest.raises(ShareNotFoundError):
                await service.get_shared_conversation("share-bad", _owner())


    @pytest.mark.asyncio
    async def test_export_reads_s3_backed_body(self, service):
        """export_shared_conversation loads the body from S3, not inline."""
        meta_patch, msgs_patch = _patch_snapshot_sources(
            {"title": "Export Me"},
            [_message_dict("one", "m0"), _message_dict("two", "m1")],
        )
        with meta_patch, msgs_patch:
            await service.create_share(
                "sess-1", _owner(), CreateShareRequest(accessLevel="public")
            )
        item = service._table.put_item.call_args[1]["Item"]

        with patch.object(service, "_get_share_item", return_value=item), \
             patch.object(service, "_check_access"), \
             patch.object(
                 service, "_copy_messages_to_memory",
                 new_callable=AsyncMock, return_value=2,
             ) as mock_copy, \
             patch(
                 "apis.app_api.shares.service.store_session_metadata",
                 new_callable=AsyncMock,
             ):
            result = await service.export_shared_conversation("share-x", _owner())

        assert result["title"] == "Export Me (shared)"
        # The two S3-stored messages were the ones handed to the memory copy.
        assert len(mock_copy.call_args[0][2]) == 2


class TestRevokeCleansUpS3:
    @pytest.mark.asyncio
    async def test_revoke_deletes_s3_object(self, service, s3_client):
        meta_patch, msgs_patch = _patch_snapshot_sources({"title": "T"}, [_message_dict("hi")])
        with meta_patch, msgs_patch:
            await service.create_share(
                "sess-1", _owner(), CreateShareRequest(accessLevel="public")
            )
        item = service._table.put_item.call_args[1]["Item"]
        key = item["body_ref"]["bucket_key"]
        # Object exists before revoke.
        assert s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()

        with patch.object(service, "_get_share_item", return_value=item):
            await service.revoke_share(item["share_id"], _owner())

        with pytest.raises(s3_client.exceptions.NoSuchKey):
            s3_client.get_object(Bucket=BUCKET, Key=key)
