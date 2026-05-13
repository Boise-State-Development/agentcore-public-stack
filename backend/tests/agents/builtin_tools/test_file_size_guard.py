"""Unit tests for the file-size guard added to analyze_spreadsheet (issue #258).

All tests are self-contained — no AWS credentials or Code Interpreter required.
The guard runs before any S3 download, so we only need to mock _find_file.

Note: the Strands @tool decorator (v1.39) runs the underlying coroutine to
completion synchronously via its own event-loop management, so the wrapped
function is called as a plain function — NOT awaited.

Run from backend/:
    uv run --extra agentcore --extra dev python -m pytest tests/agents/builtin_tools/test_file_size_guard.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    FILE_SIZE_HARD_BYTES,
    FILE_SIZE_WARN_BYTES,
    make_analyze_tool,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BASE_FILE_INFO = {
    "filename": "data.csv",
    "source": "chat_attachment",
    "content_type": "text/csv",
    "document_id": "doc-1",
    "s3_key": "sessions/s1/data.csv",
    "s3_bucket": "my-bucket",
}


def _make_tool():
    """Create the analyze_spreadsheet tool with a dummy context."""
    return make_analyze_tool(
        assistant_id=None,
        session_id="session-1",
        user_id="user-1",
    )


def _file_info(size_bytes: int) -> dict:
    return {**_BASE_FILE_INFO, "size_bytes": size_bytes}


# ---------------------------------------------------------------------------
# Test 1 — Hard limit: file >= 25 MB → error returned, S3 never called
# ---------------------------------------------------------------------------


class TestHardSizeLimit:
    def test_rejects_file_over_25mb(self):
        """Files over FILE_SIZE_HARD_BYTES must be rejected before download."""
        oversized = FILE_SIZE_HARD_BYTES + 1  # 25 MB + 1 byte

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
                return_value=_file_info(oversized),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
                return_value="ci-123",
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._download_file",
            ) as mock_download,
        ):
            fn = _make_tool()
            result = fn(filename="data.csv", python_code="print('hi')")

        assert result["status"] == "error"
        assert "❌" in result["content"][0]["text"]
        assert "25 MB" in result["content"][0]["text"]
        # Critical: S3 download must never be called for oversized files.
        mock_download.assert_not_called()

    def test_error_message_contains_actual_size(self):
        """The error message must tell the user the actual file size."""
        size = 30 * 1024 * 1024  # 30 MB

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
                return_value=_file_info(size),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
                return_value="ci-123",
            ),
            patch("agents.builtin_tools.spreadsheet_analysis.analyze_tool._download_file"),
        ):
            fn = _make_tool()
            result = fn(filename="data.csv", python_code="print('hi')")

        assert result["status"] == "error"
        assert "30.0 MB" in result["content"][0]["text"]

    def test_exact_hard_limit_is_rejected(self):
        """A file at exactly FILE_SIZE_HARD_BYTES must also be rejected (>= guard)."""
        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
                return_value=_file_info(FILE_SIZE_HARD_BYTES),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
                return_value="ci-123",
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._download_file",
            ) as mock_download,
        ):
            fn = _make_tool()
            result = fn(filename="data.csv", python_code="print('hi')")

        assert result["status"] == "error"
        mock_download.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2 — Soft warning: 10 MB <= file < 25 MB → success + ⚠️ warning
# ---------------------------------------------------------------------------


class TestSoftSizeWarning:
    def _mock_ci(self, stdout: str = "42\n"):
        """Minimal CodeInterpreter mock that returns the given stdout."""
        ci = MagicMock()
        ci.start.return_value = None
        ci.stop.return_value = None
        ci.invoke.return_value = {
            "stream": [
                {
                    "result": {
                        "isError": False,
                        "structuredContent": {"stdout": stdout, "stderr": ""},
                    }
                }
            ]
        }
        return ci

    def test_large_file_gets_warning_prepended(self):
        """Files between 10–25 MB must succeed but include a ⚠️ warning."""
        large_size = FILE_SIZE_WARN_BYTES + 1  # just over 10 MB
        mock_ci = self._mock_ci("result: 99\n")

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
                return_value=_file_info(large_size),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
                return_value="ci-123",
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._download_file",
                return_value=b"csvdata",
            ),
            patch(
                "bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter",
                return_value=mock_ci,
            ),
        ):
            fn = _make_tool()
            result = fn(filename="data.csv", python_code="print(99)")

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "⚠️" in text
        assert "result: 99" in text  # actual output still present

    def test_file_below_warn_threshold_has_no_warning(self):
        """Files under 10 MB must have no warning in the output."""
        small_size = FILE_SIZE_WARN_BYTES - 1  # just under 10 MB
        mock_ci = self._mock_ci("done\n")

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
                return_value=_file_info(small_size),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
                return_value="ci-123",
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._download_file",
                return_value=b"csvdata",
            ),
            patch(
                "bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter",
                return_value=mock_ci,
            ),
        ):
            fn = _make_tool()
            result = fn(filename="data.csv", python_code="print('done')")

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "⚠️" not in text
        assert "MB" not in text


# ---------------------------------------------------------------------------
# Test 3 — size_bytes missing → treated as 0, guard never fires
# ---------------------------------------------------------------------------


class TestMissingSizeBytes:
    def test_missing_size_bytes_defaults_to_zero(self):
        """If size_bytes is absent from file_info, guard must not fire."""
        file_without_size = {k: v for k, v in _BASE_FILE_INFO.items()}

        mock_ci = MagicMock()
        mock_ci.start.return_value = None
        mock_ci.stop.return_value = None
        mock_ci.invoke.return_value = {
            "stream": [
                {
                    "result": {
                        "isError": False,
                        "structuredContent": {"stdout": "ok\n", "stderr": ""},
                    }
                }
            ]
        }

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
                return_value=file_without_size,
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
                return_value="ci-123",
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.analyze_tool._download_file",
                return_value=b"data",
            ),
            patch(
                "bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter",
                return_value=mock_ci,
            ),
        ):
            fn = _make_tool()
            result = fn(filename="data.csv", python_code="print('ok')")

        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Test 4 — list_spreadsheets size labels (sync helpers on main branch)
# ---------------------------------------------------------------------------


class TestSizeLabelsInListOutput:
    """list_spreadsheets must surface ⛔ / ⚠️ labels for large files."""

    def test_oversized_file_shows_blocked_label(self):
        from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
            make_list_spreadsheets_tool,
        )

        oversized_file = {**_BASE_FILE_INFO, "size_bytes": 30 * 1024 * 1024}

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
                return_value=[],
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_session_files",
                return_value=[oversized_file],
            ),
        ):
            tool_fn = make_list_spreadsheets_tool(
                assistant_id=None, session_id="s1", user_id="u1"
            )
            result = tool_fn()

        text = result["content"][0]["text"]
        assert "⛔" in text
        assert "exceeds" in text

    def test_large_file_shows_warning_label(self):
        from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
            make_list_spreadsheets_tool,
        )

        large_file = {**_BASE_FILE_INFO, "size_bytes": 15 * 1024 * 1024}

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
                return_value=[],
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_session_files",
                return_value=[large_file],
            ),
        ):
            tool_fn = make_list_spreadsheets_tool(
                assistant_id=None, session_id="s1", user_id="u1"
            )
            result = tool_fn()

        text = result["content"][0]["text"]
        assert "⚠️" in text

    def test_small_file_shows_kb_only(self):
        from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
            make_list_spreadsheets_tool,
        )

        small_file = {**_BASE_FILE_INFO, "size_bytes": 512 * 1024}

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
                return_value=[],
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_session_files",
                return_value=[small_file],
            ),
        ):
            tool_fn = make_list_spreadsheets_tool(
                assistant_id=None, session_id="s1", user_id="u1"
            )
            result = tool_fn()

        text = result["content"][0]["text"]
        assert "KB" in text
        assert "⛔" not in text
        assert "⚠️" not in text
