"""A generated office file gets the same card preview an uploaded one does.

``_store_document`` writes a generated Word or spreadsheet file READY, so it
never passes through the prepare Lambda that writes ``_derived/preview.json``
for uploads. Without its own preview the download card would only ever show a
skeleton. These pin that the preview is written beside the file, in the shape
``GET /files/previews`` reads, and that nothing about it can fail the tool.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from agents.builtin_tools.office import _storage
from apis.shared.files.attachment_preview import decode_preview, preview_key
from tests.shared.test_attachment_preview import DOCX, XLSX, build_xlsx
from tests.shared.test_document_read import build_docx

pytestmark = pytest.mark.asyncio


class _FakeS3:
    def __init__(self, fail_on: str = "") -> None:
        self.objects: Dict[str, Dict[str, Any]] = {}
        self._fail_on = fail_on

    def put_object(self, **kwargs: Any) -> None:
        if self._fail_on and kwargs["Key"].endswith(self._fail_on):
            raise RuntimeError("S3 is down")
        self.objects[kwargs["Key"]] = kwargs


class _FakeRepository:
    def __init__(self) -> None:
        self.rows: List[Any] = []

    async def create_file(self, metadata: Any) -> None:
        self.rows.append(metadata)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch):
    def install(fail_on: str = "") -> tuple[_FakeS3, _FakeRepository]:
        s3 = _FakeS3(fail_on)
        repo = _FakeRepository()
        monkeypatch.setenv("S3_USER_FILES_BUCKET_NAME", "user-files-test")
        monkeypatch.setattr(_storage, "_s3", lambda: s3)
        import apis.shared.files as files

        monkeypatch.setattr(files, "get_file_upload_repository", lambda: repo)
        return s3, repo

    return install


async def test_word_file_gets_its_opening_lines_as_a_preview(storage) -> None:
    s3, repo = storage()
    raw = build_docx(["Consulting Agreement", "Section 1. Services"])

    upload_id, _ = await _storage._store_document("u1", "s1", "redline.docx", raw, DOCX)

    key = f"user-files/u1/s1/{upload_id}/redline.docx"
    assert key in s3.objects
    stored = s3.objects[preview_key(key)]
    assert stored["ContentType"] == "application/json"
    preview = decode_preview(stored["Body"])
    assert preview is not None
    assert preview["kind"] == "doc"
    assert preview["lines"][:2] == ["Consulting Agreement", "Section 1. Services"]
    assert [row.upload_id for row in repo.rows] == [upload_id]


async def test_spreadsheet_gets_its_first_rows_as_a_preview(storage) -> None:
    s3, _ = storage()
    raw = build_xlsx({"Budget": [["Item", "Cost"], ["Travel", 1200]]})

    upload_id, _ = await _storage._store_document("u1", "s1", "budget.xlsx", raw, XLSX)

    preview = decode_preview(s3.objects[preview_key(f"user-files/u1/s1/{upload_id}/budget.xlsx")]["Body"])
    assert preview is not None
    assert preview["kind"] == "sheet"
    assert preview["rows"][0] == ["Item", "Cost"]


async def test_a_file_with_no_preview_format_writes_only_itself(storage) -> None:
    s3, repo = storage()

    await _storage._store_document("u1", "s1", "notes.json", json.dumps({}).encode(), "application/json")

    assert len(s3.objects) == 1
    assert len(repo.rows) == 1


async def test_an_unparseable_file_is_still_stored_without_a_preview(storage) -> None:
    s3, repo = storage()

    upload_id, size_kb = await _storage._store_document("u1", "s1", "broken.docx", b"not a zip", DOCX)

    assert list(s3.objects) == [f"user-files/u1/s1/{upload_id}/broken.docx"]
    assert len(repo.rows) == 1
    assert size_kb == "0.0 KB"


async def test_a_failed_preview_write_never_fails_the_tool(storage) -> None:
    s3, repo = storage(fail_on="_derived/preview.json")

    upload_id, _ = await _storage._store_document(
        "u1", "s1", "redline.docx", build_docx(["Agreement"]), DOCX
    )

    assert list(s3.objects) == [f"user-files/u1/s1/{upload_id}/redline.docx"]
    assert len(repo.rows) == 1
