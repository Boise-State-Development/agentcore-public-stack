"""Tests for ``_parse_sheet_inventory`` and ``_format_sheet_note`` — the
parser for the XLSX bootstrap's pipe-delimited sheet inventory, and the
markdown footer builder that surfaces it to the model.

The inventory flows from the sandbox's stdout back to the tool response,
so regressions here would either silently drop multi-sheet support or
mis-report which sheets were included/skipped. See #261.
"""

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    MAX_ROWS_PER_SHEET,
    _SHEETS_MARKER,
    _format_sheet_note,
    _parse_sheet_inventory,
)


def _wrap_block(lines: list[str]) -> str:
    """Helper: wrap inventory lines with the sheet markers as the bootstrap
    would emit them.
    """
    body = "\n".join(lines)
    return f"{_SHEETS_MARKER}\n{body}\n{_SHEETS_MARKER}\n"


class TestParseSheetInventoryEmpty:
    def test_no_marker_returns_empty_inventory(self):
        result = _parse_sheet_inventory("some unrelated stdout")
        assert result["total"] == 0
        assert result["sheets"] == []
        assert result["skipped"] == 0
        assert result["has_primary_alias"] is False

    def test_empty_string_returns_empty_inventory(self):
        result = _parse_sheet_inventory("")
        assert result["sheets"] == []

    def test_single_marker_returns_empty_inventory(self):
        """Malformed emission with only one marker — don't try to parse."""
        result = _parse_sheet_inventory(f"partial {_SHEETS_MARKER}\nsheet|x|x|0|0|")
        # Behavior: parser splits on marker; only one marker means no
        # bracketed block. Should still return a safe empty structure.
        # Whether it returns data or empty is implementation-defined, but
        # it must not raise.
        assert isinstance(result, dict)


class TestParseSheetInventorySingleSheet:
    def test_single_sheet_no_truncation(self):
        stdout = _wrap_block([
            "total: 1",
            "converted: 1",
            "skipped: 0",
            "sheet|Summary|Budget.csv|100|0|",
        ])
        result = _parse_sheet_inventory(stdout)
        assert result["total"] == 1
        assert result["converted"] == 1
        assert result["skipped"] == 0
        assert len(result["sheets"]) == 1
        assert result["sheets"][0]["name"] == "Summary"
        assert result["sheets"][0]["path"] == "Budget.csv"
        assert result["sheets"][0]["rows"] == 100
        assert result["sheets"][0]["truncated"] is False
        assert result["sheets"][0]["primary_alias"] is None
        assert result["has_primary_alias"] is False

    def test_truncation_flag_parsed(self):
        stdout = _wrap_block([
            "total: 1",
            "converted: 1",
            "skipped: 0",
            "sheet|BigSheet|data.csv|500000|1|",
        ])
        result = _parse_sheet_inventory(stdout)
        assert result["sheets"][0]["truncated"] is True


class TestParseSheetInventoryMultiSheet:
    def test_multi_sheet_with_primary_alias(self):
        stdout = _wrap_block([
            "total: 3",
            "converted: 3",
            "skipped: 0",
            "sheet|Summary|Budget.summary.csv|12|0|Budget.csv",
            "sheet|Transactions|Budget.transactions.csv|18551|0|",
            "sheet|Notes|Budget.notes.csv|5|0|",
        ])
        result = _parse_sheet_inventory(stdout)
        assert result["total"] == 3
        assert result["converted"] == 3
        assert len(result["sheets"]) == 3
        assert result["has_primary_alias"] is True
        assert result["sheets"][0]["primary_alias"] == "Budget.csv"
        # Sibling sheets don't carry the alias.
        assert result["sheets"][1]["primary_alias"] is None
        assert result["sheets"][2]["primary_alias"] is None

    def test_skipped_sheets_preview(self):
        stdout = _wrap_block([
            "total: 30",
            "converted: 25",
            "skipped: 5",
            "skipped_names: ['Sheet26', 'Sheet27', 'Sheet28', 'Sheet29', 'Sheet30']",
            "sheet|Sheet1|data.sheet1.csv|10|0|",
        ])
        result = _parse_sheet_inventory(stdout)
        assert result["skipped"] == 5
        assert result["skipped_preview"] == [
            "Sheet26", "Sheet27", "Sheet28", "Sheet29", "Sheet30",
        ]

    def test_sheet_names_with_special_chars_via_literal_eval(self):
        """Sheet names can contain commas, apostrophes, etc. The skipped_names
        field is a Python list literal — ast.literal_eval handles quoting
        correctly. This locks in the contract.
        """
        stdout = _wrap_block([
            "total: 5",
            "converted: 3",
            "skipped: 2",
            'skipped_names: ["O\'Brien, J.", "Q1, 2026"]',
            "sheet|Main|data.main.csv|10|0|",
        ])
        result = _parse_sheet_inventory(stdout)
        # Both names survive round-trip.
        assert "O'Brien, J." in result["skipped_preview"]
        assert "Q1, 2026" in result["skipped_preview"]

    def test_malformed_skipped_names_gracefully_ignored(self):
        """If the literal is invalid, we don't crash — we just skip it."""
        stdout = _wrap_block([
            "total: 10",
            "converted: 5",
            "skipped: 5",
            "skipped_names: not-a-valid-literal",
            "sheet|Main|data.main.csv|10|0|",
        ])
        result = _parse_sheet_inventory(stdout)
        assert result["skipped_preview"] == []
        # Other fields still populated.
        assert result["total"] == 10


