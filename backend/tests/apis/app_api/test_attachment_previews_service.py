"""``GET /files/previews`` and the preview message (files/service.py, routes.py; moto S3)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from apis.app_api.files.service import FileUploadService
from apis.shared.files.attachment_preview import encode_preview
from apis.shared.files.models import FileMetadata, FileStatus

BUCKET = "test-previews"
QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/test-large-document-prepare"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _meta(upload_id: str, filename: str, mime: str, status=FileStatus.READY, user="u1") -> FileMetadata:
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id="s1", filename=filename, mime_type=mime, size_bytes=10,
        s3_key=f"user-files/{user}/s1/{upload_id}/{filename}", s3_bucket=BUCKET, status=status,
    )


@pytest.fixture
def bucket(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def _service(s3, rows: dict, sqs=None):
    repository = MagicMock()
    # Owner-scoped like the real repository: a row is found only for its own user.
    repository.get_file = AsyncMock(
        side_effect=lambda user_id, upload_id: rows.get(upload_id) if rows.get(upload_id) and rows[upload_id].user_id == user_id else None
    )
    return FileUploadService(repository=repository, s3_client=s3, bucket_name=BUCKET, sqs_client=sqs or MagicMock())


@pytest.mark.asyncio
class TestGetPreviews:
    async def test_returns_each_written_preview_and_null_for_the_rest(self, bucket):
        rows = {
            "doc": _meta("doc", "memo.docx", DOCX),
            "sheet": _meta("sheet", "budget.xlsx", XLSX),
            "pdf": _meta("pdf", "report.pdf", "application/pdf"),
            "pending": _meta("pending", "plan.docx", DOCX, status=FileStatus.PENDING),
            "theirs": _meta("theirs", "secret.docx", DOCX, user="u2"),
        }
        preview = {"version": 1, "kind": "doc", "lines": ["Memo"]}
        for upload_id in ("doc", "pending", "theirs"):
            meta = rows[upload_id]
            bucket.put_object(Bucket=BUCKET, Key=f"user-files/{meta.user_id}/s1/{upload_id}/_derived/preview.json", Body=encode_preview(preview))

        result = await _service(bucket, rows).get_previews("u1", ["doc", "sheet", "pdf", "pending", "theirs", "doc"])

        assert result == {"doc": preview, "sheet": None, "pdf": None, "pending": None, "theirs": None}


@pytest.mark.asyncio
class TestEnqueuePreview:
    @pytest.fixture(autouse=True)
    def queue(self, monkeypatch):
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
        monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", QUEUE_URL)

    @pytest.mark.parametrize(
        "filename,mime,sent",
        [
            ("budget.xlsx", XLSX, True),
            ("grades.csv", "text/csv", True),
            ("talk.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", True),
            ("memo.docx", DOCX, False),  # its facts pass writes the preview
            ("report.pdf", "application/pdf", False),
            ("photo.png", "image/png", False),
        ],
    )
    async def test_queues_only_formats_no_other_pass_reaches(self, filename, mime, sent):
        sqs = MagicMock()
        service = _service(MagicMock(), {}, sqs=sqs)
        await service._enqueue_preview(_meta("up-1", filename, mime))
        if sent:
            body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
            assert body == {"action": "preview", "user_id": "u1", "upload_id": "up-1"}
        else:
            sqs.send_message.assert_not_called()

    async def test_a_failed_send_never_raises(self):
        sqs = MagicMock()
        sqs.send_message.side_effect = RuntimeError("throttled")
        await _service(MagicMock(), {}, sqs=sqs)._enqueue_preview(_meta("up-1", "budget.xlsx", XLSX))


def _client(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apis.app_api.files import routes
    from apis.shared.auth.dependencies import get_current_user_from_session

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: MagicMock(user_id="u1")
    app.dependency_overrides[routes.get_file_upload_service] = lambda: service
    return TestClient(app), routes.router.prefix or ""


def test_the_route_answers_for_the_ids_asked():
    service = MagicMock()
    service.get_previews = AsyncMock(return_value={"a": None, "b": {"version": 1, "kind": "doc", "lines": ["x"]}})
    client, prefix = _client(service)
    response = client.get(f"{prefix}/previews", params={"ids": "a, b"})
    assert response.status_code == 200
    assert response.json() == {"previews": {"a": None, "b": {"version": 1, "kind": "doc", "lines": ["x"]}}}
    service.get_previews.assert_awaited_once_with("u1", ["a", "b"])


@pytest.mark.parametrize("ids", ["", " , ", ",".join(f"id{i}" for i in range(21))])
def test_the_route_refuses_no_ids_or_more_than_twenty(ids):
    service = MagicMock()
    service.get_previews = AsyncMock()
    client, prefix = _client(service)
    assert client.get(f"{prefix}/previews", params={"ids": ids}).status_code == 400
    service.get_previews.assert_not_awaited()
