"""`FileUploadService.import_file`: a file-source download stored as a ready upload.

The presign flow in one call, for bytes app-api already holds. These tests
pin the gates it shares with `request_presigned_url` (type, class size cap,
quota), the row lifecycle (PENDING → PUT → READY → quota), and that the
post-ready step is the one `complete_upload` runs (digest or preparation).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.app_api.files.service import (
    FileTooLargeError,
    FileUploadService,
    InvalidFileTypeError,
    QuotaExceededError,
    FileUploadError,
)
from apis.shared.files.models import (
    INLINE_DOCUMENT_MAX_BYTES,
    LARGE_DOCUMENT_MAX_BYTES,
    FileStatus,
    UserFileQuota,
)

QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/test-large-document-prepare"
GENERAL_CAP = 4 * 1024 * 1024
DECK_CAP = 25 * 1024 * 1024


def _service(*, used_bytes: int = 0, quota_bytes: int = 1024 * 1024 * 1024, put_error=None):
    repository = MagicMock()
    repository.get_user_quota = AsyncMock(
        return_value=UserFileQuota(user_id="u1", total_bytes=used_bytes)
    )
    # A copy, because the service flips the row it created to READY later
    # and the tests want the status it was created with.
    created: list = []
    repository.create_file = AsyncMock(
        side_effect=lambda meta: created.append(meta.model_copy()) or meta
    )
    repository.update_file_status = AsyncMock()
    repository.increment_quota = AsyncMock()
    repository.update_file_digest = AsyncMock()
    repository.update_file_preparation = AsyncMock(side_effect=lambda *a, **k: created[0])
    s3 = MagicMock()
    if put_error is not None:
        s3.put_object.side_effect = put_error
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"hello"))}
    sqs = MagicMock()
    service = FileUploadService(
        repository=repository,
        s3_client=s3,
        bucket_name="bucket",
        max_file_size=GENERAL_CAP,
        presentation_max_file_size=DECK_CAP,
        user_quota_bytes=quota_bytes,
        sqs_client=sqs,
    )
    return service, repository, s3, sqs, created


async def _drain(service):
    tasks = list(service._digest_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.fixture(autouse=True)
def _no_digest(monkeypatch):
    # The in-process digest reads the object back and calls a model; it is
    # not what these tests are about. One test turns it back on explicitly.
    monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "false")
    monkeypatch.delenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", raising=False)


@pytest.mark.asyncio
class TestImportFile:
    async def test_stores_the_bytes_and_returns_a_ready_row(self):
        service, repository, s3, _, created = _service()

        meta = await service.import_file(
            "u1", "s1", "report.pdf", "application/pdf", b"%PDF-1.7", source="connector:google"
        )

        assert meta.status == FileStatus.READY.value or meta.status == FileStatus.READY
        assert meta.user_id == "u1"
        assert meta.session_id == "s1"
        assert meta.filename == "report.pdf"
        assert meta.mime_type == "application/pdf"
        assert meta.size_bytes == 8
        assert meta.source == "connector:google"
        assert meta.s3_key == f"user-files/u1/s1/{meta.upload_id}/report.pdf"
        assert meta.s3_bucket == "bucket"

        # PENDING row first, then the PUT, then READY and the quota.
        assert created[0].status in (FileStatus.PENDING, FileStatus.PENDING.value)
        s3.put_object.assert_called_once_with(
            Bucket="bucket", Key=meta.s3_key, Body=b"%PDF-1.7", ContentType="application/pdf"
        )
        repository.update_file_status.assert_awaited_once_with("u1", meta.upload_id, FileStatus.READY)
        repository.increment_quota.assert_awaited_once_with("u1", 8)

    async def test_mime_type_resolves_from_the_extension_when_the_provider_is_vague(self):
        service, _, s3, _, _ = _service()

        meta = await service.import_file(
            "u1", "s1", "notes.md", "application/octet-stream", b"# hi"
        )

        assert meta.mime_type == "text/markdown"
        assert s3.put_object.call_args.kwargs["ContentType"] == "text/markdown"

    async def test_unsupported_type_is_refused_before_any_write(self):
        service, repository, s3, _, _ = _service()

        with pytest.raises(InvalidFileTypeError):
            await service.import_file("u1", "s1", "archive.zip", "application/zip", b"PK")

        repository.create_file.assert_not_awaited()
        s3.put_object.assert_not_called()

    async def test_size_cap_is_the_file_class_cap(self):
        service, repository, s3, _, _ = _service()
        too_big_for_general = b"x" * (GENERAL_CAP + 1)

        with pytest.raises(FileTooLargeError) as err:
            await service.import_file("u1", "s1", "big.png", "image/png", too_big_for_general)
        assert err.value.max_size == GENERAL_CAP

        # The same byte count is fine for a deck, which has its own cap.
        deck_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        meta = await service.import_file("u1", "s1", "deck.pptx", deck_mime, too_big_for_general)
        assert meta.size_bytes == GENERAL_CAP + 1
        repository.create_file.assert_awaited_once()
        s3.put_object.assert_called_once()

    async def test_quota_is_checked_before_any_write(self):
        service, repository, s3, _, _ = _service(used_bytes=95, quota_bytes=100)

        with pytest.raises(QuotaExceededError) as err:
            await service.import_file("u1", "s1", "a.txt", "text/plain", b"0123456789")

        assert err.value.current_usage == 95
        assert err.value.required_space == 10
        repository.create_file.assert_not_awaited()
        s3.put_object.assert_not_called()

    async def test_a_failed_put_marks_the_row_failed_and_raises(self):
        error = ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutObject")
        service, repository, _, _, created = _service(put_error=error)

        with pytest.raises(FileUploadError):
            await service.import_file("u1", "s1", "a.txt", "text/plain", b"hi")

        repository.update_file_status.assert_awaited_once_with(
            "u1", created[0].upload_id, FileStatus.FAILED
        )
        repository.increment_quota.assert_not_awaited()

    async def test_a_document_gets_the_in_process_digest(self, monkeypatch):
        monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "true")
        service, repository, _, _, _ = _service()

        async def fake_build(**kwargs):
            digest = MagicMock()
            digest.to_item.return_value = {"abstract": "hi"}
            return digest

        monkeypatch.setattr("apis.shared.files.document_digest.build_digest", fake_build)
        monkeypatch.setattr("apis.shared.files.document_digest.record_digest", lambda d: None)

        meta = await service.import_file("u1", "s1", "a.txt", "text/plain", b"hello")
        await _drain(service)

        repository.update_file_digest.assert_awaited_once_with("u1", meta.upload_id, {"abstract": "hi"})

    async def test_a_reference_class_document_is_queued_for_preparation(self, monkeypatch):
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
        monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", QUEUE_URL)
        service, repository, _, sqs, _ = _service()
        content = b"%" * (INLINE_DOCUMENT_MAX_BYTES + 1)

        meta = await service.import_file("u1", "s1", "manual.pdf", "application/pdf", content)

        assert INLINE_DOCUMENT_MAX_BYTES < meta.size_bytes <= LARGE_DOCUMENT_MAX_BYTES
        repository.update_file_preparation.assert_awaited_once()
        sqs.send_message.assert_called_once()
        assert sqs.send_message.call_args.kwargs["QueueUrl"] == QUEUE_URL


    async def test_reference_delivery_queues_a_small_document_for_preparation(self, monkeypatch):
        # A folder file (drive-folder spec §4.3): by reference whatever its size.
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
        monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", QUEUE_URL)
        service, repository, _, sqs, created = _service()

        meta = await service.import_file(
            "u1", "s1", "syllabus.pdf", "application/pdf", b"%PDF small",
            source="connector:google:folder:fa-1", delivery="reference",
        )

        assert meta.size_bytes < INLINE_DOCUMENT_MAX_BYTES
        assert created[0].delivery == "reference"
        assert created[0].source == "connector:google:folder:fa-1"
        repository.update_file_preparation.assert_awaited_once()
        sqs.send_message.assert_called_once()

    async def test_delivery_defaults_to_inline(self):
        service, _, _, sqs, created = _service()
        await service.import_file("u1", "s1", "a.pdf", "application/pdf", b"%PDF")
        assert created[0].delivery == "inline"
        sqs.send_message.assert_not_called()


class TestMaxDownloadBytes:
    def test_is_the_widest_class_cap(self, monkeypatch):
        service, *_ = _service()
        assert service.max_download_bytes() == DECK_CAP

        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
        assert service.max_download_bytes() == LARGE_DOCUMENT_MAX_BYTES
