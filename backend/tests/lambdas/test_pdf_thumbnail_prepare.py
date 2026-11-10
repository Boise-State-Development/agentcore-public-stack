"""The prepare Lambda renders a PDF's first page to ``_derived/thumb.png``
(moto S3 + DynamoDB), on both its paths and for a PDF uploaded before it did."""

from __future__ import annotations

import json

import pytest

from lambdas.large_document_prepare import handler
from tests.lambdas.test_large_document_prepare import _body, _keys, _upload, store  # noqa: F401 - fixture
from tests.shared.test_document_read import build_docx, build_pdf

THUMB = "user-files/u1/s1/up-1/_derived/thumb.png"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.asyncio
class TestThumbnail:
    async def test_the_facts_pass_renders_a_small_pdf(self, store):
        s3, repository = store
        raw = build_pdf(["Syllabus", "Week two"])
        await _upload(store, raw, size=len(raw))
        assert await handler.compute_facts("u1", "up-1", s3=s3, repository=repository) == "ready"
        assert _body(s3, THUMB).startswith(b"\x89PNG\r\n\x1a\n")

    async def test_the_facts_pass_renders_nothing_for_a_word_file(self, store):
        s3, repository = store
        raw = build_docx(["Memo"])
        await _upload(store, raw, filename="memo.docx", mime=DOCX, size=len(raw))
        assert await handler.compute_facts("u1", "up-1", s3=s3, repository=repository) == "ready"
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    async def test_a_pdf_that_will_not_open_still_gets_its_facts_outcome(self, store):
        s3, repository = store
        await _upload(store, b"%PDF-1.4 not a pdf", size=2000)
        assert await handler.compute_facts("u1", "up-1", s3=s3, repository=repository) == "failed"
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    async def test_the_thumbnail_action_renders_an_older_pdf(self, store):
        s3, repository = store
        await _upload(store, build_pdf(["Older upload"]), size=2000)
        assert await handler.render_thumbnail("u1", "up-1", s3=s3, repository=repository) == "ready"
        assert _body(s3, THUMB).startswith(b"\x89PNG")

    @pytest.mark.parametrize(
        "kwargs,outcome",
        [
            ({"raw": b"%PDF-1.4 not a pdf"}, "failed"),
            ({"filename": "memo.docx", "mime": DOCX}, "not_pdf"),
            ({"upload_id": "up-2"}, "missing"),
        ],
    )
    async def test_the_thumbnail_action_skips_what_it_cannot_render(self, store, kwargs, outcome):
        s3, repository = store
        raw = kwargs.pop("raw", build_pdf(["x"]))
        target = kwargs.pop("upload_id", "up-1")
        await _upload(store, raw, size=2000, **kwargs)
        assert await handler.render_thumbnail("u1", target, s3=s3, repository=repository) == outcome
        assert THUMB not in _keys(s3)


def test_a_thumbnail_message_parses_to_its_kind():
    body = json.dumps({"action": "thumbnail", "user_id": "u1", "upload_id": "up-1"})
    assert handler._parse(body) == {"kind": "thumbnail", "user_id": "u1", "upload_id": "up-1"}
