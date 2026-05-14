"""Tests for ``_strip_first_row`` — drops the ``first_row:`` line from a
schema footer on the error path to keep retry responses token-efficient.

Simple helper but load-bearing: every analyze_spreadsheet error retry goes
through it, and a bug here silently bloats every follow-up turn by ~1K
tokens (#261).
"""

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import _strip_first_row


class TestStripFirstRow:
    def test_drops_first_row_line(self):
        schema = (
            "file: data.csv (100 rows x 5 cols)\n"
            "load: pd.read_csv('data.csv', low_memory=False)\n"
            "columns: a, b, c, d, e\n"
            "first_row: {'a': 1, 'b': 2, 'c': 3, 'd': 4, 'e': 5}\n"
        )
        result = _strip_first_row(schema)
        assert "first_row:" not in result
        assert "file: data.csv" in result
        assert "load:" in result
        assert "columns:" in result

    def test_no_first_row_line_unchanged(self):
        """If the schema footer doesn't have a first_row line (malformed or
        schema-preview-failed path), return it as-is. Don't lose structure
        trying to remove something that isn't there.
        """
        schema = (
            "file: data.csv (100 rows x 5 cols)\n"
            "load: pd.read_csv('data.csv', low_memory=False)\n"
            "columns: a, b, c, d, e"
        )
        result = _strip_first_row(schema)
        assert result.count("\n") == schema.count("\n")
        assert "file: data.csv" in result
        assert "columns:" in result

    def test_empty_input_returns_empty_string(self):
        assert _strip_first_row("") == ""

    def test_only_first_row_line_returns_empty(self):
        assert _strip_first_row("first_row: {'a': 1}") == ""

    def test_first_row_with_leading_whitespace_not_stripped(self):
        """The helper is strict: only lines whose raw text starts with
        ``first_row:`` are dropped. Indented variants (which we don't emit)
        should pass through. Pinning this so a future "be more lenient"
        change is deliberate.
        """
        schema = (
            "file: data.csv\n"
            "  first_row: {'indented': True}\n"
            "columns: a, b"
        )
        result = _strip_first_row(schema)
        assert "indented" in result

    def test_preserves_line_ordering(self):
        schema = (
            "file: a\n"
            "first_row: x\n"
            "columns: z\n"
            "note: extra\n"
        )
        lines = _strip_first_row(schema).splitlines()
        # Only the first_row line should be gone; relative order preserved.
        assert lines == ["file: a", "columns: z", "note: extra"]
