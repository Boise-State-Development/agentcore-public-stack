"""Tests for the ``list_spreadsheets`` tool factory and its two private
helpers (``_get_kb_files``, ``_get_session_files``).

The factory (``make_list_spreadsheets_tool``) builds a closure-bound tool
the agent can invoke. Helper-level tests exercise the real boto3 /
DynamoDB paths via moto so the query / filter / field-mapping logic is
under test, not mocked out.

After #260 the helpers and the tool itself are ``async def``; tests that
invoke them are marked with ``@pytest.mark.asyncio`` and use ``await``.

See #261.
"""

import pytest

from agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool import (
    _get_kb_files,
    _get_session_files,
    _is_tabular_file,
    make_list_spreadsheets_tool,
)


# ---------------------------------------------------------------------------
# _is_tabular_file — thin wrapper; delegate to shared is_tabular_file
# ---------------------------------------------------------------------------


class TestIsTabularFile:
    def test_csv_by_extension(self):
        assert _is_tabular_file("data.csv", "") is True

    def test_csv_by_mime(self):
        assert _is_tabular_file("anything", "text/csv") is True

    def test_xlsx_by_extension(self):
        assert _is_tabular_file("data.xlsx", "") is True

    def test_xlsx_by_mime(self):
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert _is_tabular_file("anything", mime) is True

    def test_pdf_rejected(self):
        assert _is_tabular_file("report.pdf", "application/pdf") is False

    def test_docx_rejected(self):
        docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert _is_tabular_file("report.docx", docx_mime) is False


# ---------------------------------------------------------------------------
# list_spreadsheets tool — factory + invocation
# ---------------------------------------------------------------------------


def _call_tool(tool) -> dict:
    """Invoke a Strands-decorated async tool and unwrap the result.

    ``@tool`` returns a wrapper that exposes the original coroutine
    function via ``__wrapped__``. We ``await`` it from the test, which
    must be marked ``@pytest.mark.asyncio``.
    """
    fn = getattr(tool, "__wrapped__", None) or tool
    return fn()


class TestMakeListSpreadsheetsTool:
    @pytest.mark.asyncio
    async def test_empty_state_returns_helpful_message(self, file_sources):
        set_kb, set_session = file_sources
        set_kb([])
        set_session([])

        tool = make_list_spreadsheets_tool(
            assistant_id="ast-1", session_id="s1", user_id="u1"
        )
        result = await _call_tool(tool)

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "No spreadsheet files" in text
        # "files" key should NOT be present on the empty path so the model
        # doesn't loop on an empty list.
        assert "files" not in result

    @pytest.mark.asyncio
    async def test_kb_and_session_files_merged(self, file_sources, file_factories):
        set_kb, set_session = file_sources
        set_kb([file_factories["kb_xlsx"]("Budget.xlsx")])
        set_session([file_factories["session_csv"]("notes.csv")])

        tool = make_list_spreadsheets_tool(
            assistant_id="ast-1", session_id="s1", user_id="u1"
        )
        result = await _call_tool(tool)

        assert result["status"] == "success"
        filenames = [f["filename"] for f in result["files"]]
        assert filenames == ["Budget.xlsx", "notes.csv"]

        text = result["content"][0]["text"]
        assert "Budget.xlsx" in text
        assert "knowledge_base" in text
        assert "notes.csv" in text
        assert "chat_attachment" in text

    @pytest.mark.asyncio
    async def test_no_assistant_skips_kb_call(self, file_sources):
        """Without an assistant_id, KB files aren't queried — locks in the
        conditional branch so we don't regress and start spamming DynamoDB
        on non-assistant chats.
        """
        from unittest.mock import patch

        kb_calls = []

        async def _track(_aid):
            kb_calls.append(_aid)
            return []

        set_kb, set_session = file_sources
        set_session([])
        with patch(
            "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
            side_effect=_track,
        ):
            tool = make_list_spreadsheets_tool(
                assistant_id=None, session_id="s1", user_id="u1"
            )
            await _call_tool(tool)

        assert kb_calls == [], "KB lookup should be skipped when assistant_id is None"

    @pytest.mark.asyncio
    async def test_size_formatted_in_kb(self, file_sources, file_factories):
        """Files are rendered with their size in KB for the preview text.
        Pinning this so the formatter change doesn't silently regress.
        """
        set_kb, set_session = file_sources
        set_kb([])
        set_session([file_factories["session_csv"]("tiny.csv", size=2560)])

        tool = make_list_spreadsheets_tool(
            assistant_id=None, session_id="s1", user_id="u1"
        )
        result = await _call_tool(tool)
        text = result["content"][0]["text"]
        # 2560 bytes → 3 KB with the current round-to-nearest formatter.
        assert "3 KB" in text or "2 KB" in text  # allow either rounding


