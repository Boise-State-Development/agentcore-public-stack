"""What an attachment card shows of a Word, spreadsheet, CSV or deck file.

The prepare Lambda writes it to ``_derived/preview.json`` when the file is
uploaded, and app-api serves it from ``GET /files/previews`` so the card can
draw the file's real opening lines, first rows or first slide instead of a
skeleton. PDFs have a rendered first page instead (``pdf_thumbnail``), and
text files are read from ``GET /files/{id}/text-snippet`` as before.

Display only: nothing here reaches a model call, the system prompt or the
stored message. Built with the standard library alone (``zipfile`` and
``ElementTree``) because the Lambda image carries no openpyxl or python-pptx,
and capped at ``PREVIEW_MAX_BYTES`` so a batch of twenty stays small.

Shapes (``version`` 1):

* ``{"kind": "doc", "lines": [str, ...]}``: a Word file's opening paragraphs;
* ``{"kind": "sheet", "sheets": [str, ...], "rows": [[str, ...], ...]}``: a
  workbook's sheet names and its first sheet's first rows, or a CSV's rows;
* ``{"kind": "deck", "slides": int, "title": str, "bullets": [str, ...]}``: a
  deck's slide count and its first slide's text.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from apis.shared.files.large_document import derived_prefix
from apis.shared.files.models import is_presentation_file, is_tabular_file

PREVIEW_VERSION = 1
PREVIEW_NAME = "preview.json"
#: The most a stored preview may weigh; past it rows and lines are dropped.
PREVIEW_MAX_BYTES = 4096

MAX_LINES = 8
MAX_ROWS = 6
MAX_COLUMNS = 5
MAX_BULLETS = 5
MAX_SHEETS = 6
LINE_CHARS = 160
CELL_CHARS = 40

#: Bytes of a CSV decoded for its first rows.
CSV_HEAD_BYTES = 16 * 1024
#: Uncompressed bytes read from any one part of an Office zip.
PART_MAX_BYTES = 4 * 1024 * 1024

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_DOCX = {"docx"}
_XLSX = {"xlsx", "xlsm"}
_CSV = {"csv"}
_PPTX = {"pptx"}


class PreviewUnavailable(Exception):
    """The file opened but holds nothing a card can show, or will not parse."""


def preview_key(s3_key: str) -> str:
    """``.../{upload_id}/_derived/preview.json`` for an original at ``s3_key``."""
    return f"{derived_prefix(s3_key)}{PREVIEW_NAME}"


def preview_format(filename: str, mime_type: str) -> Optional[str]:
    """``docx`` / ``xlsx`` / ``csv`` / ``pptx`` when a preview applies, else ``None``."""
    extension = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    mime = (mime_type or "").lower()
    if extension in _DOCX or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if is_presentation_file(filename or "", mime):
        return "pptx"
    if extension in _CSV or mime == "text/csv":
        return "csv"
    if is_tabular_file(filename or "", mime):
        return "xlsx" if extension in _XLSX or "spreadsheetml" in mime else None
    return None


def build_preview(fmt: str, raw: bytes) -> Dict[str, Any]:
    """The preview of ``raw`` as ``fmt``, fitted to ``PREVIEW_MAX_BYTES``.

    Raises ``PreviewUnavailable`` when there is nothing to show or the file
    will not parse.
    """
    try:
        if fmt == "docx":
            body: Dict[str, Any] = {"kind": "doc", "lines": _docx_lines(raw)}
            if not body["lines"]:
                raise PreviewUnavailable("no text")
        elif fmt == "csv":
            body = {"kind": "sheet", "sheets": [], "rows": _csv_rows(raw)}
        elif fmt == "xlsx":
            body = _xlsx_preview(raw)
        elif fmt == "pptx":
            body = _pptx_preview(raw)
        else:
            raise PreviewUnavailable(f"no preview for {fmt}")
    except PreviewUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - a malformed file is a missing preview
        raise PreviewUnavailable(type(e).__name__) from e
    return _fit({"version": PREVIEW_VERSION, **body})


def encode_preview(preview: Dict[str, Any]) -> bytes:
    return json.dumps(preview, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_preview(raw: bytes) -> Optional[Dict[str, Any]]:
    """A stored preview, or ``None`` when it is not one this version reads."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != PREVIEW_VERSION:
        return None
    if value.get("kind") not in ("doc", "sheet", "deck"):
        return None
    return value


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _part(archive: zipfile.ZipFile, name: str) -> Optional[ElementTree.Element]:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > PART_MAX_BYTES:
        raise PreviewUnavailable("part too large")
    return ElementTree.fromstring(archive.read(info))


