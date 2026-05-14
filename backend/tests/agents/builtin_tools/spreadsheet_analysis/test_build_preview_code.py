"""Tests for ``_build_preview_code`` — the schema-probe Python template
that runs inside the Code Interpreter sandbox.

Scope note: the sandbox runs in an AWS-managed container with pandas
preinstalled; the backend's own test environment does NOT bundle pandas.
That means we can't execute the template in-process here without pulling
pandas into backend dependencies — which nothing else needs. So these
tests focus on the template's **shape**: it must parse as valid Python,
quote the filename safely (including filenames with apostrophes or
double quotes), and include the expected scorer/marker scaffolding so
regressions to the template structure are caught.

Execution-level coverage of the scorer (does it correctly prescribe
``skiprows=4`` for a 4-row title preamble?) will land in a follow-up
issue to extract the scorer into a pure, directly-testable helper. See
#261.
"""

import ast

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    _SCHEMA_MARKER,
    _build_preview_code,
)


class TestPreviewCodeParsesAsValidPython:
    def test_simple_filename(self):
        ast.parse(_build_preview_code("data.csv"))

    def test_filename_with_apostrophe(self):
        """Regression: before the ``_FNAME`` indirection, a filename like
        ``O'Brien data.csv`` produced invalid Python because repr() emits
        double quotes around strings containing single quotes, conflicting
        with the template's outer f-string quoting.
        """
        ast.parse(_build_preview_code("O'Brien data.csv"))

    def test_filename_with_double_quote(self):
        """Double quotes in filenames should also survive — repr() picks
        single quotes when the string contains doubles.
        """
        ast.parse(_build_preview_code('say "hello".csv'))

    def test_filename_with_backslashes(self):
        ast.parse(_build_preview_code("path\\with\\backslashes.csv"))

    def test_filename_with_tabs_and_newlines(self):
        """Whitespace escapes — Python's repr uses \\t / \\n so the
        generated source stays on one line.
        """
        ast.parse(_build_preview_code("file\twith\ttabs.csv"))
        ast.parse(_build_preview_code("file\nwith\nnewlines.csv"))

    def test_filename_with_unicode(self):
        ast.parse(_build_preview_code("Ñiño.csv"))

    def test_filename_with_braces(self):
        """Curly braces in filenames must not be interpreted as f-string
        placeholders. ``_FNAME`` indirection sidesteps the issue.
        """
        ast.parse(_build_preview_code("{templated}.csv"))

    def test_empty_filename(self):
        """Empty strings should produce valid (if useless) Python — we
        don't want to fail tool construction on a bad filename; that's
        the call site's job.
        """
        ast.parse(_build_preview_code(""))


class TestPreviewCodeShape:
    def test_contains_schema_markers(self):
        code = _build_preview_code("x.csv")
        # The marker appears at least twice — once to open, once to close.
        assert code.count(repr(_SCHEMA_MARKER)) >= 2

    def test_emits_marker_on_failure_branch(self):
        """The template wraps its probe in try/except and emits the marker
        on the except path too, so a probe failure doesn't leave the
        outer parser hanging on a half-emitted schema.
        """
        code = _build_preview_code("x.csv")
        # Look for the failure branch's signature text. Resilience against
        # template churn: use a stable keyword rather than exact wording.
        assert "schema preview unavailable" in code

    def test_scorer_iterates_skiprows_0_to_8(self):
        """Regression: the probe range is deliberate. If someone shortens
        it, the scorer can't find the right header on deeply-nested
        report exports.
        """
        code = _build_preview_code("x.csv")
        assert "range(9)" in code

    def test_references_pandas(self):
        code = _build_preview_code("x.csv")
        assert "pandas" in code
        assert "pd.read_csv" in code

    def test_stores_filename_in_local_once(self):
        """The template references ``_FNAME`` rather than re-interpolating
        the raw filename into every usage. Pin this to keep the quoting
        bug from regressing if someone "simplifies" the template.
        """
        code = _build_preview_code("whatever.csv")
        # Exactly one assignment of _FNAME.
        assert code.count("_FNAME = ") == 1
        # All file operations use the local, not a re-interpolated literal.
        for expected in (
            "open(_FNAME",
            "pd.read_csv(_FNAME, nrows=0",
            "pd.read_csv(_FNAME, skiprows=",
        ):
            assert expected in code

    def test_confidence_gate_still_present(self):
        """The ``_prescribe`` gate is what prevents over-eager skiprows
        recommendations. If it disappears, the scorer will happily point
        the model at a data-row-as-header and regressions become silent.
        """
        code = _build_preview_code("x.csv")
        assert "_prescribe" in code
        # The gate checks all three conditions — drop any and we're
        # back to pre-gate behavior.
        assert "_best_skip > 0" in code
        assert "_win_clean_ratio" in code
