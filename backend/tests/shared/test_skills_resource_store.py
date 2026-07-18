"""Tests for the S3-backed skill resource store (Skills v2).

moto-backed: exercises the standard agentskills.io bundle layout
(``references|scripts|assets``), the SKILL.md projection write, get/delete
round-trip, overwrite-in-place, and the not-configured guard.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.skills.resource_store import (
    SkillResourceStore,
    SkillResourceStoreError,
    compute_content_hash,
    resource_key,
    skill_md_key,
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


class TestKeys:
    def test_reference_key_uses_bundle_layout(self):
        assert resource_key("pdf_workflows", "reference", "forms.md") == (
            "skills/pdf_workflows/references/forms.md"
        )

    def test_script_and_asset_dirs(self):
        assert resource_key("s", "script", "run.py") == "skills/s/scripts/run.py"
        assert resource_key("s", "asset", "logo.png") == "skills/s/assets/logo.png"

    def test_unknown_kind_falls_back_to_references(self):
        assert resource_key("s", "bogus", "f.md") == "skills/s/references/f.md"

    def test_skill_md_key(self):
        assert skill_md_key("pdf_workflows") == "skills/pdf_workflows/SKILL.md"

    def test_hash_is_sha256_hex(self):
        import hashlib

        assert compute_content_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()


class TestPutGetDelete:
    def test_put_returns_bundle_key(self, store):
        key = store.put(
            skill_id="pdf_workflows",
            filename="forms.md",
            content=b"# Notes",
            content_type="text/markdown",
        )
        assert key == "skills/pdf_workflows/references/forms.md"

    def test_get_returns_bytes(self, store):
        key = store.put(
            skill_id="pdf_workflows",
            filename="a.txt",
            content=b"hello",
            content_type="text/plain",
        )
        assert store.get(key) == b"hello"

    def test_put_overwrites_in_place(self, store, s3_client):
        # Same (kind, filename) → same key → one object, latest content wins.
        store.put(skill_id="s", filename="a.txt", content=b"v1", content_type="text/plain")
        key = store.put(
            skill_id="s", filename="a.txt", content=b"v2", content_type="text/plain"
        )
        listed = s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        assert len(listed) == 1
        assert store.get(key) == b"v2"

    def test_distinct_filenames_distinct_objects(self, store, s3_client):
        store.put(skill_id="s", filename="a.txt", content=b"a", content_type="text/plain")
        store.put(skill_id="s", filename="b.txt", content=b"a", content_type="text/plain")
        listed = s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        # No dedupe: identical content under two filenames is two objects.
        assert len(listed) == 2

    def test_kinds_land_in_distinct_dirs(self, store, s3_client):
        store.put(skill_id="s", filename="f.md", content=b"r", content_type="text/plain", kind="reference")
        store.put(skill_id="s", filename="f.md", content=b"x", content_type="text/plain", kind="script")
        keys = {o["Key"] for o in s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
        assert keys == {"skills/s/references/f.md", "skills/s/scripts/f.md"}

    def test_put_skill_md(self, store, s3_client):
        key = store.put_skill_md(skill_id="pdf_workflows", content="---\nname: x\n---\n\nbody\n")
        assert key == "skills/pdf_workflows/SKILL.md"
        assert store.get(key).decode() == "---\nname: x\n---\n\nbody\n"
        head = s3_client.head_object(Bucket=BUCKET, Key=key)
        assert head["ContentType"] == "text/markdown"

    def test_put_sets_content_type(self, store, s3_client):
        key = store.put(
            skill_id="s", filename="m.md", content=b"# md", content_type="text/markdown"
        )
        head = s3_client.head_object(Bucket=BUCKET, Key=key)
        assert head["ContentType"] == "text/markdown"

    def test_delete_removes_object(self, store, s3_client):
        key = store.put(skill_id="s", filename="x.txt", content=b"x", content_type="text/plain")
        store.delete(key)
        assert s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", []) == []

    def test_get_missing_raises(self, store):
        with pytest.raises(SkillResourceStoreError):
            store.get("skills/s/references/missing.md")

    def test_delete_missing_is_noop(self, store):
        # No object at the key — delete must not raise (S3 delete is idempotent).
        store.delete("skills/s/references/missing.md")


class TestNotConfigured:
    def test_disabled_when_no_bucket(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        s = SkillResourceStore()
        assert s.enabled is False

    def test_put_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        s = SkillResourceStore()
        with pytest.raises(SkillResourceStoreError):
            s.put(skill_id="s", filename="x.txt", content=b"x", content_type="text/plain")

    def test_get_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        s = SkillResourceStore()
        with pytest.raises(SkillResourceStoreError):
            s.get("skills/s/references/abc.md")

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
