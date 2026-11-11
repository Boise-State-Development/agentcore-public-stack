"""app-api serves the PDF thumbnail the prepare Lambda rendered, and never runs
PDFium itself while the prepare queue exists (files/service.py, moto S3)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from apis.app_api.files.service import FileUploadService
from apis.app_api.files.thumbnails import ThumbnailPending
from apis.shared.files.models import FileMetadata, FileStatus

BUCKET = "test-thumbs"
QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/test-large-document-prepare"
KEY = "user-files/u1/s1/up-1/report.pdf"
DERIVED = "user-files/u1/s1/up-1/_derived/thumb.png"
LEGACY = "user-files/u1/s1/up-1/_thumb.png"


@pytest.fixture
def bucket(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"%PDF")
        yield s3


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
    monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", QUEUE_URL)


@pytest.fixture
def no_queue(monkeypatch):
    monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "false")
    monkeypatch.delenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", raising=False)


def _service(s3):
    meta = FileMetadata(
        upload_id="up-1", user_id="u1", session_id="s1", filename="report.pdf", mime_type="application/pdf",
        size_bytes=2000, s3_key=KEY, s3_bucket=BUCKET, status=FileStatus.READY,
    )
    repository = MagicMock()
    repository.get_file = AsyncMock(return_value=meta)
    renderer = MagicMock()
    renderer.render.return_value = b"\x89PNG in-process"
    sqs = MagicMock()
    service = FileUploadService(
        repository=repository, s3_client=s3, bucket_name=BUCKET, sqs_client=sqs, thumbnail_renderer=renderer,
    )
    return service, renderer, sqs


@pytest.mark.asyncio
class TestThumbnail:
    async def test_serves_the_lambdas_thumbnail_without_rendering(self, bucket, queue):
        bucket.put_object(Bucket=BUCKET, Key=DERIVED, Body=b"\x89PNG")
        service, renderer, sqs = _service(bucket)
        response = await service.get_or_create_thumbnail("u1", "up-1")
        assert "_derived/thumb.png" in response.url and response.cached is True
        renderer.render.assert_not_called()
        sqs.send_message.assert_not_called()

    async def test_serves_a_thumbnail_rendered_before_the_lambda_did(self, bucket, queue):
        bucket.put_object(Bucket=BUCKET, Key=LEGACY, Body=b"\x89PNG")
        service, renderer, sqs = _service(bucket)
        response = await service.get_or_create_thumbnail("u1", "up-1")
        assert "_thumb.png" in response.url and "_derived" not in response.url
        renderer.render.assert_not_called()

    async def test_a_missing_thumbnail_is_queued_never_rendered_here(self, bucket, queue):
        service, renderer, sqs = _service(bucket)
        with pytest.raises(ThumbnailPending):
            await service.get_or_create_thumbnail("u1", "up-1")
        renderer.render.assert_not_called()
        kwargs = sqs.send_message.call_args.kwargs
        assert kwargs["QueueUrl"] == QUEUE_URL
        assert json.loads(kwargs["MessageBody"]) == {"action": "thumbnail", "user_id": "u1", "upload_id": "up-1"}

    async def test_a_failed_send_still_never_renders_here(self, bucket, queue):
        service, renderer, sqs = _service(bucket)
        sqs.send_message.side_effect = RuntimeError("throttled")
        with pytest.raises(ThumbnailPending):
            await service.get_or_create_thumbnail("u1", "up-1")
        renderer.render.assert_not_called()

    async def test_without_the_prepare_queue_it_renders_in_process_as_before(self, bucket, no_queue):
        service, renderer, sqs = _service(bucket)
        response = await service.get_or_create_thumbnail("u1", "up-1")
        renderer.render.assert_called_once()
        sqs.send_message.assert_not_called()
        assert bucket.get_object(Bucket=BUCKET, Key=LEGACY)["Body"].read() == b"\x89PNG in-process"
        assert response.cached is False


def test_the_route_answers_202_while_a_render_is_queued():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apis.app_api.files import routes
    from apis.shared.auth.dependencies import get_current_user_from_session

    service = MagicMock()
    service.get_or_create_thumbnail = AsyncMock(side_effect=ThumbnailPending("queued"))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: MagicMock(user_id="u1")
    app.dependency_overrides[routes.get_file_upload_service] = lambda: service
    prefix = routes.router.prefix or ""
    response = TestClient(app).get(f"{prefix}/up-1/thumbnail")
    assert response.status_code == 202
    assert response.json() == {"status": "rendering"}
