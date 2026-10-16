"""Large-document attachments on app-api's file service (files/service.py), PR-1.

* ``complete_upload`` of a reference-class document enqueues it for the
  preparation Lambda (``preparation = pending`` first, then the message) and
  never reads it in-process; every other upload keeps the in-process digest;
* nothing on that path can fail the upload;
* ``delete_file`` and ``delete_session_files`` remove the ``_derived/``
  sidecars along with the original and the thumbnail (moto S3).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from apis.app_api.files.service import FileUploadService
from apis.shared.files import large_document as ld
from apis.shared.files.models import INLINE_DOCUMENT_MAX_BYTES, FileMetadata, FileStatus

QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/test-large-document-prepare"
LARGE = INLINE_DOCUMENT_MAX_BYTES + 1
BIG_CAP = 50 * 1024 * 1024


def _meta(size=LARGE, mime="application/pdf", filename="manual.pdf", status=FileStatus.PENDING,
          upload_id="up-1", preparation=None):
    return FileMetadata(
        upload_id=upload_id, user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size, s3_key=f"user-files/u1/s1/{upload_id}/{filename}", s3_bucket="b", status=status,
        preparation=preparation,
    )


def _service(meta, sqs=None):
    repository = MagicMock()
    repository.get_file = AsyncMock(return_value=meta)
    repository.update_file_status = AsyncMock()
    repository.increment_quota = AsyncMock()
    repository.update_file_digest = AsyncMock(return_value=meta)
    calls = []
    repository.update_file_preparation = AsyncMock(
        side_effect=lambda user_id, upload_id, prep, digest=None: calls.append(("prep", prep)) or meta
    )
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": meta.size_bytes}
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"%PDF"))}
    sqs = sqs or MagicMock()
    sqs.send_message.side_effect = sqs.send_message.side_effect or (lambda **kw: calls.append(("send", kw)))
    service = FileUploadService(
        repository=repository, s3_client=s3, bucket_name="b", max_file_size=BIG_CAP, sqs_client=sqs,
    )
    return service, repository, s3, sqs, calls


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
    monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", QUEUE_URL)
    monkeypatch.delenv("DOCUMENT_DIGEST_ENABLED", raising=False)


async def _drain(service):
    tasks = list(service._digest_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return tasks


@pytest.mark.asyncio
class TestCompleteUpload:
    async def test_reference_document_is_queued_pending_first_and_never_read(self, enabled):
        service, repository, s3, sqs, calls = _service(_meta())

        response = await service.complete_upload("u1", "up-1")

        assert response.status == "ready"
        assert [c[0] for c in calls] == ["prep", "send"]
        assert calls[0][1] == ld.preparation_record(status=ld.STATUS_PENDING)
        sent = calls[1][1]
        assert sent["QueueUrl"] == QUEUE_URL
        assert json.loads(sent["MessageBody"]) == {"user_id": "u1", "upload_id": "up-1"}
        assert await _drain(service) == []
        s3.get_object.assert_not_called()
        repository.update_file_digest.assert_not_awaited()

    @pytest.mark.parametrize(
        "size,mime,filename",
        [
            (1000, "application/pdf", "small.pdf"),
            (LARGE, "text/csv", "big.csv"),
            (LARGE, "application/vnd.openxmlformats-officedocument.presentationml.presentation", "deck.pptx"),
        ],
    )
    async def test_everything_else_keeps_the_in_process_digest_path(self, enabled, size, mime, filename):
        service, repository, _, sqs, _ = _service(_meta(size, mime, filename))
        await service.complete_upload("u1", "up-1")
        await _drain(service)
        sqs.send_message.assert_not_called()
        repository.update_file_preparation.assert_not_awaited()

    @pytest.mark.parametrize(
        "env",
        [
            {"LARGE_DOCUMENT_ATTACHMENTS_ENABLED": "", "LARGE_DOCUMENT_PREPARE_QUEUE_URL": QUEUE_URL},
            {"LARGE_DOCUMENT_ATTACHMENTS_ENABLED": "true", "LARGE_DOCUMENT_PREPARE_QUEUE_URL": ""},
        ],
    )
    async def test_feature_off_or_no_queue_sends_nothing_and_still_reads_nothing(self, monkeypatch, env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        service, repository, s3, sqs, _ = _service(_meta())
        response = await service.complete_upload("u1", "up-1")
        assert response.status == "ready"
        assert await _drain(service) == []
        sqs.send_message.assert_not_called()
        repository.update_file_preparation.assert_not_awaited()
        s3.get_object.assert_not_called()

    async def test_a_failed_send_marks_the_row_failed_and_the_upload_succeeds(self, enabled):
        sqs = MagicMock()
        sqs.send_message.side_effect = RuntimeError("sqs down")
        service, repository, _, _, calls = _service(_meta(), sqs=sqs)

        response = await service.complete_upload("u1", "up-1")

        assert response.status == "ready"
        assert [c[1]["status"] for c in calls] == ["pending", "failed"]
        assert calls[1][1]["error"] == "RuntimeError"

    async def test_a_row_deleted_before_the_pending_write_is_not_sent(self, enabled):
        service, repository, _, sqs, _ = _service(_meta())
        repository.update_file_preparation = AsyncMock(return_value=None)
        await service.complete_upload("u1", "up-1")
        sqs.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Deletion (moto S3)
# ---------------------------------------------------------------------------

BUCKET = "test-user-files"


@pytest.fixture
def bucket(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def _seed(s3, meta, derived=("text.jsonl", "pages-0001-0010.pdf", "pages-0011-0012.pdf")):
    base = meta.s3_key.rpartition("/")[0]
    s3.put_object(Bucket=BUCKET, Key=meta.s3_key, Body=b"original")
    s3.put_object(Bucket=BUCKET, Key=f"{base}/_thumb.png", Body=b"png")
    for name in derived:
        s3.put_object(Bucket=BUCKET, Key=f"{base}/_derived/{name}", Body=b"x")


def _keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def _deleting_service(s3, files):
    repository = MagicMock()
    by_id = {m.upload_id: m for m in files}
    repository.get_file = AsyncMock(side_effect=lambda user_id, upload_id: by_id.get(upload_id))
    repository.delete_file = AsyncMock(side_effect=lambda user_id, upload_id: by_id.get(upload_id))
    repository.decrement_quota = AsyncMock()
    repository.list_session_files = AsyncMock(return_value=files)
    return FileUploadService(repository=repository, s3_client=s3, bucket_name=BUCKET)


def _ready(upload_id="up-1", size=LARGE, prep=True):
    preparation = ld.preparation_record(status=ld.STATUS_READY, fmt="pdf", page_count=12, shard_pages=10) if prep else None
    return _meta(size=size, status=FileStatus.READY, upload_id=upload_id, preparation=preparation)


@pytest.mark.asyncio
class TestDeletion:
    async def test_delete_file_removes_the_derived_prefix(self, bucket):
        meta = _ready()
        other = _ready("up-2")
        _seed(bucket, meta)
        _seed(bucket, other)

        assert await _deleting_service(bucket, [meta, other]).delete_file("u1", "up-1") is True

        assert _keys(bucket) == sorted([
            "user-files/u1/s1/up-2/manual.pdf",
            "user-files/u1/s1/up-2/_thumb.png",
            "user-files/u1/s1/up-2/_derived/text.jsonl",
            "user-files/u1/s1/up-2/_derived/pages-0001-0010.pdf",
            "user-files/u1/s1/up-2/_derived/pages-0011-0012.pdf",
        ])

    async def test_delete_session_files_removes_every_derived_prefix(self, bucket):
        files = [_ready("up-1"), _ready("up-2", prep=False)]  # the second never reached the Lambda
        for meta in files:
            _seed(bucket, meta)

        assert await _deleting_service(bucket, files).delete_session_files("s1") == 2
        assert _keys(bucket) == []

    async def test_an_inline_class_file_skips_the_listing(self, bucket):
        meta = _meta(size=1000, status=FileStatus.READY)
        _seed(bucket, meta, derived=())
        s3 = MagicMock(wraps=bucket)
        assert await _deleting_service(s3, [meta]).delete_file("u1", "up-1") is True
        s3.get_paginator.assert_not_called()
        assert _keys(bucket) == []

    async def test_a_listing_error_never_blocks_the_delete(self, bucket):
        from botocore.exceptions import ClientError

        meta = _ready()
        _seed(bucket, meta)
        s3 = MagicMock(wraps=bucket)
        s3.get_paginator.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")
        service = _deleting_service(s3, [meta])
        assert await service.delete_file("u1", "up-1") is True
        service.repository.delete_file.assert_awaited_once()
