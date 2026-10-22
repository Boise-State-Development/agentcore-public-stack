"""Tests for file upload, listing, deletion, and quota routes.

Endpoints under test:
- POST   /files/presign              → 200 with presigned URL
- POST   /files/presign              → 400 for unsupported MIME type
- POST   /files/presign              → 400 for oversized file
- POST   /files/presign              → 403 for exceeded quota
- POST   /files/{upload_id}/complete → 200 for valid upload
- POST   /files/{upload_id}/complete → 404 for nonexistent upload
- POST   /files/{upload_id}/complete → 400 when the stored object exceeds the cap
- POST   /files/{upload_id}/complete → 409 when the stored size differs from the declared size
- GET    /files                      → 200 with paginated file list
- DELETE /files/{upload_id}          → 204 for owned file
- DELETE /files/{upload_id}          → 404 for nonexistent file
- GET    /files/quota                → 200 with quota usage data

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from apis.app_api.files.routes import router
from apis.app_api.files.service import (
    FileUploadService,
    get_file_upload_service,
    QuotaExceededError,
    InvalidFileTypeError,
    FileTooLargeError,
    UploadSizeMismatchError,
    FileNotFoundError as ServiceFileNotFoundError,
)
from apis.shared.files.models import (
    PresignResponse,
    CompleteUploadResponse,
    FileListResponse,
    FileResponse,
    QuotaResponse,
)

from tests.routes.conftest import mock_auth_user, mock_no_auth, mock_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_file_service():
    """Create an AsyncMock of FileUploadService."""
    return AsyncMock(spec=FileUploadService)


@pytest.fixture
def app(mock_file_service):
    """Minimal FastAPI app mounting only the files router with mocked service."""
    _app = FastAPI()
    _app.include_router(router)
    mock_service(_app, get_file_upload_service, mock_file_service)
    return _app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_PRESIGN_PAYLOAD = {
    "sessionId": "sess-001",
    "filename": "report.pdf",
    "mimeType": "application/pdf",
    "sizeBytes": 1024,
}


# ---------------------------------------------------------------------------
# Requirement 4.1: POST /files/presign with valid request returns 200
# Requirement 4.2: POST /files/presign with unsupported MIME type returns 400
# Requirement 4.3: POST /files/presign with oversized file returns 400
# Requirement 4.4: POST /files/presign with exceeded quota returns 403
# ---------------------------------------------------------------------------


class TestPresignUrl:
    """POST /files/presign endpoint tests."""

    def test_returns_200_with_presigned_url(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.1: Valid presign request returns 200 with presigned URL and upload ID."""
        client = authenticated_client(app, make_user())

        mock_file_service.request_presigned_url.return_value = PresignResponse(
            upload_id="upload-001",
            presigned_url="https://s3.amazonaws.com/bucket/key?signed",
            expires_at="2025-01-01T01:00:00Z",
        )

        resp = client.post("/files/presign", json=VALID_PRESIGN_PAYLOAD)

        assert resp.status_code == 200
        body = resp.json()
        assert body["uploadId"] == "upload-001"
        assert "presignedUrl" in body
        assert "expiresAt" in body

    def test_returns_400_for_unsupported_mime_type(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.2: Unsupported MIME type returns 400."""
        client = authenticated_client(app, make_user())

        mock_file_service.request_presigned_url.side_effect = InvalidFileTypeError(
            "application/zip"
        )

        payload = {**VALID_PRESIGN_PAYLOAD, "mimeType": "application/zip"}
        resp = client.post("/files/presign", json=payload)

        assert resp.status_code == 400
        assert "Unsupported file type" in resp.json()["detail"]

    def test_returns_400_for_oversized_file(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.3: File exceeding size limit returns 400."""
        client = authenticated_client(app, make_user())

        mock_file_service.request_presigned_url.side_effect = FileTooLargeError(
            size_bytes=10_000_000, max_size=4_194_304
        )

        payload = {**VALID_PRESIGN_PAYLOAD, "sizeBytes": 10_000_000}
        resp = client.post("/files/presign", json=payload)

        assert resp.status_code == 400
        assert "limit" in resp.json()["detail"]

    def test_returns_403_for_exceeded_quota(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.4: Exceeded quota returns 403."""
        client = authenticated_client(app, make_user())

        mock_file_service.request_presigned_url.side_effect = QuotaExceededError(
            current_usage=1_073_741_000,
            max_allowed=1_073_741_824,
            required_space=2_000_000,
        )

        resp = client.post("/files/presign", json=VALID_PRESIGN_PAYLOAD)

        assert resp.status_code == 403
        body = resp.json()["detail"]
        assert body["error"] == "QUOTA_EXCEEDED"

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Presign endpoint requires authentication."""
        client = unauthenticated_client(app)
        resp = client.post("/files/presign", json=VALID_PRESIGN_PAYLOAD)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Requirement 4.5: POST /files/{upload_id}/complete for valid upload returns 200
# Requirement 4.6: POST /files/{upload_id}/complete for nonexistent upload returns 404
# ---------------------------------------------------------------------------


class TestCompleteUpload:
    """POST /files/{upload_id}/complete endpoint tests."""

    def test_returns_200_for_valid_upload(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.5: Completing a valid upload returns 200."""
        client = authenticated_client(app, make_user())

        mock_file_service.complete_upload.return_value = CompleteUploadResponse(
            upload_id="upload-001",
            status="ready",
            s3_uri="s3://bucket/key",
            filename="report.pdf",
            size_bytes=1024,
        )

        resp = client.post("/files/upload-001/complete")

        assert resp.status_code == 200
        body = resp.json()
        assert body["uploadId"] == "upload-001"
        assert body["status"] == "ready"
        assert body["filename"] == "report.pdf"

    def test_returns_404_for_nonexistent_upload(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.6: Completing a nonexistent upload returns 404."""
        client = authenticated_client(app, make_user())

        mock_file_service.complete_upload.side_effect = ServiceFileNotFoundError(
            "Upload not-exist not found"
        )

        resp = client.post("/files/not-exist/complete")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_returns_400_when_stored_object_exceeds_cap(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        client = authenticated_client(app, make_user())

        mock_file_service.complete_upload.side_effect = FileTooLargeError(
            size_bytes=50 * 1024 * 1024, max_size=4 * 1024 * 1024
        )

        resp = client.post("/files/upload-001/complete")

        assert resp.status_code == 400
        assert "4MB" in resp.json()["detail"]

    def test_returns_409_when_stored_size_differs_from_declared(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        client = authenticated_client(app, make_user())

        mock_file_service.complete_upload.side_effect = UploadSizeMismatchError(
            declared_bytes=1024, stored_bytes=2048
        )

        resp = client.post("/files/upload-001/complete")

        assert resp.status_code == 409
        assert "does not match" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Requirement 4.7: GET /files returns 200 with paginated file list
# ---------------------------------------------------------------------------


class TestListFiles:
    """GET /files endpoint tests."""

    def test_returns_200_with_file_list(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.7: Listing files returns 200 with paginated results."""
        client = authenticated_client(app, make_user())

        mock_file_service.list_user_files.return_value = FileListResponse(
            files=[
                FileResponse(
                    upload_id="upload-001",
                    filename="report.pdf",
                    mime_type="application/pdf",
                    size_bytes=1024,
                    session_id="sess-001",
                    s3_uri="s3://bucket/key",
                    status="ready",
                    created_at="2025-01-01T00:00:00Z",
                ),
            ],
            next_cursor="cursor-abc",
            total_count=None,
        )

        resp = client.get("/files")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["files"]) == 1
        assert body["files"][0]["uploadId"] == "upload-001"
        assert body["nextCursor"] == "cursor-abc"

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Listing files requires authentication."""
        client = unauthenticated_client(app)
        resp = client.get("/files")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Requirement 4.8: DELETE /files/{upload_id} for owned file returns 204
# Requirement 4.9: DELETE /files/{upload_id} for nonexistent file returns 404
# ---------------------------------------------------------------------------


class TestDeleteFile:
    """DELETE /files/{upload_id} endpoint tests."""

    def test_returns_204_for_owned_file(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.8: Deleting an owned file returns 204."""
        client = authenticated_client(app, make_user())

        mock_file_service.delete_file.return_value = True

        resp = client.delete("/files/upload-001")

        assert resp.status_code == 204

    def test_returns_404_for_nonexistent_file(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.9: Deleting a nonexistent file returns 404."""
        client = authenticated_client(app, make_user())

        mock_file_service.delete_file.return_value = False

        resp = client.delete("/files/not-exist")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Requirement 4.10: GET /files/quota returns 200 with quota usage data
# ---------------------------------------------------------------------------


class TestGetQuota:
    """GET /files/quota endpoint tests."""

    def test_returns_200_with_quota_data(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        """Req 4.10: Quota endpoint returns 200 with usage data."""
        client = authenticated_client(app, make_user())

        mock_file_service.get_user_quota.return_value = QuotaResponse(
            used_bytes=500_000,
            max_bytes=1_073_741_824,
            file_count=3,
        )

        resp = client.get("/files/quota")

        assert resp.status_code == 200
        body = resp.json()
        assert body["usedBytes"] == 500_000
        assert body["maxBytes"] == 1_073_741_824
        assert body["fileCount"] == 3

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        """Quota endpoint requires authentication."""
        client = unauthenticated_client(app)
        resp = client.get("/files/quota")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /files/{upload_id}/download — the durable download link
# ---------------------------------------------------------------------------


class TestDownloadFile:
    """GET /files/{upload_id}/download endpoint tests.

    This route exists because presigned S3 URLs expire in minutes, so any URL
    written into a conversation (a download card, a link in an assistant
    message) is dead when the thread is reopened. The route is the stable path;
    the signature is minted per click.
    """

    def test_redirects_to_a_freshly_minted_presigned_url(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        client = authenticated_client(app, make_user())
        mock_file_service.get_download_url.return_value = (
            "https://bucket.s3.us-west-2.amazonaws.com/user-files/u/s/up1/plan.docx"
            "?X-Amz-Signature=deadbeef"
        )

        resp = client.get("/files/up1/download", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"].endswith("X-Amz-Signature=deadbeef")
        # Never cached: the Location carries a signature that expires.
        assert resp.headers["cache-control"] == "no-store"

    def test_returns_404_for_unowned_or_missing_file(
        self, app, make_user, authenticated_client, mock_file_service
    ):
        client = authenticated_client(app, make_user())
        mock_file_service.get_download_url.side_effect = ServiceFileNotFoundError(
            "nope"
        )

        resp = client.get("/files/ghost/download", follow_redirects=False)

        assert resp.status_code == 404

    def test_returns_401_for_unauthenticated(self, app, unauthenticated_client):
        client = unauthenticated_client(app)
        resp = client.get("/files/up1/download", follow_redirects=False)
        assert resp.status_code == 401
