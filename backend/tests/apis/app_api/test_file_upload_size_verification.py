"""Stored-size verification on upload completion (files/service.py).

The presigned PUT does not sign Content-Length, so ``request_presigned_url``
only sees the size the client declared. ``complete_upload`` holds the stored
object (its HEAD ``ContentLength``) to the size cap and to that declaration:
an object that fails is marked FAILED, deleted from S3, and never counted
against quota.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.app_api.files.service import (
    FileTooLargeError,
    FileUploadService,
    UploadSizeMismatchError,
)
from apis.shared.files.models import FileMetadata, FileStatus

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MAX = 4 * 1024 * 1024
PRESENTATION_MAX = 25 * 1024 * 1024


def _meta(size_bytes=1024, mime="text/csv", filename="data.csv"):
    return FileMetadata(
        upload_id="up-1", user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size_bytes, s3_key="user-files/u1/s1/up-1/" + filename, s3_bucket="b",
        status=FileStatus.PENDING,
    )


def _service(meta, head):
    repository = MagicMock()
    repository.get_file = AsyncMock(return_value=meta)
    repository.update_file_status = AsyncMock()
    repository.increment_quota = AsyncMock()
    s3 = MagicMock()
    s3.head_object.return_value = head
    service = FileUploadService(
        repository=repository, s3_client=s3, bucket_name="b",
        max_file_size=MAX, presentation_max_file_size=PRESENTATION_MAX,
    )
    return service, repository, s3


def _assert_rejected(repository, s3, meta):
    repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.FAILED)
    repository.increment_quota.assert_not_awaited()
    s3.delete_object.assert_called_once_with(Bucket="b", Key=meta.s3_key)


@pytest.mark.asyncio
async def test_matching_size_within_cap_completes_and_counts_stored_bytes():
    meta = _meta(size_bytes=1024)
    service, repository, s3 = _service(meta, {"ContentLength": 1024})

    response = await service.complete_upload("u1", "up-1")

    assert response.status == FileStatus.READY.value
    assert response.size_bytes == 1024
    repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.READY)
    repository.increment_quota.assert_awaited_once_with("u1", 1024)
    s3.delete_object.assert_not_called()
    s3.head_object.assert_called_once()


@pytest.mark.asyncio
async def test_stored_object_over_cap_is_rejected_even_when_declared_small():
    meta = _meta(size_bytes=1024)
    service, repository, s3 = _service(meta, {"ContentLength": MAX + 1})

    with pytest.raises(FileTooLargeError) as exc:
        await service.complete_upload("u1", "up-1")

    assert exc.value.size_bytes == MAX + 1
    assert exc.value.max_size == MAX
    _assert_rejected(repository, s3, meta)


@pytest.mark.asyncio
async def test_cap_is_the_one_for_the_files_class():
    """A deck over the general cap but under its own cap is accepted."""
    size = MAX + 1
    meta = _meta(size_bytes=size, mime=PPTX, filename="deck.pptx")
    service, repository, s3 = _service(meta, {"ContentLength": size})

    await service.complete_upload("u1", "up-1")

    repository.increment_quota.assert_awaited_once_with("u1", size)
    s3.delete_object.assert_not_called()


@pytest.mark.asyncio
async def test_deck_over_its_own_cap_is_rejected():
    meta = _meta(size_bytes=1024, mime=PPTX, filename="deck.pptx")
    service, repository, s3 = _service(meta, {"ContentLength": PRESENTATION_MAX + 1})

    with pytest.raises(FileTooLargeError) as exc:
        await service.complete_upload("u1", "up-1")

    assert exc.value.max_size == PRESENTATION_MAX
    _assert_rejected(repository, s3, meta)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [1023, 1025])
async def test_stored_size_differing_from_declared_is_rejected(stored):
    meta = _meta(size_bytes=1024)
    service, repository, s3 = _service(meta, {"ContentLength": stored})

    with pytest.raises(UploadSizeMismatchError) as exc:
        await service.complete_upload("u1", "up-1")

    assert exc.value.declared_bytes == 1024
    assert exc.value.stored_bytes == stored
    _assert_rejected(repository, s3, meta)


@pytest.mark.asyncio
async def test_missing_content_length_fails_closed():
    meta = _meta(size_bytes=1024)
    service, repository, s3 = _service(meta, {})

    with pytest.raises(UploadSizeMismatchError):
        await service.complete_upload("u1", "up-1")

    _assert_rejected(repository, s3, meta)


@pytest.mark.asyncio
async def test_s3_delete_failure_still_rejects():
    meta = _meta(size_bytes=1024)
    service, repository, s3 = _service(meta, {"ContentLength": MAX + 1})
    s3.delete_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DeleteObject"
    )

    with pytest.raises(FileTooLargeError):
        await service.complete_upload("u1", "up-1")

    repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.FAILED)
    repository.increment_quota.assert_not_awaited()
