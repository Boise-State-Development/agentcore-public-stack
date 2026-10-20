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
from typing import Any, Dict, Optional, Sequence, Tuple

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


def _format_number(value: float) -> str:
    """A number as a reader expects it: ``15`` rather than ``15.0``.

    JSON has one number type, so a `number` field whose document says "15
    pages" comes back as ``15.0`` and renders that way in a column headed
    "Page limit" — which reads as a bug to the office looking at it. Only
    whole values are shortened; ``54.5`` stays ``54.5``.
    """
    return str(int(value)) if float(value).is_integer() else str(value)


def _cell(text: str) -> str:
    """One value, safe to drop in a markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _display(row: Row) -> str:
    if row.value is None:
        return "—"
    if isinstance(row.value, bool):
        return "Yes" if row.value else "No"
    if isinstance(row.value, float):
        return _format_number(row.value)
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
    "batch_workbook_filename",
    "render_batch_markdown",
    "render_batch_workbook",
    "render_markdown",
    "render_workbook",
    "workbook_filename",
]


# ---------------------------------------------------------------------------
# A batch: one artifact and one workbook holding the whole set
# ---------------------------------------------------------------------------
#
# Not N artifacts and N workbooks (§17.2). The deliverable of an N-document run
# is the **comparison**: "two collide on the same March deadline and both need
# the same PI" is a fact about the set, not about any document in it, and it is
# only visible in a table that holds the set. A reviewer handed fourteen
# separate tables has been handed the work of joining them.


def render_batch_markdown(
    per_document: Sequence[Tuple[Any, Sequence[Row]]],
    *,
    field_set: FieldSet,
    model_id: str,
    schema_enforced: bool,
    failures: Sequence[Tuple[str, str]] = (),
) -> str:
    """The batch artifact: one comparison table, then one section per document.

    The comparison table comes **first** and is oriented field-per-row,
    document-per-column, because that is the orientation in which a collision
    is visible at a glance — the same deadline twice on one row. The per-document
    sections below it carry the provenance, which is what a reviewer goes to
    once the comparison has told them where to look.
    """
    documents = [outcome for outcome, _ in per_document]
    lines = [
        f"# {field_set.title}",
        "",
        f"**Documents:** {len(documents)}  ",
        f"**Field set:** `{field_set.name}` (version {field_set.version})  ",
        f"**Extraction model:** `{model_id}`",
    ]
    if not schema_enforced:
        lines.append(
            "  \n**⚠ Types were requested, not enforced** for at least one "
            "document. The constrained schema could not be applied, so those "
            "values were validated after the fact rather than guaranteed as "
            "they were produced."
        )
    if failures:
        lines += [
            "",
            f"**⚠ {len(failures)} document(s) could not be extracted** and are "
            "absent from the comparison below:",
            "",
        ]
        lines += [f"- {_cell(name)} — {_cell(reason)}" for name, reason in failures]

    if len(per_document) > 1:
        lines += ["", "## Comparison", ""]
        header = "| Field | " + " | ".join(
            _cell(o.filename or o.upload_id) for o in documents
        ) + " |"
        lines.append(header)
        lines.append("| --- | " + " | ".join("---" for _ in documents) + " |")
        for index, field in enumerate(field_set.fields):
            cells = []
            for _, rows in per_document:
                row = rows[index] if index < len(rows) else None
                if row is None:
                    cells.append("—")
                    continue
                text = _cell(_display(row))
                # A value the check did not support must not sit in a
                # comparison looking like one that was. The marker is the
                # cheapest honest way to carry that into a dense table.
                if row.state == STATE_QUOTE_UNSUPPORTED:
                    text = f"⚠ {text}"
                elif row.state == STATE_UNVERIFIED and row.value is not None:
                    text = f"? {text}"
                cells.append(text)
            lines.append(f"| {_cell(field.title)} | " + " | ".join(cells) + " |")
        lines += [
            "",
            "⚠ = the value is **not** in the passage cited for it. "
            "? = no source passage was found for it. "
            "Neither is safe to rely on without checking the document.",
        ]

    for outcome, rows in per_document:
        lines += [
            "",
            f"## {outcome.filename or outcome.upload_id}",
            "",
            "| Field | Value | Source | Status |",
            "| --- | --- | --- | --- |",
        ]
        for row in rows:
            lines.append(
                f"| {_cell(row.field.title)} | {_cell(_display(row))} | "
                f"{_cell(row.locator_text or '—')} | {_cell(_label(row.state))} |"
            )

    lines += ["", _STATE_NOTE]
    return "\n".join(lines) + "\n"


def render_batch_workbook(
    per_document: Sequence[Tuple[Any, Sequence[Row]]],
    *,
    field_set: FieldSet,
    model_id: str,
    schema_enforced: bool,
) -> Optional[bytes]:
    """One workbook for the batch: a Comparison sheet, then one row per cell.

    Two sheets rather than one, because the two questions an office asks of a
    batch want opposite orientations: *Comparison* is field-per-row and
    document-per-column for spotting collisions, while *Detail* is one row per
    (document, field) with its passage, which is what sorts and filters.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
    except ImportError:  # pragma: no cover - openpyxl is a base dependency
        logger.warning("document_extract: openpyxl unavailable; no workbook written")
        return None

    try:
        documents = [outcome for outcome, _ in per_document]
        book = Workbook()

        comparison = book.active
        comparison.title = "Comparison"
        comparison.append(
            [f"{field_set.title} — {len(documents)} document(s)"]
        )
        comparison["A1"].font = Font(bold=True, size=14)
        comparison.append(
            [
                f"Field set {field_set.name} v{field_set.version}; "
                f"extracted with {model_id}"
                + ("" if schema_enforced else "; TYPES REQUESTED, NOT ENFORCED")
            ]
        )
        comparison.append([])
        header = ["Field"] + [o.filename or o.upload_id for o in documents]
        comparison.append(header)
        for index in range(1, len(header) + 1):
            comparison.cell(row=4, column=index).font = Font(bold=True)
        for index, field in enumerate(field_set.fields):
            cells = [field.title]
            for _, rows in per_document:
                row = rows[index] if index < len(rows) else None
                cells.append("" if row is None else _display(row))
            comparison.append(cells)
        comparison.column_dimensions["A"].width = 28
        for column in range(2, len(header) + 1):
            comparison.cell(row=4, column=column)
            comparison.column_dimensions[
                comparison.cell(row=4, column=column).column_letter
            ].width = 30

        detail = book.create_sheet("Detail")
        detail_header = ["Document", "Field", "Value", "Source", "Status", "Cited passage"]
        detail.append(detail_header)
        for index in range(1, len(detail_header) + 1):
            detail.cell(row=1, column=index).font = Font(bold=True)
        for outcome, rows in per_document:
            for row in rows:
                detail.append(
                    [
                        outcome.filename or outcome.upload_id,
                        row.field.title,
                        _display(row) if row.value is not None else "",
                        row.locator_text or "",
                        _label(row.state),
                        row.quote or "",
                    ]
                )
        for column, width in zip("ABCDEF", (32, 28, 36, 12, 34, 80)):
            detail.column_dimensions[column].width = width
        for cell in detail["F"]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

        buffer = BytesIO()
        book.save(buffer)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a missing workbook beats a lost run
        logger.warning("document_extract: failed to build batch workbook", exc_info=True)
        return None


def batch_workbook_filename(field_set: FieldSet, count: int) -> str:
    """A readable name for a batch workbook."""
    if count == 1:
        return f"{field_set.name}.xlsx"
    return f"{field_set.name}-{count}-documents.xlsx"
