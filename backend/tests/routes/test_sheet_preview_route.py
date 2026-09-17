"""End-to-end tests for GET /files/{upload_id}/sheet-preview.

Unlike `tests/apis/app_api/test_sheet_preview.py`, which exercises the
reader on its own, these drive the whole chain: route -> auth dependency
-> FileUploadService -> S3 body -> openpyxl -> response model. The
service is real; only the repository and the S3 client are stubs, so a
mismatch between what openpyxl returns and what the response model
accepts fails here rather than in production.
"""

from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from openpyxl import Workbook

from apis.app_api.files.routes import router
from apis.app_api.files.service import FileUploadService, get_file_upload_service
from apis.app_api.files.sheet_preview import MAX_WORKBOOK_BYTES
from apis.shared.files.models import FileMetadata, FileStatus

from tests.routes.conftest import mock_service

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook_bytes(rows=None, *, sheet_name="Sheet1") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows if rows is not None else [["Item", "Cost"], ["Rent", 1200]]:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def metadata(**overrides) -> FileMetadata:
    base = dict(
        upload_id="up1",
        user_id="user-1",
        session_id="sess-1",
        filename="budget.xlsx",
        mime_type=XLSX_MIME,
        size_bytes=4096,
        s3_key="users/user-1/up1/budget.xlsx",
        s3_bucket="test-bucket",
        status=FileStatus.READY,
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return FileMetadata(**base)


@pytest.fixture
def s3_body():
    """Mutable holder for whatever the stub S3 client returns."""
    return {"data": workbook_bytes()}


@pytest.fixture
def file_meta():
    return {"value": metadata()}


@pytest.fixture
def app(s3_body, file_meta):
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **_: {
        "Body": BytesIO(s3_body["data"])
    }

    repository = MagicMock()
    repository.get_file = AsyncMock(side_effect=lambda *_: file_meta["value"])

    service = FileUploadService(
        repository=repository,
        s3_client=s3,
        bucket_name="test-bucket",
        thumbnail_renderer=MagicMock(),
    )

    _app = FastAPI()
    _app.include_router(router)
    mock_service(_app, get_file_upload_service, service)
    return _app


class TestSuccess:
    def test_returns_sheets_as_rows_of_strings(
        self, app, make_user, authenticated_client
    ):
        client = authenticated_client(app, make_user(user_id="user-1"))

        response = client.get("/files/up1/sheet-preview")

        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "budget.xlsx"
        assert body["truncated"] is False
        [sheet] = body["sheets"]
        assert sheet["name"] == "Sheet1"
        assert sheet["headers"] == ["Item", "Cost"]
        assert sheet["rows"] == [["Rent", "1200"]]

    def test_serialises_with_camelCase_aliases(
        self, app, make_user, authenticated_client
    ):
        # The SPA's SheetPreview interface reads these names directly.
        client = authenticated_client(app, make_user(user_id="user-1"))

        [sheet] = client.get("/files/up1/sheet-preview").json()["sheets"]

        assert "totalRows" in sheet
        assert "truncatedBy" in sheet
        assert "total_rows" not in sheet

    def test_the_workbook_bytes_never_leave_the_server(
        self, app, make_user, authenticated_client
    ):
        # The whole point of this route: no presigned URL, no bytes.
        client = authenticated_client(app, make_user(user_id="user-1"))

        raw = client.get("/files/up1/sheet-preview").content

        assert b"PK\x03\x04" not in raw  # the zip magic an .xlsx starts with

    def test_shows_formula_text_where_no_value_was_cached(
        self, app, make_user, authenticated_client, s3_body
    ):
        # openpyxl does not evaluate, so a workbook it wrote carries no
        # cached value — a values-only read would show a blank total.
        s3_body["data"] = workbook_bytes(
            [["Item", "Cost"], ["Rent", 1200], ["Total", "=SUM(B2:B2)"]]
        )
        client = authenticated_client(app, make_user(user_id="user-1"))

        [sheet] = client.get("/files/up1/sheet-preview").json()["sheets"]

        assert sheet["rows"][1] == ["Total", "=SUM(B2:B2)"]


class TestRejections:
    def test_401_without_a_session(self, app, unauthenticated_client):
        client = unauthenticated_client(app)

        assert client.get("/files/up1/sheet-preview").status_code == 401

    def test_404_when_the_file_is_not_the_callers(
        self, app, make_user, authenticated_client, file_meta
    ):
        file_meta["value"] = None
        client = authenticated_client(app, make_user(user_id="user-1"))

        assert client.get("/files/up1/sheet-preview").status_code == 404

    def test_404_while_the_upload_is_still_pending(
        self, app, make_user, authenticated_client, file_meta
    ):
        file_meta["value"] = metadata(status=FileStatus.PENDING)
        client = authenticated_client(app, make_user(user_id="user-1"))

        assert client.get("/files/up1/sheet-preview").status_code == 404

    def test_415_for_a_format_that_is_not_a_workbook(
        self, app, make_user, authenticated_client, file_meta
    ):
        # .xls shares nothing with OOXML; the UI should never have asked.
        file_meta["value"] = metadata(
            filename="old.xls", mime_type="application/vnd.ms-excel"
        )
        client = authenticated_client(app, make_user(user_id="user-1"))

        assert client.get("/files/up1/sheet-preview").status_code == 415

    def test_413_before_downloading_an_oversized_workbook(
        self, app, make_user, authenticated_client, file_meta
    ):
        # Checked against recorded metadata, so an oversized file costs a
        # metadata read rather than a transfer into memory.
        file_meta["value"] = metadata(size_bytes=MAX_WORKBOOK_BYTES + 1)
        client = authenticated_client(app, make_user(user_id="user-1"))

        assert client.get("/files/up1/sheet-preview").status_code == 413

    def test_422_for_bytes_that_are_not_a_workbook(
        self, app, make_user, authenticated_client, s3_body
    ):
        s3_body["data"] = b"not a workbook at all"
        client = authenticated_client(app, make_user(user_id="user-1"))

        response = client.get("/files/up1/sheet-preview")

        assert response.status_code == 422
        # openpyxl's own message names internal XML parts; the user gets
        # something they can act on instead.
        assert "corrupt or password-protected" in response.json()["detail"]
