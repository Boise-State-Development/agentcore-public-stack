"""Tests for the S3-backed Memory Space byte store (PR-1).

moto-backed: exercises content-hash keying, dedupe (no second put for
identical bytes), get/delete round-trip, and the not-configured guard.
Mirrors ``test_skills_resource_store.py``.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.memory.store import (
    MemorySpaceStore,
    MemorySpaceStoreError,
    compute_content_hash,
    content_key,
    get_memory_space_store,
)

AWS_REGION = "us-east-1"
BUCKET = "test-memory-spaces"


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
    return MemorySpaceStore(bucket_name=BUCKET, s3_client=s3_client)


class TestContentKey:
    def test_key_is_content_addressed(self):
        digest = compute_content_hash(b"# Memory")
        assert content_key("spc_abc", digest) == f"spaces/spc_abc/{digest}"

    def test_hash_is_stable_and_distinct(self):
        assert compute_content_hash(b"a") == compute_content_hash(b"a")
        assert compute_content_hash(b"a") != compute_content_hash(b"b")


class TestPutGetDelete:
    def test_put_returns_content_addressed_key(self, store):
        key = store.put(
            space_id="spc_abc", content=b"# Memory", content_type="text/markdown"
        )
        assert key == content_key("spc_abc", compute_content_hash(b"# Memory"))

    def test_get_round_trip(self, store):
        key = store.put(
            space_id="spc_abc", content=b"hello", content_type="text/markdown"
        )
        assert store.get(key) == b"hello"

    def test_put_is_idempotent_dedupe(self, store, s3_client):
        k1 = store.put(space_id="s", content=b"same", content_type="text/markdown")
        k2 = store.put(space_id="s", content=b"same", content_type="text/markdown")
        assert k1 == k2
        listed = s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        assert len(listed) == 1

    def test_different_content_distinct_keys(self, store):
        k1 = store.put(space_id="s", content=b"one", content_type="text/markdown")
        k2 = store.put(space_id="s", content=b"two", content_type="text/markdown")
        assert k1 != k2

    def test_get_missing_raises(self, store):
        with pytest.raises(MemorySpaceStoreError):
            store.get("spaces/s/deadbeef")

    def test_delete_round_trip(self, store):
        key = store.put(space_id="s", content=b"bye", content_type="text/markdown")
        store.delete(key)
        with pytest.raises(MemorySpaceStoreError):
            store.get(key)

    def test_delete_absent_is_noop(self, store):
        # Best-effort: deleting a missing key does not raise.
        store.delete("spaces/s/missing")


class TestListKeys:
    def test_lists_only_the_space_prefix(self, store):
        k1 = store.put(space_id="s1", content=b"a", content_type="text/markdown")
        k2 = store.put(space_id="s1", content=b"b", content_type="text/markdown")
        store.put(space_id="s2", content=b"c", content_type="text/markdown")
        assert set(store.list_keys("s1")) == {k1, k2}

    def test_empty_space_returns_empty(self, store):
        assert store.list_keys("nope") == []

    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.delenv("S3_MEMORY_SPACES_BUCKET_NAME", raising=False)
        assert MemorySpaceStore(bucket_name=None).list_keys("s") == []


class TestNotConfigured:
    def test_disabled_when_no_bucket(self, monkeypatch):
        monkeypatch.delenv("S3_MEMORY_SPACES_BUCKET_NAME", raising=False)
        store = MemorySpaceStore(bucket_name=None)
        assert store.enabled is False

    def test_put_raises_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("S3_MEMORY_SPACES_BUCKET_NAME", raising=False)
        store = MemorySpaceStore(bucket_name=None)
        with pytest.raises(MemorySpaceStoreError):
            store.put(space_id="s", content=b"x", content_type="text/markdown")

    def test_delete_when_disabled_is_noop(self, monkeypatch):
        monkeypatch.delenv("S3_MEMORY_SPACES_BUCKET_NAME", raising=False)
        MemorySpaceStore(bucket_name=None).delete("spaces/s/x")


def test_global_store_singleton(monkeypatch):
    monkeypatch.setenv("S3_MEMORY_SPACES_BUCKET_NAME", "b")
    import apis.shared.memory.store as store_mod

    store_mod._store = None  # reset the process-global for a clean read
    a = get_memory_space_store()
    b = get_memory_space_store()
    assert a is b
    store_mod._store = None
