"""Unit tests for the small pure helpers in analyze_tool.py.

These cover the boring-but-critical glue: output truncation, schema-marker
extraction, sheet-name sanitization, and safe int parsing. The logic is
simple so the tests are small — their job is to lock in the current
behavior so the async refactor doesn't regress the happy paths (#261).
"""

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    MAX_OUTPUT_CHARS,
    _extract_schema_preview,
    _safe_int,
    _sanitize_sheet_name,
    _truncate_output,
    _SCHEMA_MARKER,
)


class TestTruncateOutput:
    def test_empty_returns_empty(self):
        assert _truncate_output("") == ""

    def test_none_returns_none(self):
        # The helper short-circuits on falsy inputs. Preserve that.
        assert _truncate_output(None) is None  # type: ignore[arg-type]

    def test_under_cap_unchanged(self):
        text = "x" * (MAX_OUTPUT_CHARS - 1)
        assert _truncate_output(text) == text

    def test_at_cap_unchanged(self):
        text = "x" * MAX_OUTPUT_CHARS
        assert _truncate_output(text) == text

    def test_over_cap_truncated_with_marker(self):
        text = "x" * (MAX_OUTPUT_CHARS + 500)
        out = _truncate_output(text)
        assert out.startswith("x" * MAX_OUTPUT_CHARS)
        assert "truncated" in out
        assert f"{MAX_OUTPUT_CHARS:,}" in out
        assert f"{len(text):,}" in out


class TestExtractSchemaPreview:
    def test_no_marker_returns_empty_block_and_full_stdout(self):
        stdout = "some tool output\nwith no marker\n"
        schema, remaining = _extract_schema_preview(stdout)
        assert schema == ""
        assert remaining == stdout

    def test_full_block_between_markers(self):
        stdout = (
            f"{_SCHEMA_MARKER}\n"
            "file: data.csv (10 rows x 3 cols)\n"
            "columns: a, b, c\n"
            f"{_SCHEMA_MARKER}\n"
        )
        schema, remaining = _extract_schema_preview(stdout)
        assert "file: data.csv" in schema
        assert "columns: a, b, c" in schema
        # The remaining stdout should be empty (or a stripped empty string)
        assert remaining == "" or remaining.strip() == ""

    def test_schema_surrounded_by_user_output(self):
        """User code may print before AND after the schema block.

        The helper should pull out just the schema and preserve both sides
        of the user stdout — important because the tool concatenates the
        two halves back together when rendering the final response.
        """
        stdout = (
            "Hello from user code\n"
            f"{_SCHEMA_MARKER}\n"
            "file: data.csv\n"
            f"{_SCHEMA_MARKER}\n"
            "After schema user output\n"
        )
        schema, remaining = _extract_schema_preview(stdout)
        assert "file: data.csv" in schema
        assert "Hello from user code" in remaining
        assert "After schema user output" in remaining

    def test_marker_present_only_once_returns_empty_block(self):
        """A single marker (not bracketed) is malformed — treat as no schema.

        Prevents us from accidentally surfacing half of a stream as a
        "schema" when the bootstrap failed mid-emit.
        """
        stdout = f"partial {_SCHEMA_MARKER}\ntruncated"
        schema, remaining = _extract_schema_preview(stdout)
        assert schema == ""
        # Original stdout returned on the malformed path
        assert remaining == stdout


class TestSafeInt:
    def test_parses_int(self):
        assert _safe_int("42") == 42

    def test_parses_large_int(self):
        assert _safe_int("1000000") == 1_000_000

    def test_strips_whitespace(self):
        assert _safe_int("  7  ") == 7

    def test_returns_zero_for_empty(self):
        assert _safe_int("") == 0

    def test_returns_zero_for_garbage(self):
        assert _safe_int("not-a-number") == 0

    def test_returns_zero_for_none(self):
        assert _safe_int(None) == 0  # type: ignore[arg-type]

    def test_parses_negative(self):
        assert _safe_int("-5") == -5


class TestSanitizeSheetName:
    def test_simple_name_lowercased(self):
        assert _sanitize_sheet_name("Summary") == "summary"

    def test_spaces_become_underscore(self):
        assert _sanitize_sheet_name("Q1 2026") == "q1_2026"

    def test_multiple_non_alnum_collapse_to_single_underscore(self):
        assert _sanitize_sheet_name("Q1   ---  2026") == "q1_2026"

    def test_slashes_replaced(self):
        assert _sanitize_sheet_name("Sales/2026") == "sales_2026"

    def test_unicode_replaced(self):
        # Non-ASCII characters aren't in [A-Za-z0-9] so they all collapse.
        assert _sanitize_sheet_name("Ñiño") == "i_o"

    def test_leading_trailing_punctuation_stripped(self):
        assert _sanitize_sheet_name("--Budget--") == "budget"

    def test_empty_returns_fallback(self):
        assert _sanitize_sheet_name("") == "sheet"

    def test_all_punctuation_returns_fallback(self):
        # Everything collapses to "" post-strip, fallback kicks in.
        assert _sanitize_sheet_name("---") == "sheet"

    def test_deterministic(self):
        # Same input always yields same output — callers rely on this
        # to predict filenames.
        assert _sanitize_sheet_name("Q1 2026") == _sanitize_sheet_name("Q1 2026")
