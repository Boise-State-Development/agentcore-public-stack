"""Card previews of Word, spreadsheet, CSV and deck files (apis/shared/files/attachment_preview.py)."""

from __future__ import annotations

import io
import zipfile

import pytest

from apis.shared.files.attachment_preview import (
    PREVIEW_MAX_BYTES,
    PreviewUnavailable,
    build_preview,
    decode_preview,
    encode_preview,
    preview_format,
    preview_key,
)
from tests.shared.test_document_read import build_docx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def build_pptx(slides: list[tuple[str, list[str]]]) -> bytes:
    """A deck holding just slide parts: a title placeholder and a body per slide."""

    def shape(text_rows: list[str], title: bool) -> str:
        ph = '<p:ph type="title"/>' if title else '<p:ph idx="1"/>'
        paras = "".join(f"<a:p><a:r><a:t>{row}</a:t></a:r></a:p>" for row in text_rows)
        return f"<p:sp><p:nvSpPr><p:cNvPr id='1' name='s'/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:txBody>{paras}</p:txBody></p:sp>"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, (title, bullets) in enumerate(slides, start=1):
            body = shape([title], True) + shape(bullets, False)
            archive.writestr(f"ppt/slides/slide{index}.xml", f"<p:sld {_P} {_A}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>")
    return buffer.getvalue()


def build_xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "filename,mime,fmt",
    [
        ("memo.docx", DOCX, "docx"),
        ("budget.xlsx", XLSX, "xlsx"),
        ("grades.csv", "text/csv", "csv"),
        ("talk.pptx", PPTX, "pptx"),
        ("report.pdf", "application/pdf", None),
        ("notes.md", "text/markdown", None),
        ("photo.png", "image/png", None),
    ],
)
def test_preview_format(filename, mime, fmt):
    assert preview_format(filename, mime) == fmt


def test_word_preview_is_its_opening_paragraphs_without_blanks():
    preview = build_preview("docx", build_docx(["Memo to the board", "", "Costs held flat"]))
    assert preview == {"version": 1, "kind": "doc", "lines": ["Memo to the board", "Costs held flat"]}


def test_csv_preview_reads_quoted_cells_and_caps_rows():
    raw = b"Quarter,Revenue\n" + b"".join(f'Q{i},"1,{i}00"\n'.encode() for i in range(1, 20))
    preview = build_preview("csv", raw)
    assert preview["kind"] == "sheet" and preview["sheets"] == []
    assert preview["rows"][0] == ["Quarter", "Revenue"] and preview["rows"][1] == ["Q1", "1,100"]
    assert len(preview["rows"]) == 6


def test_workbook_preview_names_its_sheets_and_reads_the_first():
    raw = build_xlsx({"Budget": [["Line", "FY27"], ["Travel", 1200], ["Equipment", 5400]], "Notes": [["x"]]})
    preview = build_preview("xlsx", raw)
    assert preview["sheets"] == ["Budget", "Notes"]
    assert preview["rows"] == [["Line", "FY27"], ["Travel", "1200"], ["Equipment", "5400"]]


def test_deck_preview_is_the_first_slides_title_and_bullets():
    raw = build_pptx([("Quarterly review", ["Revenue up 12%", "Costs flat"]), ("Second", ["x"])])
    preview = build_preview("pptx", raw)
    assert preview == {"version": 1, "kind": "deck", "slides": 2, "title": "Quarterly review", "bullets": ["Revenue up 12%", "Costs flat"]}


def test_a_long_document_is_trimmed_to_the_byte_cap():
    raw = build_docx(["Lorem ipsum dolor sit amet " * 20] * 30)
    preview = build_preview("docx", raw)
    assert len(encode_preview(preview)) <= PREVIEW_MAX_BYTES
    assert all(len(line) <= 160 for line in preview["lines"])


@pytest.mark.parametrize("fmt", ["docx", "xlsx", "pptx"])
def test_a_file_that_is_not_its_format_has_no_preview(fmt):
    with pytest.raises(PreviewUnavailable):
        build_preview(fmt, b"not a zip")


def test_decode_reads_only_its_own_version():
    assert decode_preview(encode_preview({"version": 1, "kind": "doc", "lines": ["a"]})) == {"version": 1, "kind": "doc", "lines": ["a"]}
    assert decode_preview(b'{"version": 2, "kind": "doc"}') is None
    assert decode_preview(b"not json") is None


def test_preview_sits_in_the_uploads_derived_folder():
    assert preview_key("user-files/u1/s1/up-1/memo.docx") == "user-files/u1/s1/up-1/_derived/preview.json"