def _docx_lines(raw: bytes) -> List[str]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        root = _part(archive, "word/document.xml")
    if root is None:
        raise PreviewUnavailable("no document part")
    lines: List[str] = []
    for paragraph in root.iter(f"{{{_NS['w']}}}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{{{_NS['w']}}}t"))
        if text.strip():
            lines.append(_clip(text, LINE_CHARS))
        if len(lines) >= MAX_LINES:
            break
    return lines


def _csv_rows(raw: bytes) -> List[List[str]]:
    text = raw[:CSV_HEAD_BYTES].decode("utf-8-sig", errors="replace")
    rows: List[List[str]] = []
    for row in csv.reader(io.StringIO(text)):
        if any(cell.strip() for cell in row):
            rows.append([_clip(cell, CELL_CHARS) for cell in row[:MAX_COLUMNS]])
        if len(rows) >= MAX_ROWS:
            break
    if not rows:
        raise PreviewUnavailable("no rows")
    return rows


def _xlsx_preview(raw: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        workbook = _part(archive, "xl/workbook.xml")
        if workbook is None:
            raise PreviewUnavailable("no workbook part")
        sheets = workbook.findall("s:sheets/s:sheet", _NS)
        names = [_clip(sheet.get("name") or "", CELL_CHARS) for sheet in sheets][:MAX_SHEETS]
        if not sheets:
            raise PreviewUnavailable("no sheets")
        target = _first_sheet_part(archive, sheets[0].get(f"{{{_NS['r']}}}id"))
        shared = _shared_strings(archive)
        sheet = _part(archive, target) if target else None
    rows: List[List[str]] = []
    if sheet is not None:
        for row in sheet.iterfind("s:sheetData/s:row", _NS):
            cells: Dict[int, str] = {}
            for cell in row.iterfind("s:c", _NS):
                column = _column_index(cell.get("r") or "")
                if column is None or column >= MAX_COLUMNS:
                    continue
                cells[column] = _clip(_cell_text(cell, shared), CELL_CHARS)
            if any(value for value in cells.values()):
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
            if len(rows) >= MAX_ROWS:
                break
    return {"kind": "sheet", "sheets": names, "rows": rows}


def _first_sheet_part(archive: zipfile.ZipFile, rel_id: Optional[str]) -> Optional[str]:
    rels = _part(archive, "xl/_rels/workbook.xml.rels")
    if rels is None or not rel_id:
        return "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in archive.namelist() else None
    for rel in rels.iterfind("rel:Relationship", _NS):
        if rel.get("Id") == rel_id:
            target = (rel.get("Target") or "").lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    return None


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    root = _part(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    return ["".join(node.text or "" for node in item.iter(f"{{{_NS['s']}}}t")) for item in root.iterfind("s:si", _NS)]


def _cell_text(cell: ElementTree.Element, shared: List[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{_NS['s']}}}t"))
    value = cell.findtext("s:v", default="", namespaces=_NS)
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def _column_index(reference: str) -> Optional[int]:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        return None
    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _pptx_preview(raw: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        slides = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.findall(r"\d+", name)[-1]),
        )
        first = _part(archive, slides[0]) if slides else None
    if first is None:
        raise PreviewUnavailable("no slides")
    title = ""
    bullets: List[str] = []
    for shape in first.iter(f"{{{_NS['p']}}}sp"):
        placeholder = shape.find(".//p:nvPr/p:ph", _NS)
        is_title = placeholder is not None and placeholder.get("type") in ("title", "ctrTitle")
        for paragraph in shape.iter(f"{{{_NS['a']}}}p"):
            text = "".join(node.text or "" for node in paragraph.iter(f"{{{_NS['a']}}}t")).strip()
            if not text:
                continue
            if is_title and not title:
                title = _clip(text, LINE_CHARS)
            elif len(bullets) < MAX_BULLETS:
                bullets.append(_clip(text, LINE_CHARS))
    if not title and bullets:
        title = bullets.pop(0)
    return {"kind": "deck", "slides": len(slides), "title": title, "bullets": bullets}


def _fit(preview: Dict[str, Any]) -> Dict[str, Any]:
    """Drop trailing lines, rows or bullets until the preview fits."""
    for field in ("lines", "rows", "bullets", "sheets"):
        while len(encode_preview(preview)) > PREVIEW_MAX_BYTES and preview.get(field):
            preview[field] = preview[field][:-1]
    if len(encode_preview(preview)) > PREVIEW_MAX_BYTES:
        raise PreviewUnavailable("too large")
    return preview
