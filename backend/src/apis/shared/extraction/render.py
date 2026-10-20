"""The reviewable record: a markdown table the backend writes, and an .xlsx.

``docs/specs/structured-extraction.md`` §8, D13. Both are authored here, by the
backend, from data it already holds — the row values never pass through the
model to produce either one. That is the whole reason this uses
``create_artifact_record`` rather than the ``create_artifact`` tool, and
``openpyxl`` in process rather than the existing ``create_excel_spreadsheet``,
which drives openpyxl *inside* Code Interpreter from code the model writes.

HOW THE STATES ARE WORDED
-------------------------
The header states the claim exactly once, in the source module's own terms:
*this value is in this passage, on this page*. Nothing stronger. The value
check asks whether the value appears somewhere in the cited passage, not
whether the passage assigns it to this field, and a table of badges invites
over-reading precisely because its structure implies the assignment was
checked. So no column header reads "verified", and the one qualifier a reader
needs is spelled out rather than encoded in a tick.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Dict, Optional, Sequence

from apis.shared.files.source_provenance import (
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNASSESSED,
    STATE_UNVERIFIED,
)

from .field_sets import FieldSet
from .rows import Row

logger = logging.getLogger(__name__)

XLSX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

#: How each state reads to a human. Keyed off the exported constants, never off
#: a string written here, so a renamed state is a broken import rather than a
#: silently unlabelled row.
_STATE_LABEL: Dict[str, str] = {
    STATE_SUPPORTED: "In the cited passage",
    STATE_QUOTE_UNSUPPORTED: "⚠ NOT in the cited passage",
    STATE_UNASSESSED: "Passage found; value not confirmed",
    STATE_UNVERIFIED: "No source passage found",
}

_STATE_NOTE = (
    "**What the status column means.** Each value was checked against the "
    "document in two steps: is the quoted passage really in the document, and "
    "does the value itself appear in that passage. So *In the cited passage* "
    "means exactly that — this value is in this passage, at this position. It "
    "does not mean the passage assigns the value to this field, and it is not "
    "a judgement that the value is correct. *NOT in the cited passage* is the "
    "one to read first: the passage is real and the value is not in it."
)


def _label(state: str) -> str:
    return _STATE_LABEL.get(state, state)


def _cell(text: str) -> str:
    """One value, safe to drop in a markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _display(row: Row) -> str:
    if row.value is None:
        return "—"
    if isinstance(row.value, bool):
        return "Yes" if row.value else "No"
    if isinstance(row.value, list):
        return ", ".join(str(item) for item in row.value if item is not None) or "—"
    return str(row.value)


def render_markdown(
    rows: Sequence[Row],
    *,
    field_set: FieldSet,
    filename: str,
    model_id: str,
    schema_enforced: bool,
) -> str:
    """The artifact's content: a header, the rows, and what to make of them."""
    lines = [
        f"# {field_set.title}",
        "",
        f"**Document:** {filename}  ",
        f"**Field set:** `{field_set.name}` (version {field_set.version})  ",
        f"**Extraction model:** `{model_id}`",
    ]
    if not schema_enforced:
        # D11: a reviewer must be able to tell types that were guaranteed from
        # types that were merely asked for.
        lines.append(
            "  \n**⚠ Types were requested, not enforced.** The constrained "
            "schema could not be applied to this run, so the values were "
            "validated after the fact rather than guaranteed as they were "
            "produced. Treat the types in this table as the model's, not the "
            "schema's."
        )
    lines += [
        "",
        "| Field | Value | Source | Status |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        locator = row.locator_text or "—"
        lines.append(
            f"| {_cell(row.field.title)} | {_cell(_display(row))} | "
            f"{_cell(locator)} | {_cell(_label(row.state))} |"
        )

    unsupported = [row for row in rows if row.state == STATE_QUOTE_UNSUPPORTED]
    lines += ["", _STATE_NOTE]
    if unsupported:
        names = ", ".join(row.field.title for row in unsupported)
        lines += [
            "",
            f"**Check these first:** {names} — the passage cited for each is in "
            "the document, but the value is not in that passage.",
        ]
    return "\n".join(lines) + "\n"


def render_workbook(
    rows: Sequence[Row],
    *,
    field_set: FieldSet,
    filename: str,
    model_id: str,
    schema_enforced: bool,
) -> Optional[bytes]:
    """The .xlsx bytes, or ``None`` when openpyxl is unavailable.

    ``openpyxl`` is a base dependency, in every image, so the import is
    expected to succeed — but a missing workbook is a degraded result and a
    raised exception is a lost extraction, so this returns ``None`` and the
    caller carries on with the artifact and the table.

    One sheet, one row per field, with the provenance beside each value rather
    than on a second sheet: a reviewer scanning for the ``NOT in the cited
    passage`` rows should not have to cross-reference anything.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
    except ImportError:  # pragma: no cover - openpyxl is a base dependency
        logger.warning("document_extract: openpyxl unavailable; no workbook written")
        return None

    try:
        book = Workbook()
        sheet = book.active
        sheet.title = (field_set.name or "extraction")[:31]

        sheet.append([f"{field_set.title} — {filename}"])
        sheet["A1"].font = Font(bold=True, size=14)
        sheet.append(
            [
                f"Field set {field_set.name} v{field_set.version}; "
                f"extracted with {model_id}"
                + ("" if schema_enforced else "; TYPES REQUESTED, NOT ENFORCED")
            ]
        )
        sheet.append([])

        headers = ["Field", "Value", "Source", "Status", "Cited passage"]
        sheet.append(headers)
        for index in range(1, len(headers) + 1):
            sheet.cell(row=4, column=index).font = Font(bold=True)

        for row in rows:
            sheet.append(
                [
                    row.field.title,
                    _display(row) if row.value is not None else "",
                    row.locator_text or "",
                    _label(row.state),
                    row.quote or "",
                ]
            )

        for column, width in zip("ABCDE", (28, 36, 12, 34, 80)):
            sheet.column_dimensions[column].width = width
        for cell in sheet["E"]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

        buffer = BytesIO()
        book.save(buffer)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a missing workbook beats a lost run
        logger.warning("document_extract: failed to build workbook", exc_info=True)
        return None


def workbook_filename(field_set: FieldSet, filename: str) -> str:
    """A readable, filesystem-safe name for the workbook."""
    stem = (filename or "document").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    safe = "".join(
        char if (char.isalnum() or char in "-_") else "-" for char in stem
    ).strip("-")[:80]
    return f"{safe or 'document'}-{field_set.name}.xlsx"


__all__ = [
    "XLSX_MIME_TYPE",
    "render_markdown",
    "render_workbook",
    "workbook_filename",
]
