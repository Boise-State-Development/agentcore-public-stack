"""Tests for the .xlsx reader behind GET /files/{id}/sheet-preview."""

from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import Workbook

from apis.app_api.files.sheet_preview import (
    MAX_COLUMNS_PER_SHEET,
    MAX_ROWS_PER_SHEET,
    MAX_SHEETS,
    MAX_WORKBOOK_BYTES,
    WorkbookTooLargeError,
    WorkbookUnreadableError,
    read_workbook_preview,
)


def build(*, sheets: dict[str, list[list]] | None = None) -> bytes:
    """Serialise a workbook the way openpyxl itself would write one."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in (sheets or {"Sheet1": [["a"], [1]]}).items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class TestBasicReading:
    def test_reads_headers_and_rows(self):
        data = build(sheets={"Budget": [["Item", "Cost"], ["Rent", 1200], ["Food", 300]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.name == "Budget"
        assert sheet.headers == ["Item", "Cost"]
        assert sheet.rows == [["Rent", "1200"], ["Food", "300"]]
        assert sheet.truncated is False

    def test_reads_every_visible_sheet_in_order(self):
        data = build(
            sheets={
                "First": [["a"], ["1"]],
                "Second": [["b"], ["2"]],
            }
        )

        sheets = read_workbook_preview(data)

        assert [s.name for s in sheets] == ["First", "Second"]

    def test_skips_hidden_sheets(self):
        # Excel does not show them either; surfacing a scratch sheet would
        # misrepresent the workbook.
        wb = Workbook()
        wb.remove(wb.active)
        visible = wb.create_sheet(title="Visible")
        visible.append(["a"])
        visible.append([1])
        hidden = wb.create_sheet(title="Scratch")
        hidden.append(["secret"])
        hidden.sheet_state = "hidden"
        buffer = BytesIO()
        wb.save(buffer)

        sheets = read_workbook_preview(buffer.getvalue())

        assert [s.name for s in sheets] == ["Visible"]

    def test_substitutes_a_column_letter_for_a_blank_header(self):
        data = build(sheets={"S": [[None, "name"], [1, "widget"]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.headers == ["A", "name"]

    def test_pads_a_short_row_to_the_grid_width(self):
        data = build(sheets={"S": [["a", "b", "c"], [1, 2]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [["1", "2", ""]]

    def test_reports_an_empty_sheet_without_failing(self):
        data = build(sheets={"Empty": []})

        [sheet] = read_workbook_preview(data)

        assert sheet.headers == []
        assert sheet.rows == []


class TestFormulas:
    """The trap that motivated reading the workbook twice."""

    def test_shows_formula_text_when_no_value_was_cached(self):
        # openpyxl performs no evaluation, so every formula in a workbook
        # it wrote reads back as None under data_only=True. A values-only
        # preview would show a blank exactly where the total belongs.
        wb = Workbook()
        ws = wb.active
        ws.append(["Item", "Cost"])
        ws.append(["Rent", 1200])
        ws.append(["Total", "=SUM(B2:B2)"])
        buffer = BytesIO()
        wb.save(buffer)

        [sheet] = read_workbook_preview(buffer.getvalue())

        assert sheet.rows[1] == ["Total", "=SUM(B2:B2)"]

    def test_prefers_a_cached_value_over_the_formula(self):
        # A workbook saved by Excel carries the computed value, which is
        # what the user should see.
        wb = Workbook()
        wb.active.append(["total"])
        wb.active.append(["=SUM(A1:A1)"])
        buffer = BytesIO()
        wb.save(buffer)
        without_cache = read_workbook_preview(buffer.getvalue())[0]
        assert without_cache.rows == [["=SUM(A1:A1)"]]

        # Simulate the cached-value case by writing the literal instead.
        cached = read_workbook_preview(build(sheets={"Sheet": [["total"], [42]]}))[0]
        assert cached.rows == [["42"]]


class TestCellFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, ""),
            (True, "TRUE"),
            (False, "FALSE"),
            (7, "7"),
            (-3, "-3"),
            ("plain", "plain"),
        ],
    )
    def test_scalar_values(self, value, expected):
        data = build(sheets={"S": [["h"], [value]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [[expected]]

    def test_a_whole_float_loses_its_trailing_zero(self):
        data = build(sheets={"S": [["h"], [3.0]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [["3"]]

    def test_float_noise_is_not_shown(self):
        # 0.1 + 0.2 must not preview as 0.30000000000000004.
        data = build(sheets={"S": [["h"], [0.1 + 0.2]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [["0.3"]]

    def test_a_date_does_not_gain_a_midnight_it_never_had(self):
        # openpyxl returns a datetime for every date-formatted cell, so
        # rendering it whole would invent precision the file lacks.
        data = build(sheets={"S": [["when"], [datetime(2026, 3, 15)]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [["2026-03-15"]]

    def test_a_real_timestamp_keeps_its_time(self):
        data = build(sheets={"S": [["when"], [datetime(2026, 3, 15, 9, 30)]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [["2026-03-15 09:30:00"]]

    def test_a_numeric_string_stays_a_string(self):
        # Same contract as the csv grid: the preview must not reinterpret
        # what the file will hand downstream.
        data = build(sheets={"S": [["zip"], ["007"]]})

        [sheet] = read_workbook_preview(data)

        assert sheet.rows == [["007"]]


class TestCaps:
    def test_stops_at_the_row_cap_and_reports_the_real_total(self):
        rows = [["n"]] + [[i] for i in range(MAX_ROWS_PER_SHEET + 50)]
        data = build(sheets={"S": rows})

        [sheet] = read_workbook_preview(data)

        assert len(sheet.rows) == MAX_ROWS_PER_SHEET
        assert sheet.truncated is True
        assert sheet.truncated_by == "rows"
        # The denominator is what the file claims, not what we read.
        assert sheet.total_rows == MAX_ROWS_PER_SHEET + 50

    def test_stops_at_the_column_cap(self):
        wide = list(range(MAX_COLUMNS_PER_SHEET + 10))
        data = build(sheets={"S": [wide, wide]})

        [sheet] = read_workbook_preview(data)

        assert len(sheet.headers) == MAX_COLUMNS_PER_SHEET
        assert len(sheet.rows[0]) == MAX_COLUMNS_PER_SHEET
        assert sheet.truncated_by == "columns"

    def test_stops_at_the_sheet_cap(self):
        data = build(
            sheets={f"S{i}": [["a"], [i]] for i in range(MAX_SHEETS + 5)}
        )

        sheets = read_workbook_preview(data)

        assert len(sheets) <= MAX_SHEETS

    def test_drops_trailing_empty_columns(self):
        # A stray format on an empty cell is enough to stretch a sheet's
        # used range, and the grid should not open on a screen of blanks.
        wb = Workbook()
        ws = wb.active
        ws.append(["a", "b"])
        ws.append([1, 2])
        ws["H5"] = None
        buffer = BytesIO()
        wb.save(buffer)

        [sheet] = read_workbook_preview(buffer.getvalue())

        assert sheet.headers == ["a", "b"]

    def test_refuses_a_workbook_past_the_byte_cap(self):
        with pytest.raises(WorkbookTooLargeError):
            read_workbook_preview(b"x" * (MAX_WORKBOOK_BYTES + 1))


class TestFailures:
    def test_rejects_bytes_that_are_not_a_workbook(self):
        with pytest.raises(WorkbookUnreadableError):
            read_workbook_preview(b"this is not a zip file")

    def test_rejects_an_empty_body(self):
        with pytest.raises(WorkbookUnreadableError):
            read_workbook_preview(b"")
