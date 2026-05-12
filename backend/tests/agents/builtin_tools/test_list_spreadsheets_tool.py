"""
Tests for list_spreadsheets_tool async correctness (issue #260).

Verifies that:
- _get_session_files directly awaits the async repository (no asyncio-in-asyncio).
- _get_kb_files offloads the blocking boto3 call to asyncio.to_thread and does
  not block the running event loop.
- make_list_spreadsheets_tool returns a coroutine function (async @tool).
- The tool correctly filters non-tabular files and formats its output.
"""

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs for external types
# ---------------------------------------------------------------------------


@dataclass
class FakeFileMetadata:
    """Minimal stand-in for apis.shared.files.models.FileMetadata."""

    filename: str
    mime_type: str
    size_bytes: int
    upload_id: str
    s3_key: str
    s3_bucket: str


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def csv_session_file() -> FakeFileMetadata:
    return FakeFileMetadata(
        filename="budget.csv",
        mime_type="text/csv",
        size_bytes=4096,
        upload_id="upload-1",
        s3_key="sessions/s1/budget.csv",
        s3_bucket="my-bucket",
    )


@pytest.fixture
def pdf_session_file() -> FakeFileMetadata:
    return FakeFileMetadata(
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=102400,
        upload_id="upload-2",
        s3_key="sessions/s1/report.pdf",
        s3_bucket="my-bucket",
    )


# ---------------------------------------------------------------------------
# _get_session_files — must await the repository directly (no thread tricks)
# ---------------------------------------------------------------------------


class TestGetSessionFiles:
    @pytest.mark.asyncio
    async def test_awaits_repository_directly(self, csv_session_file):
        """_get_session_files should await repo.list_session_files, not spawn threads."""
        mock_repo = MagicMock()
        mock_repo.list_session_files = AsyncMock(return_value=[csv_session_file])

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.get_file_upload_repository",
                return_value=mock_repo,
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.is_tabular_file",
                return_value=True,
            ),
        ):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                _get_session_files,
            )

            files = await _get_session_files("session-1")

        mock_repo.list_session_files.assert_awaited_once_with("session-1")
        assert len(files) == 1
        assert files[0]["filename"] == "budget.csv"
        assert files[0]["source"] == "chat_attachment"

    @pytest.mark.asyncio
    async def test_filters_non_tabular_files(self, csv_session_file, pdf_session_file):
        """Only CSV/XLSX files should pass through the tabular filter."""
        mock_repo = MagicMock()
        mock_repo.list_session_files = AsyncMock(
            return_value=[csv_session_file, pdf_session_file]
        )

        def _is_tabular(filename: str, mime: str) -> bool:
            return filename.endswith(".csv") or filename.endswith(".xlsx")

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.get_file_upload_repository",
                return_value=mock_repo,
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.is_tabular_file",
                side_effect=_is_tabular,
            ),
        ):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                _get_session_files,
            )

            files = await _get_session_files("session-1")

        assert len(files) == 1
        assert files[0]["filename"] == "budget.csv"

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_exception(self):
        """Errors in the repository should be swallowed, returning []."""
        mock_repo = MagicMock()
        mock_repo.list_session_files = AsyncMock(side_effect=RuntimeError("DDB down"))

        with patch(
            "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.get_file_upload_repository",
            return_value=mock_repo,
        ):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                _get_session_files,
            )

            files = await _get_session_files("session-x")

        assert files == []


# ---------------------------------------------------------------------------
# _get_kb_files — must use asyncio.to_thread (not block the loop directly)
# ---------------------------------------------------------------------------


