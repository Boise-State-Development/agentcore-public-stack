"""A PDF's pages are counted at admission under the ``token`` profile (spec §2, §4.4).

Over 1,000 pages is refused with a message that names the file and the limit;
under it, the count is recorded on the row (``pageCount``) for a turn that
arrives before the file's facts. The count is read by ranges, so a large PDF is
never held in app-api. The ``legacy`` profile reads nothing.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pypdfium2 as pdfium
import pytest

from apis.app_api.files import service as service_module
from apis.app_api.files.service import FileUploadService, TooManyPagesError
from apis.shared.files import attachment_plan
from apis.shared.files.attachment_plan import legacy_profile, token_profile
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.files.pdf_page_count import BLOCK_BYTES, S3RangeReader, count_pages, count_s3_pdf_pages

PDF = "application/pdf"


def _pdf(pages: int) -> bytes:
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(612, 792)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


class _RangeS3:
    """``get_object`` with ``Range`` over one object, counting what it serves."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls = 0
        self.served = 0

    def get_object(self, Bucket, Key, Range=None):
        self.calls += 1
        data = self.raw
        if Range:
            start, end = (int(x) for x in Range.removeprefix("bytes=").split("-"))
            data = self.raw[start : end + 1]
        self.served += len(data)
        body = MagicMock()
        body.read.return_value = data
        return {"Body": body}


def _meta(raw: bytes, filename: str = "reader.pdf") -> FileMetadata:
    return FileMetadata(
        upload_id="up-1", user_id="u1", session_id="s1", filename=filename, mime_type=PDF,
        size_bytes=len(raw), s3_key="user-files/u1/s1/up-1/" + filename, s3_bucket="b",
        status=FileStatus.PENDING,
    )


def _service(meta: FileMetadata, s3) -> tuple:
    repository = MagicMock()
    repository.get_file = AsyncMock(return_value=meta)
    repository.update_file_status = AsyncMock()
    repository.increment_quota = AsyncMock()
    repository.update_file_page_count = AsyncMock(side_effect=lambda u, i, n: meta.model_copy(update={"page_count": n}))
    s3.head_object = MagicMock(return_value={"ContentLength": meta.size_bytes})
    s3.delete_object = MagicMock()
    service = FileUploadService(repository=repository, s3_client=s3, bucket_name="b", max_file_size=4 * 1024 * 1024)
    service._after_upload_ready = AsyncMock()
    return service, repository


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(attachment_plan, "current_limits_profile", token_profile)
    monkeypatch.setattr(service_module, "current_limits_profile", token_profile)


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(attachment_plan, "current_limits_profile", legacy_profile)
    monkeypatch.setattr(service_module, "current_limits_profile", legacy_profile)


class TestCounting:
    def test_counts_from_bytes(self):
        assert count_pages(_pdf(7)) == 7

    def test_counts_by_range_reading_a_fraction(self):
        raw = _pdf(1_200)
        s3 = _RangeS3(raw)
        assert count_s3_pdf_pages(s3, "b", "k", len(raw)) == 1_200
        assert s3.calls <= 2
        assert s3.served <= 2 * BLOCK_BYTES

    def test_reader_is_seekable_and_exact(self):
        raw = bytes(range(256)) * 4_000
        reader = S3RangeReader(_RangeS3(raw), "b", "k", len(raw))
        reader.seek(BLOCK_BYTES - 3)
        assert reader.read(6) == raw[BLOCK_BYTES - 3 : BLOCK_BYTES + 3]
        reader.seek(-10, io.SEEK_END)
        assert reader.read() == raw[-10:]


class TestAdmission:
    @pytest.mark.asyncio
    async def test_over_1000_pages_is_refused_with_its_message(self, token):
        raw = _pdf(1_200)
        meta = _meta(raw, "handbook.pdf")
        service, repository = _service(meta, _RangeS3(raw))

        with pytest.raises(TooManyPagesError) as exc:
            await service.complete_upload("u1", "up-1")

        assert str(exc.value) == (
            "handbook.pdf has 1,200 pages. A PDF can have at most 1,000 pages; split it and attach the parts."
        )
        repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.FAILED)
        repository.increment_quota.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_under_the_limit_the_count_is_recorded_before_ready(self, token):
        raw = _pdf(300)
        meta = _meta(raw)
        service, repository = _service(meta, _RangeS3(raw))

        await service.complete_upload("u1", "up-1")

        repository.update_file_page_count.assert_awaited_once_with("u1", "up-1", 300)
        repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.READY)

    @pytest.mark.asyncio
    async def test_exactly_1000_is_admitted(self, token):
        raw = _pdf(1_000)
        service, repository = _service(_meta(raw), _RangeS3(raw))
        await service.complete_upload("u1", "up-1")
        repository.update_file_page_count.assert_awaited_once_with("u1", "up-1", 1_000)

    @pytest.mark.asyncio
    async def test_an_unreadable_pdf_is_admitted_without_a_count(self, token):
        raw = b"%PDF-1.7 not really"
        service, repository = _service(_meta(raw), _RangeS3(raw))
        await service.complete_upload("u1", "up-1")
        repository.update_file_page_count.assert_not_awaited()
        repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.READY)

    @pytest.mark.asyncio
    async def test_import_counts_the_bytes_in_hand(self, token):
        raw = _pdf(1_001)
        meta = _meta(raw)
        s3 = _RangeS3(raw)
        service, repository = _service(meta, s3)
        repository.get_user_quota = AsyncMock(return_value=MagicMock(total_bytes=0))
        repository.create_file = AsyncMock()
        s3.put_object = MagicMock()

        with pytest.raises(TooManyPagesError):
            await service.import_file("u1", "s1", "big.pdf", PDF, raw)
        assert s3.calls == 0

    @pytest.mark.asyncio
    async def test_the_legacy_profile_reads_nothing(self, legacy):
        raw = _pdf(1_200)
        s3 = _RangeS3(raw)
        service, repository = _service(_meta(raw), s3)
        await service.complete_upload("u1", "up-1")
        assert s3.calls == 0
        repository.update_file_page_count.assert_not_awaited()
