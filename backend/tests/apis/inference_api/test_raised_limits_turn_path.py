"""The route carries out phase 4b's raised limits (spec §4.4.1).

The planner's rows are in ``tests/apis/shared/files/test_attachment_plan.py``;
this file pins the route's side:

- admission's facts reach the planner (an image's two copies, a PDF's page count);
- a ``compact`` image is sent as its compact bytes, with no read;
- a PDF judged by its pages before its facts goes on demand, from the row in hand;
- the resolver fetches a turn's files concurrently, and an image's two copies at once.
"""

from __future__ import annotations

import base64
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import FileContent
from apis.inference_api.chat.routes import TurnAttachments, TurnModel, _carry_out_token_plan, _file_facts
from apis.shared.files import models as file_models
from apis.shared.files.attachment_plan import (
    MODE_NATIVE,
    MODE_ON_DEMAND,
    MODE_TEXT,
    REASON_FACTS_PENDING,
    legacy_profile,
    plan_attachments,
    token_profile,
)
from apis.shared.files.file_resolver import FileResolver

PDF = "application/pdf"
JPEG = "image/jpeg"


def _b64(n: int) -> str:
    return base64.b64encode(b"x" * n).decode()


def _image_row(upload_id: str, *, compact_size: int = 300_000) -> file_models.FileMetadata:
    key = f"user-files/u/s/{upload_id}/photo.jpg"
    return file_models.FileMetadata(
        upload_id=upload_id, user_id="user-1", session_id="sess-1", filename=f"{upload_id}.jpg",
        mime_type=JPEG, size_bytes=9_000_000, s3_key=key, s3_bucket="b", status=file_models.FileStatus.READY,
        model_image={
            "version": 1, "key": f"user-files/u/s/{upload_id}/_derived/model-image.jpg", "mimeType": JPEG,
            "sizeBytes": 1_400_000, "width": 2_348, "height": 1_565, "sourceWidth": 6_000, "sourceHeight": 4_000,
            "downscaled": True,
            "compact": {
                "key": f"user-files/u/s/{upload_id}/_derived/model-image-compact.jpg", "mimeType": JPEG,
                "sizeBytes": compact_size, "width": 1_530, "height": 1_020,
            },
        },
    )


def _pdf_row(upload_id: str, *, pages: int, size: int = 1_400_000) -> file_models.FileMetadata:
    return file_models.FileMetadata(
        upload_id=upload_id, user_id="user-1", session_id="sess-1", filename=f"{upload_id}.pdf",
        mime_type=PDF, size_bytes=size, s3_key=f"k/{upload_id}", s3_bucket="b",
        status=file_models.FileStatus.READY, page_count=pages,
    )


def _model(model_id: str, window: int) -> TurnModel:
    return TurnModel(
        model_id=model_id, provider="bedrock", caching_enabled=None, inference_params={},
        mantle_api_mode=None, mantle_region=None, input_modalities=["TEXT", "IMAGE"], context_window=window,
    )


HAIKU55 = ("us.anthropic.claude-haiku-5-5", 100_000)
SONNET46 = ("us.anthropic.claude-sonnet-4-6", 1_000_000)


