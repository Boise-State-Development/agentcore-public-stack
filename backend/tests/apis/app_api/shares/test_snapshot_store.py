"""Tests for the S3-backed share snapshot-body store.

moto-backed: exercises content-hash keying, dedupe (no second put for
identical bytes), get/delete round-trip, and the not-configured guard.
Mirrors ``tests/shared/test_memory_store.py``.
"""

import boto3
import pytest
from moto import mock_aws

from apis.app_api.shares.snapshot_store import (
    ShareSnapshotStore,
    ShareSnapshotStoreError,
    compute_content_hash,
    content_key,
)

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


class TestContentKey:
    def test_key_is_content_addressed(self):
        digest = compute_content_hash(b"body")
        assert content_key("share-1", digest) == f"shares/share-1/{digest}"

    def test_hash_is_stable_and_distinct(self):
        assert compute_content_hash(b"a") == compute_content_hash(b"a")
        assert compute_content_hash(b"a") != compute_content_hash(b"b")


class TestPutGetDelete:
    def test_put_returns_content_addressed_key(self, store):
        key = store.put(share_id="share-1", body=b'{"messages":[]}')
        assert key == content_key("share-1", compute_content_hash(b'{"messages":[]}'))

    def test_get_round_trip(self, store):
        key = store.put(share_id="share-1", body=b"hello-body")
        assert store.get(key) == b"hello-body"

    def test_put_is_idempotent_dedupe(self, store, s3_client):
        k1 = store.put(share_id="s", body=b"same")
        k2 = store.put(share_id="s", body=b"same")
        assert k1 == k2
        # Exactly one object under the prefix.
        listing = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="shares/s/")
        assert listing["KeyCount"] == 1

    def test_get_missing_key_raises(self, store):
        with pytest.raises(ShareSnapshotStoreError):
            store.get("shares/nope/deadbeef")

    def test_delete_is_best_effort(self, store):
        key = store.put(share_id="s", body=b"x")
        store.delete(key)
        # Deleting an already-absent object is a no-op (never raises).
        store.delete(key)
        with pytest.raises(ShareSnapshotStoreError):
            store.get(key)


class TestEnabledGuard:
    def test_disabled_when_bucket_unset(self, monkeypatch):
        monkeypatch.delenv("SHARED_CONVERSATIONS_BUCKET_NAME", raising=False)
        assert ShareSnapshotStore(bucket_name=None).enabled is False

    def test_put_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("SHARED_CONVERSATIONS_BUCKET_NAME", raising=False)
        store = ShareSnapshotStore(bucket_name=None)
        with pytest.raises(ShareSnapshotStoreError):
            store.put(share_id="s", body=b"x")

    def test_delete_when_disabled_is_noop(self, monkeypatch):
        monkeypatch.delenv("SHARED_CONVERSATIONS_BUCKET_NAME", raising=False)
        store = ShareSnapshotStore(bucket_name=None)
        # No exception even though storage is unconfigured.
        store.delete("shares/s/abc")