# ---------------------------------------------------------------------------
# _get_kb_files — DynamoDB query with status filter, via moto
# ---------------------------------------------------------------------------


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestGetKbFilesDynamoDB:
    """Exercise the real DynamoDB query path against a moto-backed table.

    This replaces the earlier MagicMock-based tests: those verified that
    ``table.query`` was called, but didn't actually check the schema
    (attribute names, key-condition expression) matches what production
    writes. Moto does.

    ``_get_kb_files`` is ``async def`` (see #260) so each test awaits it.
    """

    @pytest.mark.asyncio
    async def test_no_table_env_returns_empty(self, monkeypatch):
        """Helper bails out cleanly when the env var isn't set at all,
        rather than crashing on a missing table.
        """
        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME", raising=False)
        assert await _get_kb_files("ast-1") == []

    @pytest.mark.asyncio
    async def test_completed_tabular_file_included(self, assistants_table, seed_kb_doc):
        seed_kb_doc(
            assistant_id="ast-1",
            filename="Budget.xlsx",
            content_type=XLSX_MIME,
            size_bytes=1024,
        )
        files = await _get_kb_files("ast-1")
        assert len(files) == 1
        assert files[0]["filename"] == "Budget.xlsx"
        assert files[0]["source"] == "knowledge_base"
        assert files[0]["size_bytes"] == 1024

    @pytest.mark.asyncio
    async def test_non_tabular_file_filtered_out(self, assistants_table, seed_kb_doc):
        seed_kb_doc(
            assistant_id="ast-1",
            filename="report.pdf",
            content_type="application/pdf",
        )
        assert await _get_kb_files("ast-1") == []

    @pytest.mark.asyncio
    async def test_incomplete_status_filtered_out(self, assistants_table, seed_kb_doc):
        seed_kb_doc(
            assistant_id="ast-1",
            filename="Pending.xlsx",
            content_type=XLSX_MIME,
            status="processing",  # not "complete"
        )
        assert await _get_kb_files("ast-1") == []

    @pytest.mark.asyncio
    async def test_mixed_statuses_filters_correctly(self, assistants_table, seed_kb_doc):
        seed_kb_doc(assistant_id="ast-1", filename="done.csv",
                    content_type="text/csv", status="complete")
        seed_kb_doc(assistant_id="ast-1", filename="broken.csv",
                    content_type="text/csv", status="failed")
        seed_kb_doc(assistant_id="ast-1", filename="notes.txt",
                    content_type="text/plain", status="complete")

        files = await _get_kb_files("ast-1")
        assert len(files) == 1
        assert files[0]["filename"] == "done.csv"

    @pytest.mark.asyncio
    async def test_isolates_by_assistant_id(self, assistants_table, seed_kb_doc):
        """The ``PK = AST#<id>`` key condition partitions by assistant.
        Documents under a different assistant must not leak through.
        """
        seed_kb_doc(assistant_id="ast-1", filename="mine.csv",
                    content_type="text/csv")
        seed_kb_doc(assistant_id="ast-other", filename="theirs.csv",
                    content_type="text/csv")

        files = await _get_kb_files("ast-1")
        assert [f["filename"] for f in files] == ["mine.csv"]

    @pytest.mark.asyncio
    async def test_dynamodb_exception_returns_empty(
        self, aws_mocked, monkeypatch, caplog
    ):
        """Graceful degradation: a query failure shouldn't crash the
        tool. Points the helper at a table that doesn't exist *within
        moto* so the failure mode is the production-realistic
        ``ResourceNotFoundException`` rather than a credentials error
        (which would mask a real graceful-degradation regression).
        """
        import logging

        monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "nonexistent-table")
        with caplog.at_level(logging.ERROR):
            files = await _get_kb_files("ast-1")
        assert files == []
        # Verify we actually hit the exception branch — passing solely
        # because the early-return fired would be a silent regression
        # of the graceful-degradation contract the next refactor
        # (#260) needs to preserve.
        assert any(
            "ResourceNotFoundException" in record.getMessage()
            or "not found" in record.getMessage().lower()
            for record in caplog.records
        ), f"expected error log, got: {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_legacy_snake_case_fields_supported(self, assistants_table, seed_kb_doc):
        """The repo stores camelCase but some legacy items use snake_case
        aliases. Both must work.
        """
        seed_kb_doc(
            assistant_id="ast-1",
            filename="legacy.xlsx",
            content_type=XLSX_MIME,
            size_bytes=500,
            use_snake_case=True,
        )
        files = await _get_kb_files("ast-1")
        assert len(files) == 1
        assert files[0]["filename"] == "legacy.xlsx"
        assert files[0]["size_bytes"] == 500