@pytest.fixture(autouse=True)
def _compaction(monkeypatch):
    monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "true")
    for name in ("AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS", "AGENTCORE_MEMORY_COMPACTION_CEILING_RATIO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(routes, "record_reference_attachments", lambda refs: None)


def _attachments(rows, sizes, profile, compact=True) -> TurnAttachments:
    files = [FileContent(filename=r.filename, content_type=r.mime_type, bytes=_b64(n)) for r, n in zip(rows, sizes)]
    upload_rows = {id(f): r for f, r in zip(files, rows)}
    compact_images = {
        id(f): (_b64(11), "image/webp") for f, r in zip(files, rows) if compact and r.model_image
    }
    facts = [_file_facts(f, upload_rows) for f in files]
    plan = plan_attachments(facts, None, profile=profile)
    inline = plan.indices(MODE_NATIVE, MODE_TEXT)
    return TurnAttachments(
        files_to_send=[files[i] for i in inline], marker_names=[r.filename for r in rows], plan=plan,
        plan_facts=facts, inline_indices=inline, profile=profile, upload_rows=upload_rows,
        compact_images=compact_images,
    )


class TestAdmissionFactsReachThePlanner:
    def test_an_images_two_copies(self):
        row = _image_row("up-1")
        f = FileContent(filename="p.jpg", content_type=JPEG, bytes=_b64(10))
        facts = _file_facts(f, {id(f): row})
        assert (facts.image_width, facts.image_height) == (2_348, 1_565)
        assert (facts.compact_bytes, facts.compact_width, facts.compact_height) == (300_000, 1_530, 1_020)

    def test_a_pdfs_admission_page_count_without_facts(self):
        row = _pdf_row("up-1", pages=159)
        f = FileContent(filename="a.pdf", content_type=PDF, bytes=_b64(10))
        facts = _file_facts(f, {id(f): row})
        assert facts.page_count == 159 and facts.text_tokens is None

    def test_a_row_without_either_gives_nothing(self):
        row = _pdf_row("up-1", pages=0).model_copy(update={"page_count": None})
        f = FileContent(filename="a.pdf", content_type=PDF, bytes=_b64(10))
        facts = _file_facts(f, {id(f): row})
        assert facts.page_count is None and facts.image_width is None


class TestCompactImages:
    def test_a_standard_tier_model_is_sent_the_compact_bytes(self):
        rows = [_image_row(f"up-{i}") for i in range(3)]
        attachments = _attachments(rows, [1_400_000] * 3, token_profile())
        _carry_out_token_plan(attachments, _model(*SONNET46))

        assert [f.bytes for f in attachments.files_to_send] == [_b64(11)] * 3
        assert {f.content_type for f in attachments.files_to_send} == {"image/webp"}
        # The rows follow the swapped files, for the persist and the cost row.
        assert all(id(f) in attachments.upload_rows for f in attachments.files_to_send)

    def test_a_high_tier_model_keeps_the_high_copy_when_it_fits(self):
        rows = [_image_row(f"up-{i}") for i in range(3)]
        attachments = _attachments(rows, [1_400_000] * 3, token_profile())
        _carry_out_token_plan(attachments, _model(*HAIKU55))

        assert [len(base64.b64decode(f.bytes)) for f in attachments.files_to_send] == [1_400_000] * 3

    def test_over_the_bytes_every_image_goes_compact_on_any_model(self):
        rows = [_image_row(f"up-{i}") for i in range(20)]
        attachments = _attachments(rows, [1_400_000] * 20, token_profile())
        _carry_out_token_plan(attachments, _model(*HAIKU55))

        assert len(attachments.files_to_send) == 20
        assert {f.bytes for f in attachments.files_to_send} == {_b64(11)}

    def test_without_the_compact_bytes_the_high_copy_stays(self):
        rows = [_image_row("up-1")]
        attachments = _attachments(rows, [1_400_000], token_profile(), compact=False)
        _carry_out_token_plan(attachments, _model(*SONNET46))
        assert len(base64.b64decode(attachments.files_to_send[0].bytes)) == 1_400_000

    def test_the_legacy_profile_never_swaps(self):
        rows = [_image_row(f"up-{i}") for i in range(3)]
        attachments = _attachments(rows, [1_000_000] * 3, legacy_profile())
        _carry_out_token_plan(attachments, _model(*SONNET46))
        assert [len(base64.b64decode(f.bytes)) for f in attachments.files_to_send] == [1_000_000] * 3


class TestPendingPdf:
    def test_a_159_page_pdf_before_its_facts_goes_on_demand(self):
        rows = [_pdf_row("up-long", pages=159)]
        attachments = _attachments(rows, [1_400_000], token_profile())
        _carry_out_token_plan(attachments, _model(*SONNET46))

        assert attachments.files_to_send == []
        entry = attachments.plan.entries[0]
        assert (entry.mode, entry.reason) == (MODE_ON_DEMAND, REASON_FACTS_PENDING)
        assert [r.filename for r in attachments.reference_documents] == ["up-long.pdf"]

    def test_a_short_one_stays_native(self):
        rows = [_pdf_row("up-short", pages=3, size=60_000)]
        attachments = _attachments(rows, [60_000], token_profile())
        _carry_out_token_plan(attachments, _model(*HAIKU55))
        assert [f.filename for f in attachments.files_to_send] == ["up-short.pdf"]


class TestResolver:
    @pytest.mark.asyncio
    async def test_files_are_fetched_concurrently_in_attachment_order(self, monkeypatch):
        rows = {f"up-{i}": _pdf_row(f"up-{i}", pages=1, size=10) for i in range(6)}
        repo = MagicMock()
        repo.get_file = AsyncMock(side_effect=lambda user_id, upload_id: rows[upload_id])
        monkeypatch.setattr("apis.shared.files.file_resolver.get_file_upload_repository", lambda: repo)
        active = {"now": 0, "max": 0}
        lock = threading.Lock()

        def get_object(Bucket, Key):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            body = MagicMock()
            body.read.return_value = Key.encode()
            return {"Body": body}

        s3 = MagicMock()
        s3.get_object.side_effect = get_object
        resolved = await FileResolver(s3_client=s3).resolve_files("user-1", list(rows), max_files=None)

        assert [r.filename for r in resolved] == [f"up-{i}.pdf" for i in range(6)]
        assert active["max"] > 1

    @pytest.mark.asyncio
    async def test_an_image_brings_its_compact_copy(self, monkeypatch):
        row = _image_row("up-1")
        repo = MagicMock()
        repo.get_file = AsyncMock(return_value=row)
        monkeypatch.setattr("apis.shared.files.file_resolver.get_file_upload_repository", lambda: repo)
        s3 = MagicMock()

        def get_object(Bucket, Key):
            body = MagicMock()
            body.read.return_value = b"compact" if "compact" in Key else b"high"
            return {"Body": body}

        s3.get_object.side_effect = get_object
        [resolved] = await FileResolver(s3_client=s3).resolve_files("user-1", ["up-1"])

        assert base64.b64decode(resolved.bytes) == b"high"
        assert base64.b64decode(resolved.compact_bytes) == b"compact"
        assert resolved.compact_content_type == JPEG

    @pytest.mark.asyncio
    async def test_a_missing_compact_copy_never_fails_the_file(self, monkeypatch):
        from botocore.exceptions import ClientError

        row = _image_row("up-1")
        repo = MagicMock()
        repo.get_file = AsyncMock(return_value=row)
        monkeypatch.setattr("apis.shared.files.file_resolver.get_file_upload_repository", lambda: repo)

        def get_object(Bucket, Key):
            if "compact" in Key:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            body = MagicMock()
            body.read.return_value = b"high"
            return {"Body": body}

        s3 = MagicMock()
        s3.get_object.side_effect = get_object
        [resolved] = await FileResolver(s3_client=s3).resolve_files("user-1", ["up-1"])
        assert base64.b64decode(resolved.bytes) == b"high" and resolved.compact_bytes is None
