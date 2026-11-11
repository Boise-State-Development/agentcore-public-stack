"""An image's model copy is planned and stored when its upload completes (spec §4.3, §4.4).

Under the ``token`` profile every image is inspected before its row turns READY:
one over the models' limits gets a deterministic downscaled copy at
``_derived/model-image.<ext>``, one already inside them records the original as
its model image, and the resolver sends whatever the row names. Under the
``legacy`` profile nothing changes: no read, no copy, the 4 MB image limit.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from apis.app_api.files import service as service_module
from apis.app_api.files.service import (
    FileTooLargeError,
    FileUploadService,
    ImageUnreadableUploadError,
)
from apis.shared.files import attachment_plan
from apis.shared.files.attachment_plan import (
    TOKEN_IMAGE_UPLOAD_MAX_BYTES,
    client_limits,
    legacy_profile,
    token_profile,
    upload_limits,
    upload_size_limit,
)
from apis.shared.files.file_resolver import FileResolver, model_image_source
from apis.shared.files.image_downscale import (
    COMPACT_IMAGE_MAX_BYTES,
    COMPACT_LONG_EDGE_PX,
    MODEL_IMAGE_MAX_BYTES,
    target_size,
)
from apis.shared.files.models import FileMetadata, FileStatus

MAX = 4 * 1024 * 1024


def _jpeg(w: int, h: int, quality: int = 95) -> bytes:
    import random

    img = Image.frombytes("RGB", (w, h), random.Random(3).randbytes(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _flat_jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 120, 200)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _meta(size_bytes: int, mime: str = "image/jpeg", filename: str = "photo.jpg") -> FileMetadata:
    return FileMetadata(
        upload_id="up-1", user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size_bytes, s3_key="user-files/u1/s1/up-1/" + filename, s3_bucket="b",
        status=FileStatus.PENDING,
    )


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(attachment_plan, "current_limits_profile", token_profile)
    monkeypatch.setattr(service_module, "current_limits_profile", token_profile)


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(attachment_plan, "current_limits_profile", legacy_profile)
    monkeypatch.setattr(service_module, "current_limits_profile", legacy_profile)


def _service(meta: FileMetadata, raw: bytes):
    repository = MagicMock()
    repository.get_file = AsyncMock(return_value=meta)
    order: list = []
    repository.update_file_status = AsyncMock(side_effect=lambda *a, **k: order.append(("status", a[2])))

    async def _model_image(user_id, upload_id, record):
        order.append(("model_image", record))
        return meta.model_copy(update={"model_image": record})

    repository.update_file_model_image = AsyncMock(side_effect=_model_image)
    repository.increment_quota = AsyncMock()
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": len(raw)}
    body = MagicMock()
    body.read.return_value = raw
    s3.get_object.return_value = {"Body": body}
    service = FileUploadService(repository=repository, s3_client=s3, bucket_name="b", max_file_size=MAX)
    service._after_upload_ready = AsyncMock()
    return service, repository, s3, order


class TestTokenProfile:
    @pytest.mark.asyncio
    async def test_a_large_photo_is_accepted_and_its_copy_stored_before_ready(self, token):
        raw = _jpeg(6000, 4000, quality=75)
        assert MAX < len(raw) <= TOKEN_IMAGE_UPLOAD_MAX_BYTES
        meta = _meta(len(raw))
        service, repository, s3, order = _service(meta, raw)

        await service.complete_upload("u1", "up-1")

        puts = {c.kwargs["Key"]: c.kwargs for c in s3.put_object.call_args_list}
        assert set(puts) == {
            "user-files/u1/s1/up-1/_derived/model-image.jpg",
            "user-files/u1/s1/up-1/_derived/model-image-compact.jpg",
        }
        put = puts["user-files/u1/s1/up-1/_derived/model-image.jpg"]
        assert put["ContentType"] == "image/jpeg"
        assert len(put["Body"]) <= MODEL_IMAGE_MAX_BYTES
        kinds = [k for k, _ in order]
        assert kinds == ["model_image", "status"], "the copy is recorded before the row turns READY"
        record = order[0][1]
        assert record["key"] == put["Key"] and record["downscaled"] is True
        assert (record["width"], record["height"]) == target_size(6000, 4000)
        assert (record["sourceWidth"], record["sourceHeight"]) == (6000, 4000)
        assert record["sizeBytes"] == len(put["Body"])
        compact = record["compact"]
        assert compact["key"] == "user-files/u1/s1/up-1/_derived/model-image-compact.jpg"
        assert max(compact["width"], compact["height"]) <= COMPACT_LONG_EDGE_PX
        assert compact["sizeBytes"] <= COMPACT_IMAGE_MAX_BYTES
        # Quota counts what the user uploaded.
        repository.increment_quota.assert_awaited_once_with("u1", len(raw))

    @pytest.mark.asyncio
    async def test_an_image_inside_the_limits_records_its_original(self, token):
        raw = _flat_jpeg(900, 600)
        service, repository, s3, order = _service(_meta(len(raw)), raw)

        await service.complete_upload("u1", "up-1")

        s3.put_object.assert_not_called()
        record = order[0][1]
        assert record["key"] == "user-files/u1/s1/up-1/photo.jpg"
        assert record["downscaled"] is False
        assert (record["width"], record["height"]) == (900, 600)
        assert record["compact"]["key"] == record["key"]

    @pytest.mark.asyncio
    async def test_an_unreadable_image_over_the_general_limit_is_rejected(self, token):
        raw = b"\x00" * (MAX + 10)
        meta = _meta(len(raw), mime="image/png", filename="broken.png")
        service, repository, s3, _ = _service(meta, raw)

        with pytest.raises(ImageUnreadableUploadError) as exc:
            await service.complete_upload("u1", "up-1")

        assert "broken.png" in str(exc.value)
        repository.update_file_status.assert_awaited_once_with("u1", "up-1", FileStatus.FAILED)
        repository.increment_quota.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_small_image_is_accepted_as_uploaded(self, token):
        raw = b"\x00" * 2048
        service, repository, s3, order = _service(_meta(len(raw), mime="image/png", filename="x.png"), raw)

        await service.complete_upload("u1", "up-1")

        assert [k for k, _ in order] == ["status"]
        repository.update_file_model_image.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_twenty_megabytes_is_the_image_ceiling(self, token):
        size = TOKEN_IMAGE_UPLOAD_MAX_BYTES + 1
        service, *_ = _service(_meta(size), b"")
        assert service.max_size_for("photo.jpg", "image/jpeg") == TOKEN_IMAGE_UPLOAD_MAX_BYTES
        from apis.shared.files.models import PresignRequest

        with pytest.raises(FileTooLargeError):
            await service.request_presigned_url(
                "u1", PresignRequest(session_id="s1", filename="photo.jpg", mime_type="image/jpeg", size_bytes=size)
            )

    @pytest.mark.asyncio
    async def test_import_stores_the_copy_from_the_bytes_in_hand(self, token):
        raw = _jpeg(5000, 3000)
        meta = _meta(len(raw))
        service, repository, s3, order = _service(meta, raw)
        repository.get_user_quota = AsyncMock(return_value=MagicMock(total_bytes=0))
        repository.create_file = AsyncMock()

        await service.import_file("u1", "s1", "photo.jpg", "image/jpeg", raw)

        s3.get_object.assert_not_called()
        keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
        assert any(k.endswith("/_derived/model-image.jpg") for k in keys)
        assert [k for k, _ in order][-2:] == ["model_image", "status"]

    def test_limits_endpoint_serves_the_image_ceiling(self):
        body = client_limits(token_profile(), upload_limits(token_profile()))
        assert body["maxBytes"]["image"] == TOKEN_IMAGE_UPLOAD_MAX_BYTES


class TestLegacyProfileUnchanged:
    @pytest.mark.asyncio
    async def test_no_read_no_copy(self, legacy):
        raw = _jpeg(3000, 2000, quality=60)
        service, repository, s3, order = _service(_meta(len(raw)), raw)

        await service.complete_upload("u1", "up-1")

        s3.get_object.assert_not_called()
        s3.put_object.assert_not_called()
        repository.update_file_model_image.assert_not_awaited()

    def test_image_ceiling_is_the_general_limit(self):
        limits = upload_limits(legacy_profile())
        assert limits.image_max_bytes is None
        assert upload_size_limit("photo.jpg", "image/jpeg", limits) == limits.general_max_bytes
        body = client_limits(legacy_profile(), limits)
        assert body["maxBytes"]["image"] == limits.general_max_bytes


class TestResolverSendsTheModelCopy:
    def test_source_is_the_copy_when_recorded(self):
        meta = _meta(10).model_copy(
            update={"model_image": {"key": "user-files/u1/s1/up-1/_derived/model-image.webp", "mimeType": "image/webp"}}
        )
        assert model_image_source(meta) == ("user-files/u1/s1/up-1/_derived/model-image.webp", "image/webp")

    def test_compact_variant(self):
        meta = _meta(10).model_copy(
            update={
                "model_image": {
                    "key": "k/high.jpg", "mimeType": "image/jpeg",
                    "compact": {"key": "k/compact.jpg", "mimeType": "image/jpeg"},
                }
            }
        )
        assert model_image_source(meta, "compact") == ("k/compact.jpg", "image/jpeg")
        assert model_image_source(meta) == ("k/high.jpg", "image/jpeg")
        no_compact = meta.model_copy(update={"model_image": {"key": "k/high.jpg", "mimeType": "image/jpeg"}})
        assert model_image_source(no_compact, "compact") == ("k/high.jpg", "image/jpeg")

    def test_source_is_the_original_without_one(self):
        meta = _meta(10)
        assert model_image_source(meta) == (meta.s3_key, "image/jpeg")

    @pytest.mark.asyncio
    async def test_resolver_reads_the_copy(self, monkeypatch):
        meta = _meta(10, mime="image/png", filename="shot.png").model_copy(
            update={
                "status": FileStatus.READY,
                "model_image": {"key": "k/_derived/model-image.jpg", "mimeType": "image/jpeg"},
            }
        )
        repo = MagicMock()
        repo.get_file = AsyncMock(return_value=meta)
        monkeypatch.setattr("apis.shared.files.file_resolver.get_file_upload_repository", lambda: repo)
        s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b"jpegbytes"
        s3.get_object.return_value = {"Body": body}

        resolved = await FileResolver(s3_client=s3).resolve_files("u1", ["up-1"])

        s3.get_object.assert_called_once_with(Bucket="b", Key="k/_derived/model-image.jpg")
        assert resolved[0].content_type == "image/jpeg"
        assert resolved[0].filename == "shot.png"