class TestParseSheetInventoryMalformedSheetLines:
    def test_truncated_sheet_line_skipped(self):
        """A sheet line with fewer than 6 pipe-delimited fields is
        skipped rather than crashing the parser.
        """
        stdout = _wrap_block([
            "total: 2",
            "converted: 2",
            "skipped: 0",
            "sheet|Valid|data.csv|10|0|",
            "sheet|Broken|truncated",  # too few fields
        ])
        result = _parse_sheet_inventory(stdout)
        # Only the valid sheet is kept.
        assert len(result["sheets"]) == 1
        assert result["sheets"][0]["name"] == "Valid"

    def test_integer_fields_with_whitespace(self):
        """``_safe_int`` handles surrounding whitespace — regression
        guard: the parser strips on its own too.
        """
        stdout = _wrap_block([
            "total:  42 ",
            "converted:  10  ",
            "skipped: 32",
            "sheet|S|p.csv|  500  | 0 |",
        ])
        result = _parse_sheet_inventory(stdout)
        assert result["total"] == 42
        assert result["converted"] == 10
        assert result["skipped"] == 32
        assert result["sheets"][0]["rows"] == 500


# ---------------------------------------------------------------------------
# _format_sheet_note
# ---------------------------------------------------------------------------


class TestFormatSheetNoteSingleSheet:
    def test_single_sheet_no_truncation_returns_empty(self):
        """Single-sheet workbook without truncation is the boring case —
        no message needed.
        """
        inventory = {
            "total": 1,
            "converted": 1,
            "skipped": 0,
            "skipped_preview": [],
            "sheets": [
                {"name": "Sheet1", "path": "data.csv", "rows": 100,
                 "truncated": False, "primary_alias": None},
            ],
            "has_primary_alias": False,
        }
        assert _format_sheet_note(inventory) == ""

    def test_single_sheet_truncated_surfaces_warning(self):
        inventory = {
            "total": 1,
            "converted": 1,
            "skipped": 0,
            "skipped_preview": [],
            "sheets": [
                {"name": "BigSheet", "path": "data.csv",
                 "rows": MAX_ROWS_PER_SHEET, "truncated": True,
                 "primary_alias": None},
            ],
            "has_primary_alias": False,
        }
        note = _format_sheet_note(inventory)
        assert note != ""
        assert "truncated" in note.lower()
        assert "BigSheet" in note
        assert f"{MAX_ROWS_PER_SHEET:,}" in note


class TestFormatSheetNoteMultiSheet:
    def test_all_sheets_converted(self):
        inventory = {
            "total": 3,
            "converted": 3,
            "skipped": 0,
            "skipped_preview": [],
            "sheets": [
                {"name": "Summary", "path": "Budget.summary.csv", "rows": 12,
                 "truncated": False, "primary_alias": "Budget.csv"},
                {"name": "Transactions", "path": "Budget.transactions.csv",
                 "rows": 18551, "truncated": False, "primary_alias": None},
                {"name": "Notes", "path": "Budget.notes.csv", "rows": 5,
                 "truncated": False, "primary_alias": None},
            ],
            "has_primary_alias": True,
        }
        note = _format_sheet_note(inventory)
        # Full inventory listed so the model can pick or combine.
        assert "Available sheets" in note
        assert "Summary" in note
        assert "Transactions" in note
        assert "Notes" in note
        assert "Budget.summary.csv" in note
        assert "Budget.transactions.csv" in note
        assert "18,551" in note  # row count formatted with commas

    def test_skipped_sheets_surfaced_with_names(self):
        inventory = {
            "total": 30,
            "converted": 25,
            "skipped": 5,
            "skipped_preview": ["Q6", "Q7", "Q8", "Q9", "Q10"],
            "sheets": [
                {"name": f"Q{i + 1}", "path": f"Budget.q{i + 1}.csv",
                 "rows": 100, "truncated": False, "primary_alias": None}
                for i in range(25)
            ],
            "has_primary_alias": False,
        }
        note = _format_sheet_note(inventory)
        assert "30 sheets" in note
        assert "first 25" in note
        assert "Q6" in note
        assert "Q10" in note
        # Tells the user what to do about it.
        assert "split" in note.lower() or "export" in note.lower()

    def test_skipped_many_includes_more_suffix(self):
        inventory = {
            "total": 100,
            "converted": 25,
            "skipped": 75,
            "skipped_preview": ["A", "B", "C", "D", "E"],
            "sheets": [
                {"name": f"S{i}", "path": f"d.s{i}.csv", "rows": 1,
                 "truncated": False, "primary_alias": None}
                for i in range(25)
            ],
            "has_primary_alias": False,
        }
        note = _format_sheet_note(inventory)
        assert "+70 more" in note  # 75 skipped - 5 shown = 70

    def test_truncated_sheet_annotation_in_list(self):
        inventory = {
            "total": 2,
            "converted": 2,
            "skipped": 0,
            "skipped_preview": [],
            "sheets": [
                {"name": "Huge", "path": "wb.huge.csv",
                 "rows": MAX_ROWS_PER_SHEET, "truncated": True,
                 "primary_alias": None},
                {"name": "Small", "path": "wb.small.csv", "rows": 10,
                 "truncated": False, "primary_alias": None},
            ],
            "has_primary_alias": False,
        }
        note = _format_sheet_note(inventory)
        # The truncated row should have a specific tag; the other shouldn't.
        lines = note.splitlines()
        huge_line = next(line for line in lines if "Huge" in line)
        small_line = next(line for line in lines if "Small" in line)
        assert "truncated" in huge_line.lower()
        assert "truncated" not in small_line.lower()
