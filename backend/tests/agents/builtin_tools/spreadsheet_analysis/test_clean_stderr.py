"""Tests for ``_clean_stderr`` — strips pandas internal frames and warning
noise from Code Interpreter tracebacks, keeping only the user-code frame
and the final exception line.

Fixtures model real tracebacks surfaced by the interpreter: KeyError from
a missing column, ValueError from a bad dtype cast, FileNotFoundError from
an incorrect filename, a SyntaxError from malformed python_code, and a
malformed blob that doesn't match the expected traceback shape.

These tests are important because ``_clean_stderr`` output is what the
model sees on retry. Regressions here either flood the model with
irrelevant noise (bad retries, wasted tokens) or swallow the real error
(stuck retries). See #261.
"""

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    MAX_ERROR_CHARS,
    _clean_stderr,
)


class TestCleanStderrEmptyInput:
    def test_empty_string_returns_placeholder(self):
        assert _clean_stderr("") == "Unknown error"

    def test_none_returns_placeholder(self):
        assert _clean_stderr(None) == "Unknown error"  # type: ignore[arg-type]


class TestCleanStderrKeyError:
    """pandas KeyError — the most common failure: wrong column name."""

    TRACEBACK = """Traceback (most recent call last):
  File "/tmp/ipykernel_42/user_code.py", line 3, in <module>
    total = df['NET_AMOUNT_MISSPELLED'].sum()
            ~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/venv/lib/python3.12/site-packages/pandas/core/frame.py", line 4090, in __getitem__
    indexer = self.columns.get_loc(key)
  File "/opt/venv/lib/python3.12/site-packages/pandas/core/indexes/base.py", line 3812, in get_loc
    raise KeyError(key) from err
KeyError: 'NET_AMOUNT_MISSPELLED'
"""

    def test_keeps_user_frame(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "user_code.py" in cleaned
        assert "NET_AMOUNT_MISSPELLED" in cleaned

    def test_drops_pandas_internal_frames(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "site-packages/pandas/" not in cleaned
        assert "pandas/core/frame.py" not in cleaned
        assert "get_loc" not in cleaned

    def test_includes_final_exception(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "KeyError" in cleaned
        # The actual missing key should survive
        assert "'NET_AMOUNT_MISSPELLED'" in cleaned

    def test_within_budget(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert len(cleaned) <= MAX_ERROR_CHARS


class TestCleanStderrValueError:
    TRACEBACK = """Traceback (most recent call last):
  File "/tmp/ipykernel_99/script.py", line 7, in <module>
    df['amount'] = df['amount'].astype(int)
                   ~~~~~~~~~~~~~~~~~~~~^^^^^
  File "/opt/venv/lib/python3.12/site-packages/pandas/core/generic.py", line 6534, in astype
    new_data = self._mgr.astype(dtype=dtype, copy=copy, errors=errors)
  File "/opt/venv/lib/python3.12/site-packages/pandas/core/internals/managers.py", line 414, in astype
    return self.apply(
ValueError: invalid literal for int() with base 10: '$1,234.56'
"""

    def test_user_frame_kept(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "script.py" in cleaned
        assert "astype(int)" in cleaned

    def test_exception_kept(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "ValueError" in cleaned
        assert "'$1,234.56'" in cleaned

    def test_pandas_internals_dropped(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "generic.py" not in cleaned
        assert "managers.py" not in cleaned


class TestCleanStderrFileNotFoundError:
    """The XLSX/CSV mismatch path — model points at the wrong filename."""

    TRACEBACK = """Traceback (most recent call last):
  File "/tmp/ipykernel_1/user_code.py", line 2, in <module>
    df = pd.read_csv('FY_27_Ledger.xlsx', low_memory=False)
  File "/opt/venv/lib/python3.12/site-packages/pandas/io/parsers/readers.py", line 1026, in read_csv
    return _read(filepath_or_buffer, kwds)
FileNotFoundError: [Errno 2] No such file or directory: 'FY_27_Ledger.xlsx'
"""

    def test_filename_preserved_for_targeted_hint(self):
        """The outer tool matches on ``filename in error_msg`` to trigger
        the xlsx→csv retry hint — the cleaner must keep the source
        filename readable.
        """
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "FY_27_Ledger.xlsx" in cleaned

    def test_exception_name_preserved(self):
        assert "FileNotFoundError" in _clean_stderr(self.TRACEBACK)

    def test_pandas_reader_frame_dropped(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "readers.py" not in cleaned


class TestCleanStderrSyntaxError:
    """Model wrote broken python_code — no useful stack, just the syntax
    error line and caret.
    """

    TRACEBACK = """  File "/tmp/ipykernel_5/broken.py", line 2
    df = pd.read_csv('x.csv'
                           ^
SyntaxError: '(' was never closed
"""

    def test_syntax_error_surfaced(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "SyntaxError" in cleaned
        assert "never closed" in cleaned

    def test_user_frame_preserved(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "broken.py" in cleaned


class TestCleanStderrMalformed:
    """If the traceback doesn't match the expected shape, we should still
    return *something* useful (a tail of the raw stderr) rather than blank.
    """

    def test_no_exception_line_returns_tail(self):
        weird = "line1\nline2\nline3\n\nunexpected output without traceback"
        cleaned = _clean_stderr(weird)
        assert cleaned != ""
        assert len(cleaned) > 0

    def test_tail_bounded_by_budget(self):
        """Malformed output should not exceed the error budget — prevents a
        multi-kilobyte dump of unrelated stderr from eating tool result
        space on retries.
        """
        weird = "\n".join(f"random noise line {i}" for i in range(200))
        cleaned = _clean_stderr(weird)
        assert len(cleaned) <= MAX_ERROR_CHARS


class TestCleanStderrWarnings:
    """DtypeWarning / FutureWarning / UserWarning are pandas noise that
    appear *above* the real error. The cleaner drops the warning line and
    its call-site follow-up.
    """

    TRACEBACK = """/opt/venv/lib/python3.12/site-packages/pandas/io/parsers/readers.py:622: DtypeWarning: Columns (17) have mixed types. Specify dtype option on import or set low_memory=False.
  return _read(filepath_or_buffer, kwds)
Traceback (most recent call last):
  File "/tmp/ipykernel_7/code.py", line 4, in <module>
    print(df['NET'].sum())
          ~~^^^^^^^
KeyError: 'NET'
"""

    def test_warning_dropped(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "DtypeWarning" not in cleaned
        assert "mixed types" not in cleaned

    def test_real_error_preserved(self):
        cleaned = _clean_stderr(self.TRACEBACK)
        assert "KeyError" in cleaned
        assert "'NET'" in cleaned
        assert "code.py" in cleaned


class TestCleanStderrTruncation:
    def test_output_clamped_to_max_error_chars(self):
        """A long user-code frame shouldn't push the cleaned output past
        MAX_ERROR_CHARS. Truncation appends an ellipsis marker.
        """
        long_traceback = (
            "Traceback (most recent call last):\n"
            f"  File \"/tmp/ipykernel_1/code.py\", line 1, in <module>\n"
            f"    {'x' * 2000}\n"
            "ValueError: super long error message " + "y" * 1000 + "\n"
        )
        cleaned = _clean_stderr(long_traceback)
        assert len(cleaned) <= MAX_ERROR_CHARS
