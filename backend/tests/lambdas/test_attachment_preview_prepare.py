"""The prepare Lambda writes a card preview to ``_derived/preview.json`` (moto S3 + DynamoDB)."""

from __future__ import annotations

import json

import pytest

from apis.shared.files.attachment_preview import decode_preview
from lambdas.large_document_prepare import handler
from tests.lambdas.test_large_document_prepare import _body, _keys, _upload, store  # noqa: F401 - fixture
from tests.shared.test_attachment_preview import PPTX, XLSX, build_pptx, build_xlsx
from tests.shared.test_document_read import build_docx

PREVIEW = "user-files/u1/s1/up-1/_derived/preview.json"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.asyncio
class TestPreview:
    async def test_the_facts_pass_writes_a_word_files_preview(self, store):
        s3, repository = store
        raw = build_docx(["Memo to the board", "Costs held flat"])
        await _upload(store, raw, filename="memo.docx", mime=DOCX, size=len(raw))
        assert await handler.compute_facts("u1", "up-1", s3=s3, repository=repository) == "ready"
        assert decode_preview(_body(s3, PREVIEW))["lines"] == ["Memo to the board", "Costs held flat"]

    # Builders, not bytes: a zip's headers carry its build time, so bytes in a
    # parametrize id differ between xdist workers and break collection.
    @pytest.mark.parametrize(
        "filename,mime,build,kind",
        [
            ("budget.xlsx", XLSX, lambda: build_xlsx({"Budget": [["Line", "FY27"], ["Travel", 1200]]}), "sheet"),
            ("grades.csv", "text/csv", lambda: b"Student,Score\nAvery,94\n", "sheet"),
            ("talk.pptx", PPTX, lambda: build_pptx([("Quarterly review", ["Revenue up"])]), "deck"),
        ],
        ids=["xlsx", "csv", "pptx"],
    )
    async def test_the_preview_action_writes_spreadsheets_csvs_and_decks(self, store, filename, mime, build, kind):
        s3, repository = store
        raw = build()
        await _upload(store, raw, filename=filename, mime=mime, size=len(raw))
        assert await handler.render_preview("u1", "up-1", s3=s3, repository=repository) == "ready"
        assert decode_preview(_body(s3, PREVIEW))["kind"] == kind

    async def test_a_file_that_will_not_parse_leaves_no_preview(self, store):
        s3, repository = store
        await _upload(store, b"not a workbook", filename="budget.xlsx", mime=XLSX, size=14)
        assert await handler.render_preview("u1", "up-1", s3=s3, repository=repository) == "failed"
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    async def test_a_pdf_has_no_preview(self, store):
        s3, repository = store
        await _upload(store, b"%PDF", size=4)
        assert await handler.render_preview("u1", "up-1", s3=s3, repository=repository) == "no_preview"


def test_a_preview_message_parses_to_its_kind():
    body = json.dumps({"action": "preview", "user_id": "u1", "upload_id": "up-1"})
    assert handler._parse(body) == {"kind": "preview", "user_id": "u1", "upload_id": "up-1"}
