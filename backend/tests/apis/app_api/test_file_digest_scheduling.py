"""DocumentDigest scheduling on upload completion (files/service.py).

``complete_upload`` queues a digest build for document uploads only, off the
request path, with a strong task reference; the build reads S3, calls the
builder and persists the map; nothing on this path can fail the upload or
raise out of the task.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.files.service import FileUploadService
from apis.shared.files.document_digest import DocumentDigest
from apis.shared.files.models import FileMetadata, FileStatus

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _meta(mime="application/pdf", filename="policy.pdf", status=FileStatus.PENDING):
    return FileMetadata(
        upload_id="up-1", user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=10, s3_key="k", s3_bucket="b", status=status,
    )


def _service(meta, body=b"%PDF"):
    repository = MagicMock()
    repository.get_file = AsyncMock(return_value=meta)
    repository.update_file_status = AsyncMock()
    repository.increment_quota = AsyncMock()
    repository.update_file_digest = AsyncMock(return_value=meta)
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
    return FileUploadService(repository=repository, s3_client=s3, bucket_name="b")


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.delenv("DOCUMENT_DIGEST_ENABLED", raising=False)


async def _drain(service):
    tasks = list(service._digest_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime,filename,scheduled",
    [
        ("application/pdf", "a.pdf", True),
        (DOCX, "a.docx", True),
        ("text/markdown", "a.md", True),
        ("text/csv", "a.csv", False),
        ("image/png", "a.png", False),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "a.pptx", False),
    ],
)
async def test_complete_upload_schedules_a_digest_for_documents_only(monkeypatch, mime, filename, scheduled):
    built = DocumentDigest(status="ready", format="pdf", count=1, tokens=50)
    build = AsyncMock(return_value=built)
    monkeypatch.setattr("apis.shared.files.document_digest.build_digest", build)
    service = _service(_meta(mime, filename))

    response = await service.complete_upload("u1", "up-1")
    assert response.status == "ready"
    tasks = await _drain(service)

    assert bool(tasks) is scheduled
    if scheduled:
        build.assert_awaited_once()
        assert build.await_args.kwargs["raw"] == b"%PDF"
        service.repository.update_file_digest.assert_awaited_once_with("u1", "up-1", built.to_item())
    else:
        service.repository.update_file_digest.assert_not_awaited()
    assert not service._digest_tasks  # done-callback dropped the strong ref


@pytest.mark.asyncio
async def test_kill_switch_skips_scheduling(monkeypatch):
    monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "false")
    service = _service(_meta())
    await service.complete_upload("u1", "up-1")
    assert service._schedule_digest(_meta()) is None and not service._digest_tasks


@pytest.mark.asyncio
async def test_build_failures_never_escape_the_task(monkeypatch):
    monkeypatch.setattr("apis.shared.files.document_digest.build_digest", AsyncMock(side_effect=RuntimeError("boom")))
    service = _service(_meta())
    await service.complete_upload("u1", "up-1")
    await _drain(service)  # gather would re-raise if the task did
    service.repository.update_file_digest.assert_not_awaited()

    service.repository.get_file = AsyncMock(return_value=_meta())
    service._s3_client.get_object.side_effect = RuntimeError("s3 down")
    monkeypatch.setattr("apis.shared.files.document_digest.build_digest", AsyncMock())
    await service.complete_upload("u1", "up-1")
    await _drain(service)


@pytest.mark.asyncio
async def test_a_deleted_row_is_tolerated(monkeypatch):
    monkeypatch.setattr("apis.shared.files.document_digest.build_digest", AsyncMock(return_value=DocumentDigest()))
    service = _service(_meta())
    service.repository.update_file_digest = AsyncMock(return_value=None)
    await service.complete_upload("u1", "up-1")
    await _drain(service)


def test_scheduling_without_a_running_loop_is_a_noop():
    service = _service(_meta())
    assert service._schedule_digest(_meta()) is None
