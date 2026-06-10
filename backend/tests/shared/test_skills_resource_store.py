"""Tests for the S3-backed skill reference-file store (PR-4).

moto-backed: exercises content-hash keying, dedupe (no second put for
identical bytes), get/delete round-trip, and the not-configured guard.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.skills.resource_store import (
    SkillResourceStore,
    SkillResourceStoreError,
    compute_content_hash,
    content_key,
    get_skill_resource_store,
)

AWS_REGION = "us-east-1"
BUCKET = "test-skill-resources"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def s3_client(aws):
    client = boto3.client("s3", region_name=AWS_REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture()
def store(s3_client):
    return SkillResourceStore(bucket_name=BUCKET, s3_client=s3_client)


class TestContentKey:
    def test_key_is_content_addressed(self):
        digest = compute_content_hash(b"# Notes")
        assert content_key("pdf_workflows", digest) == (
            f"skills/pdf_workflows/{digest}"
        )

    def test_hash_is_sha256_hex(self):
        import hashlib

        assert compute_content_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()


class TestPutGetDelete:
    def test_put_returns_content_addressed_key(self, store):
        key = store.put(
            skill_id="pdf_workflows",
            content=b"# Notes",
            content_type="text/markdown",
        )
        assert key == content_key("pdf_workflows", compute_content_hash(b"# Notes"))

    def test_get_returns_bytes(self, store):
        key = store.put(
            skill_id="pdf_workflows", content=b"hello", content_type="text/plain"
        )
        assert store.get(key) == b"hello"

    def test_put_is_idempotent_dedupe(self, store, s3_client):
        # Two puts of identical content land on one object (content-addressed).
        k1 = store.put(
            skill_id="pdf_workflows", content=b"same", content_type="text/plain"
        )
        k2 = store.put(
            skill_id="pdf_workflows", content=b"same", content_type="text/plain"
        )
        assert k1 == k2
        listed = s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        assert len(listed) == 1

    def test_different_content_distinct_keys(self, store, s3_client):
        store.put(skill_id="s", content=b"a", content_type="text/plain")
        store.put(skill_id="s", content=b"b", content_type="text/plain")
        listed = s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        assert len(listed) == 2

    def test_same_content_scoped_per_skill(self, store, s3_client):
        # The key includes the skill_id, so identical content under two skills
        # is two objects (dedupe is per-skill, matching the key layout).
        store.put(skill_id="skill_a", content=b"shared", content_type="text/plain")
        store.put(skill_id="skill_b", content=b"shared", content_type="text/plain")
        listed = s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        assert len(listed) == 2

    def test_put_sets_content_type(self, store, s3_client):
        key = store.put(
            skill_id="s", content=b"# md", content_type="text/markdown"
        )
        head = s3_client.head_object(Bucket=BUCKET, Key=key)
        assert head["ContentType"] == "text/markdown"

    def test_delete_removes_object(self, store, s3_client):
        key = store.put(skill_id="s", content=b"x", content_type="text/plain")
        store.delete(key)
        assert s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", []) == []

    def test_get_missing_raises(self, store):
        with pytest.raises(SkillResourceStoreError):
            store.get("skills/s/deadbeef")

    def test_delete_missing_is_noop(self, store):
        # No object at the key — delete must not raise (S3 delete is idempotent).
        store.delete("skills/s/deadbeef")


class TestNotConfigured:
    def test_disabled_when_no_bucket(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        s = SkillResourceStore()
        assert s.enabled is False

    def test_put_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        s = SkillResourceStore()
        with pytest.raises(SkillResourceStoreError):
            s.put(skill_id="s", content=b"x", content_type="text/plain")

    def test_get_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        s = SkillResourceStore()
        with pytest.raises(SkillResourceStoreError):
            s.get("skills/s/abc")

    def test_delete_silent_when_disabled(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        SkillResourceStore().delete("skills/s/abc")  # must not raise

    def test_enabled_from_env(self, monkeypatch):
        monkeypatch.setenv("S3_SKILL_RESOURCES_BUCKET_NAME", "some-bucket")
        assert SkillResourceStore().enabled is True


def test_global_store_is_singleton(monkeypatch):
    monkeypatch.setenv("S3_SKILL_RESOURCES_BUCKET_NAME", "b")
    import apis.shared.skills.resource_store as mod

    mod._store = None
    a = get_skill_resource_store()
    b = get_skill_resource_store()
    assert a is b
    mod._store = None