class TestGetKbFiles:
    @pytest.mark.asyncio
    async def test_uses_asyncio_to_thread(self):
        """_get_kb_files should offload the DynamoDB query via asyncio.to_thread."""
        fake_items = [
            {
                "PK": "AST#ast-1",
                "SK": "DOC#doc-1",
                "status": "complete",
                "filename": "ledger.csv",
                "contentType": "text/csv",
                "sizeBytes": 2048,
                "documentId": "doc-1",
                "s3Key": "assistants/ast-1/ledger.csv",
            }
        ]

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": fake_items}

        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        calls: List[str] = []

        async def _capturing_to_thread(fn, *args, **kwargs):
            """Run fn synchronously but record that to_thread was called."""
            calls.append("to_thread")
            return fn(*args, **kwargs)

        with (
            patch("boto3.resource", return_value=mock_dynamodb),
            patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "AssistantsTable"}),
            patch("asyncio.to_thread", side_effect=_capturing_to_thread),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.is_tabular_file",
                return_value=True,
            ),
        ):
            from agents.builtin_tools.spreadsheet_analysis import list_spreadsheets_tool

            # Reload to pick up patched asyncio.to_thread
            import importlib
            importlib.reload(list_spreadsheets_tool)

            files = await list_spreadsheets_tool._get_kb_files("ast-1")

        assert "to_thread" in calls, "_get_kb_files must use asyncio.to_thread"
        assert len(files) == 1
        assert files[0]["filename"] == "ledger.csv"
        assert files[0]["source"] == "knowledge_base"

    @pytest.mark.asyncio
    async def test_skips_incomplete_documents(self):
        """Documents with status != 'complete' must be excluded."""
        fake_items = [
            {"status": "processing", "filename": "draft.csv", "contentType": "text/csv",
             "sizeBytes": 512, "documentId": "d1", "s3Key": "k1"},
            {"status": "complete",   "filename": "final.csv", "contentType": "text/csv",
             "sizeBytes": 1024, "documentId": "d2", "s3Key": "k2"},
        ]
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": fake_items}
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        with (
            patch("boto3.resource", return_value=mock_dynamodb),
            patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "AssistantsTable"}),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool.is_tabular_file",
                return_value=True,
            ),
        ):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                _get_kb_files,
            )
            files = await _get_kb_files("ast-1")

        assert len(files) == 1
        assert files[0]["filename"] == "final.csv"

    @pytest.mark.asyncio
    async def test_returns_empty_when_env_var_missing(self):
        """No DYNAMODB_ASSISTANTS_TABLE_NAME → return [] without crashing."""
        with patch.dict("os.environ", {}, clear=True):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                _get_kb_files,
            )
            files = await _get_kb_files("ast-1")

        assert files == []


# ---------------------------------------------------------------------------
# make_list_spreadsheets_tool — returned tool must be a coroutine function
# ---------------------------------------------------------------------------


class TestMakeListSpreadsheetsTool:
    def test_returns_async_callable(self):
        """The factory must return an async @tool function (fixes #260)."""
        from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
            make_list_spreadsheets_tool,
        )

        tool_fn = make_list_spreadsheets_tool(
            assistant_id=None,
            session_id="s1",
            user_id="u1",
        )

        assert inspect.iscoroutinefunction(tool_fn), (
            "list_spreadsheets must be async so Strands can await it (issue #260)"
        )

    @pytest.mark.asyncio
    async def test_returns_empty_message_when_no_files(self):
        """Tool returns an informative message when no tabular files are found."""
        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_session_files",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                make_list_spreadsheets_tool,
            )

            tool_fn = make_list_spreadsheets_tool(
                assistant_id="ast-1",
                session_id="s1",
                user_id="u1",
            )
            result = await tool_fn()

        assert result["status"] == "success"
        assert "No spreadsheet files" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_lists_files_from_both_sources(self):
        """Files from KB and session are combined in the output."""
        kb_file = {
            "filename": "ledger.csv",
            "source": "knowledge_base",
            "size_bytes": 2048,
            "content_type": "text/csv",
            "document_id": "doc-1",
            "s3_key": "k1",
        }
        session_file = {
            "filename": "budget.csv",
            "source": "chat_attachment",
            "size_bytes": 1024,
            "content_type": "text/csv",
            "document_id": "upload-1",
            "s3_key": "k2",
            "s3_bucket": "bucket",
        }

        with (
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
                new=AsyncMock(return_value=[kb_file]),
            ),
            patch(
                "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_session_files",
                new=AsyncMock(return_value=[session_file]),
            ),
        ):
            from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
                make_list_spreadsheets_tool,
            )

            tool_fn = make_list_spreadsheets_tool(
                assistant_id="ast-1",
                session_id="s1",
                user_id="u1",
            )
            result = await tool_fn()

        assert result["status"] == "success"
        assert len(result["files"]) == 2
        filenames = {f["filename"] for f in result["files"]}
        assert filenames == {"ledger.csv", "budget.csv"}
