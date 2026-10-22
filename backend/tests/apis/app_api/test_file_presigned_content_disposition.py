"""Presigned preview/download URLs survive a non-Latin-1 upload filename.

`FileMetadata.filename` is stored verbatim — the model constrains length, not
charset — so whatever the browser sent is what reaches the presigned URL. A
macOS screenshot is the everyday case: Finder names it with a U+202F narrow
no-break space before `AM`/`PM`. S3 rejects a `response-content-disposition`
it cannot encode as latin-1, which surfaced to the user as a broken image
thumbnail ("Preview unavailable") rather than as an error anyone could read.

These pin the header built for both presigned params: ASCII on the wire, real
name carried in RFC 5987 `filename*`. The URL is signed locally by botocore,
so nothing here touches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, unquote, urlparse

import boto3
import pytest
from moto import mock_aws

from apis.app_api.files.service import FileUploadService
from apis.shared.files.models import FileMetadata, FileStatus

REGION = "us-east-1"
BUCKET = "test-user-files"

# The names that used to break the preview. U+202F is what macOS actually
# puts in a screenshot filename; the rest are ordinary things people type.
HOSTILE_NAMES = [
    ("Screenshot 2026-09-21 at 10.22.14 AM.png", "image/png"),
    ("Research \U0001f9e0 Notes.pdf", "application/pdf"),
    ("研究ノート.pdf", "application/pdf"),
    ("Phil’s Deck.pptx", "application/vnd.ms-powerpoint"),
]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture
def service(env):
    """A service over a REAL boto3 S3 client.

    The regression lives inside botocore's request signing, so a MagicMock
    client would sail past it — the point is that the real serializer accepts
    what we hand it.
    """
    repository = MagicMock()
    return FileUploadService(
        repository=repository,
        s3_client=boto3.client("s3", region_name=REGION),
        bucket_name=BUCKET,
    )


def _ready_file(filename: str, mime_type: str) -> FileMetadata:
    return FileMetadata(
        upload_id="abc123",
        user_id="user-1",
        session_id="sess-1",
        filename=filename,
        mime_type=mime_type,
        size_bytes=1024,
        s3_key=f"user-files/user-1/sess-1/abc123/{filename}",
        s3_bucket=BUCKET,
        status=FileStatus.READY,
    )


def _disposition_from(url: str) -> str:
    """Pull the `response-content-disposition` back out of a signed URL."""
    params = parse_qs(urlparse(url).query)
    return params["response-content-disposition"][0]


@pytest.mark.parametrize("filename,mime_type", HOSTILE_NAMES)
@pytest.mark.asyncio
async def test_preview_url_survives_non_latin1_filename(
    service, filename, mime_type
):
    service.repository.get_file = AsyncMock(
        return_value=_ready_file(filename, mime_type)
    )

    resp = await service.get_preview_url("user-1", "abc123")

    cd = _disposition_from(resp.url)
    cd.encode("latin-1")  # what S3 rejected before
    assert cd.startswith("inline; ")
    assert "filename*=UTF-8''" in cd
    # The real name survives, percent-decoded out of `filename*`.
    star = cd.split("filename*=UTF-8''")[1]
    assert unquote(star) == filename
    # The response still reports the true filename to the SPA.
    assert resp.filename == filename


@pytest.mark.parametrize("filename,mime_type", HOSTILE_NAMES)
@pytest.mark.asyncio
async def test_download_url_survives_non_latin1_filename(
    service, filename, mime_type
):
    service.repository.get_file = AsyncMock(
        return_value=_ready_file(filename, mime_type)
    )

    url = await service.get_download_url("user-1", "abc123")

    cd = _disposition_from(url)
    cd.encode("latin-1")
    assert cd.startswith("attachment; ")
    assert unquote(cd.split("filename*=UTF-8''")[1]) == filename


@pytest.mark.asyncio
async def test_ordinary_ascii_filename_is_unchanged_for_old_clients(service):
    service.repository.get_file = AsyncMock(
        return_value=_ready_file("report.pdf", "application/pdf")
    )

    cd = _disposition_from(await service.get_download_url("user-1", "abc123"))

    # A client that ignores `filename*` still saves the right name.
    assert 'filename="report.pdf"' in cd
