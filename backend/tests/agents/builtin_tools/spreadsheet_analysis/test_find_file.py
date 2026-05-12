"""Tests for ``_find_file`` — file lookup used by analyze_spreadsheet to
resolve a model-supplied filename to an S3-backed file record.

The lookup pulls from two sources: the assistant's knowledge base
(``_get_kb_files``) and the session's attachments (``_get_session_files``).
The twist is an alias pass: XLSX↔CSV for tabular files, so
``analyze_spreadsheet(filename="foo.csv", ...)`` resolves to the backing
``foo.xlsx`` (and vice versa). Without this, the model's "retry with the
sandbox filename" guess — which the docstring asks for — would fail at
the tool boundary (#206).

These tests pin down:
- exact-match wins over the alias pass
- aliasing only triggers for tabular extensions (no foo.pdf ↔ foo.docx)
- both sources contribute candidates
- case-insensitive exact match
"""

from unittest.mock import patch

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import _find_file


def _kb_file(filename: str, content_type: str = "") -> dict:
    return {
        "filename": filename,
        "source": "knowledge_base",
        "content_type": content_type,
        "size_bytes": 1234,
        "document_id": "doc-1",
        "s3_key": f"kb/{filename}",
    }


def _session_file(filename: str, content_type: str = "") -> dict:
    return {
        "filename": filename,
        "source": "chat_attachment",
        "content_type": content_type,
        "size_bytes": 1234,
        "document_id": "upload-1",
        "s3_key": f"session/{filename}",
        "s3_bucket": "test-bucket",
    }


class TestExactMatchWins:
    def test_exact_xlsx_match_in_session(self):
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("Report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("Report.xlsx", assistant_id=None, session_id="s1")
            assert result is not None
            assert result["filename"] == "Report.xlsx"

    def test_exact_csv_match_in_kb(self):
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[_kb_file("Q1.csv", "text/csv")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[],
        ):
            result = _find_file("Q1.csv", assistant_id="ast-1", session_id="s1")
            assert result is not None
            assert result["filename"] == "Q1.csv"
            assert result["source"] == "knowledge_base"

    def test_exact_match_case_insensitive(self):
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("Budget.XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("budget.xlsx", assistant_id=None, session_id="s1")
            assert result is not None
            assert result["filename"] == "Budget.XLSX"

    def test_exact_match_preferred_over_alias(self):
        """If both ``foo.xlsx`` and ``foo.csv`` exist and the model asks
        for ``foo.csv``, exact match should win — no surprise aliasing.
        """
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[
                _session_file("Data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                _session_file("Data.csv", "text/csv"),
            ],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("Data.csv", assistant_id=None, session_id="s1")
            assert result is not None
            assert result["filename"] == "Data.csv"


class TestAliasPass:
    def test_csv_request_resolves_xlsx_source(self):
        """Model asked for ``foo.csv`` (sandbox filename), only ``foo.xlsx``
        is attached. Alias pass finds it.
        """
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("FY_27_Ledger.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("FY_27_Ledger.csv", assistant_id=None, session_id="s1")
            assert result is not None
            assert result["filename"] == "FY_27_Ledger.xlsx"

    def test_xlsx_request_resolves_csv_source(self):
        """Reverse direction — model asked for ``foo.xlsx`` but only
        ``foo.csv`` is attached (rare but handled).
        """
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("Q3.csv", "text/csv")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("Q3.xlsx", assistant_id=None, session_id="s1")
            assert result is not None
            assert result["filename"] == "Q3.csv"

    def test_alias_only_applies_to_tabular(self):
        """``foo.pdf`` must not alias to ``foo.docx``. The alias pass is
        gated on target being a tabular extension.
        """
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("report.pdf", assistant_id=None, session_id="s1")
            assert result is None

    def test_alias_skips_non_tabular_candidate(self):
        """Even if the target is tabular, candidates with non-tabular
        content/type shouldn't match. Prevents e.g. alias bleeding
        ``.docx`` into a ``.csv`` request.
        """
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("data.pdf", "application/pdf")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ):
            result = _find_file("data.csv", assistant_id=None, session_id="s1")
            assert result is None


class TestSourceOrder:
    def test_kb_checked_before_session(self):
        """When assistant_id is set, KB files are consulted first. This
        matches behavior documented in the tool: the KB is the
        authoritative source for assistants.
        """
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[_kb_file("shared.csv", "text/csv")],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("shared.csv", "text/csv")],
        ):
            result = _find_file("shared.csv", assistant_id="ast-1", session_id="s1")
            assert result is not None
            assert result["source"] == "knowledge_base"

    def test_no_assistant_skips_kb_lookup(self):
        """With ``assistant_id=None``, KB is not queried — only session
        files. Avoids spurious DynamoDB calls on non-assistant chats.
        """
        kb_mock = patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[_kb_file("only-in-kb.csv", "text/csv")],
        )
        with kb_mock as kb, patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[_session_file("only-in-session.csv", "text/csv")],
        ):
            result = _find_file("only-in-kb.csv", assistant_id=None, session_id="s1")
            kb.assert_not_called()
            # KB file isn't visible; only session files considered.
            assert result is None

    def test_returns_none_when_not_found(self):
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            return_value=[],
        ), patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            return_value=[],
        ):
            assert _find_file("nope.csv", assistant_id="ast-1", session_id="s1") is None