# ---------------------------------------------------------------------------
# _get_session_files — async repo, via moto
# ---------------------------------------------------------------------------


class TestGetSessionFiles:
    """Real repository queries against the moto-backed files table.

    After #260, ``_get_session_files`` awaits the repository directly
    instead of running ``asyncio.run`` inside a thread-pool. These
    tests exercise the straightened-out async path end-to-end.
    """

    @pytest.mark.asyncio
    async def test_returns_tabular_files_only(
        self, file_repository, seed_session_file
    ):
        await seed_session_file(
            session_id="s1", upload_id="u-xlsx",
            filename="Budget.xlsx", mime_type=XLSX_MIME,
        )
        await seed_session_file(
            session_id="s1", upload_id="u-md",
            filename="README.md", mime_type="text/markdown",
        )
        await seed_session_file(
            session_id="s1", upload_id="u-csv",
            filename="data.csv", mime_type="text/csv",
        )

        files = await _get_session_files("s1")
        filenames = {f["filename"] for f in files}
        assert filenames == {"Budget.xlsx", "data.csv"}
        assert "README.md" not in filenames

    @pytest.mark.asyncio
    async def test_empty_session_returns_empty(self, file_repository):
        # No files seeded — list_session_files returns [].
        assert await _get_session_files("s1") == []

    @pytest.mark.asyncio
    async def test_missing_table_env_returns_empty(self, aws_mocked, monkeypatch, caplog):
        """Pointing the repo at a table that doesn't exist exercises the
        exception path inside the async helper. Tool should return an
        empty list, not crash.

        Uses ``caplog`` to confirm the error was actually logged —
        otherwise this test could regress silently if a future refactor
        made the helper return ``[]`` without ever reaching the
        exception branch.
        """
        import logging

        # Reset the module-level singleton so the new env var is picked
        # up on the next ``get_file_upload_repository()`` call — otherwise
        # we inherit the repo bound to whatever table name another test
        # happened to set first.
        import apis.shared.files.repository as repo_module
        monkeypatch.setattr(repo_module, "_repository_instance", None)
        monkeypatch.setenv("DYNAMODB_USER_FILES_TABLE_NAME", "no-such-table")

        with caplog.at_level(logging.ERROR):
            files = await _get_session_files("s1")
        assert files == []
        assert any(
            "ResourceNotFoundException" in record.getMessage()
            or "not found" in record.getMessage().lower()
            for record in caplog.records
        ), f"expected error log, got: {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_record_structure(
        self, file_repository, seed_session_file
    ):
        """Session records need specific keys so analyze_tool._download_file
        can find the S3 bucket/key. Lock the contract.
        """
        await seed_session_file(
            session_id="s1", upload_id="u-1",
            filename="Q1.csv", mime_type="text/csv",
        )
        files = await _get_session_files("s1")
        assert files[0].keys() >= {
            "filename", "source", "content_type", "size_bytes",
            "document_id", "s3_key", "s3_bucket",
        }
        assert files[0]["source"] == "chat_attachment"

    @pytest.mark.asyncio
    async def test_isolates_by_session_id(
        self, file_repository, seed_session_file
    ):
        """The session index must partition: a file attached to session
        A should not appear in session B's list.
        """
        await seed_session_file(
            session_id="s1", upload_id="u-a",
            filename="a.csv", mime_type="text/csv",
        )
        await seed_session_file(
            session_id="s2", upload_id="u-b",
            filename="b.csv", mime_type="text/csv",
        )

        files = await _get_session_files("s1")
        assert [f["filename"] for f in files] == ["a.csv"]
