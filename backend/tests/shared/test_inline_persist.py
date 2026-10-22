"""``apis.shared.files.inline_persist`` — inline attachment bytes become session files.

What these pin: the persisted row is byte-compatible with an upload (same key
layout, READY, quota counted, ``source="inline"``); every validation the SPA
upload path enforces is enforced here too; and no single bad file, S3 error or
metadata error can fail the batch — each failure is reported per file with a
user-facing reason, and an S3 object whose row could not be written is removed
rather than orphaned.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from apis.shared.files.inline_persist import (
    INLINE_SOURCE,
    InlineAttachmentPersister,
    PersistFailure,
    s3_key_for,
)
from apis.shared.files.models import FileMetadata, FileStatus, UserFileQuota

CSV = "text/csv"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@dataclass
class _File:
    filename: str
    content_type: str
    bytes: str


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class _FakeS3:
    def __init__(self, fail_keys: Optional[set] = None) -> None:
        self.objects: Dict[str, Dict[str, Any]] = {}
        self.deleted: List[str] = []
        self.fail_keys = fail_keys or set()

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        if any(Key.endswith(k) for k in self.fail_keys):
            raise RuntimeError("boom")
        self.objects[Key] = {"bucket": Bucket, "body": Body, "content_type": ContentType}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class _FakeRepo:
    def __init__(self, used: int = 0, fail_create_for: Optional[set] = None) -> None:
        self.rows: List[FileMetadata] = []
        self.quota_increments: List[int] = []
        self.used = used
        self.fail_create_for = fail_create_for or set()

    async def get_user_quota(self, user_id: str) -> UserFileQuota:
        return UserFileQuota(user_id=user_id, total_bytes=self.used, file_count=len(self.rows))

    async def create_file(self, meta: FileMetadata) -> FileMetadata:
        if meta.filename in self.fail_create_for:
            raise RuntimeError("ddb down")
        self.rows.append(meta)
        return meta

    async def increment_quota(self, user_id: str, size_bytes: int) -> UserFileQuota:
        self.quota_increments.append(size_bytes)
        self.used += size_bytes
        return UserFileQuota(user_id=user_id, total_bytes=self.used, file_count=len(self.rows))


def _persister(repo: _FakeRepo, s3: _FakeS3) -> InlineAttachmentPersister:
    return InlineAttachmentPersister(repository=repo, s3_client=s3, bucket_name="bucket-under-test")


@pytest.mark.asyncio
async def test_persists_like_an_upload():
    repo, s3 = _FakeRepo(), _FakeS3()
    raw = b"a,b\n1,2\n"
    persisted, failures = await _persister(repo, s3).persist(user_id="u1", session_id="s1", files=[_File("data.csv", CSV, _b64(raw))])

    assert failures == []
    assert len(persisted) == 1
    meta = persisted[0]
    assert meta.s3_key == s3_key_for("u1", "s1", meta.upload_id, "data.csv")
    assert meta.s3_key.startswith("user-files/u1/s1/")
    assert meta.s3_bucket == "bucket-under-test"
    assert meta.status == FileStatus.READY.value
    assert meta.source == INLINE_SOURCE
    assert meta.size_bytes == len(raw)
    assert meta.mime_type == CSV
    assert s3.objects[meta.s3_key]["body"] == raw
    assert s3.objects[meta.s3_key]["content_type"] == CSV
    assert repo.rows == [meta]
    assert repo.quota_increments == [len(raw)]


@pytest.mark.asyncio
async def test_empty_input_touches_nothing():
    repo, s3 = _FakeRepo(), _FakeS3()
    assert await _persister(repo, s3).persist(user_id="u", session_id="s", files=[]) == ([], [])
    assert s3.objects == {} and repo.rows == []


@pytest.mark.asyncio
async def test_rejects_disallowed_mime_type():
    repo, s3 = _FakeRepo(), _FakeS3()
    persisted, failures = await _persister(repo, s3).persist(
        user_id="u", session_id="s", files=[_File("x.exe", "application/x-msdownload", _b64(b"MZ"))]
    )
    assert persisted == []
    assert [f.file.filename for f in failures] == ["x.exe"]
    assert "not accepted" in failures[0].reason
    assert s3.objects == {}


@pytest.mark.asyncio
async def test_rejects_invalid_base64_without_touching_s3():
    repo, s3 = _FakeRepo(), _FakeS3()
    _, failures = await _persister(repo, s3).persist(user_id="u", session_id="s", files=[_File("data.csv", CSV, "not base64!!")])
    assert [f.reason for f in failures] == ["its contents were not valid base64"]
    assert s3.objects == {}


@pytest.mark.asyncio
async def test_size_cap_is_per_class(monkeypatch):
    monkeypatch.setenv("FILE_UPLOAD_MAX_SIZE_BYTES", str(1024 * 1024))
    monkeypatch.setenv("FILE_UPLOAD_MAX_SIZE_BYTES_PRESENTATION", str(3 * 1024 * 1024))
    repo, s3 = _FakeRepo(), _FakeS3()
    two_mb = _b64(b"x" * (2 * 1024 * 1024))
    persisted, failures = await _persister(repo, s3).persist(
        user_id="u",
        session_id="s",
        files=[_File("big.csv", CSV, two_mb), _File("deck.pptx", PPTX, two_mb)],
    )
    # 2 MB is over the 1 MB general cap but under the 3 MB presentation cap.
    assert [m.filename for m in persisted] == ["deck.pptx"]
    assert [f.file.filename for f in failures] == ["big.csv"]
    assert "1 MB limit" in failures[0].reason


@pytest.mark.asyncio
async def test_user_quota_is_charged_cumulatively_across_the_batch(monkeypatch):
    monkeypatch.setenv("FILE_UPLOAD_USER_QUOTA_BYTES", "100")
    repo, s3 = _FakeRepo(used=40), _FakeS3()
    files = [_File("a.csv", CSV, _b64(b"x" * 30)), _File("b.csv", CSV, _b64(b"y" * 30)), _File("c.csv", CSV, _b64(b"z" * 30))]
    persisted, failures = await _persister(repo, s3).persist(user_id="u", session_id="s", files=files)
    # 40 + 30 + 30 = 100 fits; the third would be 130.
    assert [m.filename for m in persisted] == ["a.csv", "b.csv"]
    assert [(f.file.filename, f.reason) for f in failures] == [("c.csv", "your file storage quota is full")]


@pytest.mark.asyncio
async def test_s3_failure_is_per_file_and_writes_no_row():
    repo, s3 = _FakeRepo(), _FakeS3(fail_keys={"bad.csv"})
    persisted, failures = await _persister(repo, s3).persist(
        user_id="u",
        session_id="s",
        files=[_File("ok.csv", CSV, _b64(b"1")), _File("bad.csv", CSV, _b64(b"2")), _File("also.csv", CSV, _b64(b"3"))],
    )
    assert [m.filename for m in persisted] == ["ok.csv", "also.csv"]
    assert [f.file.filename for f in failures] == ["bad.csv"]
    assert failures[0].reason == "it could not be stored"
    assert [r.filename for r in repo.rows] == ["ok.csv", "also.csv"]


@pytest.mark.asyncio
async def test_metadata_failure_removes_the_orphan_object():
    repo, s3 = _FakeRepo(fail_create_for={"orphan.csv"}), _FakeS3()
    persisted, failures = await _persister(repo, s3).persist(user_id="u", session_id="s", files=[_File("orphan.csv", CSV, _b64(b"1"))])
    assert persisted == []
    assert [f.file.filename for f in failures] == ["orphan.csv"]
    assert s3.objects == {}
    assert len(s3.deleted) == 1 and s3.deleted[0].endswith("/orphan.csv")
    assert repo.quota_increments == []


@pytest.mark.asyncio
async def test_results_keep_attachment_order_across_mixed_outcomes():
    repo, s3 = _FakeRepo(), _FakeS3()
    files = [
        _File("1.csv", CSV, _b64(b"a")),
        _File("2.csv", CSV, "###"),  # invalid base64
        _File("3.csv", CSV, _b64(b"c")),
    ]
    persisted, failures = await _persister(repo, s3).persist(user_id="u", session_id="s", files=files)
    assert [m.filename for m in persisted] == ["1.csv", "3.csv"]
    assert [f.file.filename for f in failures] == ["2.csv"]
    assert all(isinstance(f, PersistFailure) for f in failures)


@pytest.mark.asyncio
async def test_quota_lookup_failure_does_not_block_the_turn():
    class _BrokenQuotaRepo(_FakeRepo):
        async def get_user_quota(self, user_id: str) -> UserFileQuota:  # type: ignore[override]
            raise RuntimeError("ddb")

    repo, s3 = _BrokenQuotaRepo(), _FakeS3()
    persisted, failures = await _persister(repo, s3).persist(user_id="u", session_id="s", files=[_File("a.csv", CSV, _b64(b"1"))])
    assert [m.filename for m in persisted] == ["a.csv"] and failures == []
