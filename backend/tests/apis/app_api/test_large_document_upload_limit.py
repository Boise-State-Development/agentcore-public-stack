"""Document-class uploads up to 50 MB while large-document attachments are on.

``docs/specs/large-document-attachments.md`` §4A (PR-2). ``max_size_for`` is the
one decision point both gates read (presign on the declared size, PR-0's
completion check on the stored object), so raising it there raises both and
nothing else: spreadsheets, images and decks keep their own caps, and with the
flag off every document is back at the inline limit.

Also pins the thumbnail source for a reference-class PDF: its first page shard,
never the 50 MB original in app-api's memory.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.files.service import FileTooLargeError, FileUploadService
from apis.app_api.files.thumbnails import ThumbnailRenderError
from apis.shared.files import large_document
from apis.shared.files.models import FileMetadata, FileStatus, LARGE_DOCUMENT_MAX_BYTES, PresignRequest

MB = 1024 * 1024
GENERAL_CAP = 4 * MB
PRESENTATION_CAP = 25 * MB
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")


@pytest.fixture
def service():
    repository = MagicMock()
    repository.get_user_quota = AsyncMock(return_value=SimpleNamespace(total_bytes=0))
    repository.create_file = AsyncMock()
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://example.invalid/put"
    return FileUploadService(
        repository=repository,
        s3_client=s3,
        bucket_name="b",
        max_file_size=GENERAL_CAP,
        presentation_max_file_size=PRESENTATION_CAP,
    )


class TestMaxSizeFor:
    @pytest.mark.parametrize(
        "filename,mime",
        [
            ("manual.pdf", "application/pdf"),
            ("handbook.docx", DOCX),
            ("log.txt", "text/plain"),
            ("notes.md", "text/markdown"),
            ("page.html", "text/html"),
        ],
    )
    def test_document_class_gets_the_large_document_ceiling(self, service, flag_on, filename, mime):
        assert service.max_size_for(filename, mime) == LARGE_DOCUMENT_MAX_BYTES == 50 * MB

    @pytest.mark.parametrize(
        "filename,mime,cap",
        [
            ("sales.xlsx", XLSX, GENERAL_CAP),
            ("data.csv", "text/csv", GENERAL_CAP),
            ("photo.png", "image/png", GENERAL_CAP),
            ("deck.pptx", PPTX, PRESENTATION_CAP),
        ],
    )
    def test_other_classes_keep_their_caps(self, service, flag_on, filename, mime, cap):
        assert service.max_size_for(filename, mime) == cap

    @pytest.mark.parametrize("value", ["false", " FALSE "])
    def test_off_only_when_false(self, service, monkeypatch, value):
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", value)

        assert service.max_size_for("manual.pdf", "application/pdf") == GENERAL_CAP


class TestPresign:
    @pytest.mark.asyncio
    async def test_a_38_mb_pdf_is_presigned_with_the_flag_on(self, service, flag_on):
        request = PresignRequest(sessionId="s1", filename="manual.pdf", mimeType="application/pdf", sizeBytes=38 * MB)

        response = await service.request_presigned_url("u1", request)

        assert response.upload_id

    @pytest.mark.asyncio
    async def test_over_the_ceiling_names_the_real_ceiling(self, service, flag_on):
        request = PresignRequest(
            sessionId="s1", filename="manual.pdf", mimeType="application/pdf", sizeBytes=50 * MB + 1
        )

        with pytest.raises(FileTooLargeError) as exc:
            await service.request_presigned_url("u1", request)

        assert exc.value.max_size == 50 * MB

    @pytest.mark.asyncio
    async def test_with_the_flag_off_a_large_pdf_is_refused_at_the_inline_limit(self, service, monkeypatch):
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "false")
        request = PresignRequest(sessionId="s1", filename="manual.pdf", mimeType="application/pdf", sizeBytes=5 * MB)

        with pytest.raises(FileTooLargeError) as exc:
            await service.request_presigned_url("u1", request)

        assert exc.value.max_size == GENERAL_CAP


def _row(size_bytes: int, preparation: dict | None = None, status: FileStatus = FileStatus.READY) -> FileMetadata:
    return FileMetadata(
        upload_id="up-1",
        user_id="u1",
        session_id="s1",
        filename="manual.pdf",
        mime_type="application/pdf",
        size_bytes=size_bytes,
        s3_key="user-files/u1/s1/up-1/manual.pdf",
        s3_bucket="b",
        status=status,
        preparation=preparation,
    )


class TestCompletion:
    @pytest.mark.asyncio
    async def test_a_large_stored_object_completes_and_is_queued_not_digested(self, flag_on, monkeypatch):
        monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "https://sqs.invalid/q")
        meta = _row(38 * MB, status=FileStatus.PENDING)
        repository = MagicMock()
        repository.get_file = AsyncMock(return_value=meta)
        repository.update_file_status = AsyncMock()
        repository.increment_quota = AsyncMock()
        repository.update_file_preparation = AsyncMock(return_value=meta)
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 38 * MB}
        sqs = MagicMock()
        service = FileUploadService(repository=repository, s3_client=s3, bucket_name="b", max_file_size=GENERAL_CAP)
        service._sqs_client = sqs
        service._schedule_digest = MagicMock()

        await service.complete_upload("u1", "up-1")

        repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.READY)
        s3.delete_object.assert_not_called()
        sqs.send_message.assert_called_once()
        service._schedule_digest.assert_not_called()
        s3.get_object.assert_not_called()


class TestThumbnailSource:
    def test_an_inline_class_pdf_renders_from_the_original(self):
        assert FileUploadService._thumbnail_source_key(_row(1 * MB)) == "user-files/u1/s1/up-1/manual.pdf"

    def test_a_prepared_large_pdf_renders_from_its_first_shard(self):
        prep = large_document.preparation_record(
            status=large_document.STATUS_READY, fmt="pdf", page_count=312, shard_pages=10
        )

        key = FileUploadService._thumbnail_source_key(_row(38 * MB, prep))

        assert key == "user-files/u1/s1/up-1/_derived/pages-0001-0010.pdf"

    @pytest.mark.parametrize(
        "prep",
        [
            None,
            large_document.preparation_record(status=large_document.STATUS_PENDING),
            large_document.preparation_record(status=large_document.STATUS_FAILED),
        ],
    )
    def test_an_unprepared_large_pdf_is_never_read_whole(self, prep):
        with pytest.raises(ThumbnailRenderError):
            FileUploadService._thumbnail_source_key(_row(38 * MB, prep))

    @pytest.mark.asyncio
    async def test_render_reads_the_shard(self):
        prep = large_document.preparation_record(
            status=large_document.STATUS_READY, fmt="pdf", page_count=3, shard_pages=10
        )
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"%PDF"))}
        renderer = MagicMock()
        renderer.render.return_value = b"PNG"
        service = FileUploadService(repository=MagicMock(), s3_client=s3, bucket_name="b")
        service._thumbnail_renderer = renderer

        await service._render_and_store_thumbnail(_row(38 * MB, prep), "user-files/u1/s1/up-1/_thumb.png")

        s3.get_object.assert_called_once_with(Bucket="b", Key="user-files/u1/s1/up-1/_derived/pages-0001-0003.pdf")
