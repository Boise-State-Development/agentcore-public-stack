"""Integration-style tests for the analyze_spreadsheet tool, exercising
the full factory → file lookup → download → CodeInterpreter → response
path with every external dependency mocked.

Covers the behaviors the issue (#261) specifically called out as "subtle
logic worth pinning down":

- CSV fast-path (no bootstrap, direct writeFiles + schema probe + user code)
- XLSX bootstrap: base64 push, sheet inventory round-trip, CSV rename
- Single-sheet vs. multi-sheet response shape
- Filename alias fallback (foo.csv ↔ foo.xlsx) via ``_find_file``
- Error-path hints: wrong-filename retry, schema-footer attached
- Truncation warnings when sheets hit MAX_ROWS_PER_SHEET
- Skipped-sheet warning when workbook exceeds MAX_SHEETS_TO_CONVERT
- Missing Code Interpreter → friendly error, no interpreter calls
- File not found → friendly error with list_spreadsheets hint
- S3 download failure → friendly error, interpreter still stopped

S3 and DynamoDB go through moto so tests exercise the real boto3 call
paths. Only the CodeInterpreter is hand-mocked (no moto equivalent for
AgentCore). All tests run offline; no AWS credentials required and the
backend env doesn't need pandas installed.
"""

from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Happy path: CSV end-to-end
# ---------------------------------------------------------------------------


class TestCsvHappyPath:
    def test_csv_end_to_end_success(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])
        seed_s3_object(key="sessions/data.csv", body=b"col1,col2\n1,2\n")

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="data.csv"),
            user_out="Total: 42\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="data.csv",
                python_code="print('Total: 42')",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "Total: 42" in text
        # Schema footer attached to success responses.
        assert "Dataset" in text
        assert "data.csv" in text
        # No XLSX bootstrap: one writeFiles (raw CSV) + two executeCode
        # calls (schema probe, user code).
        assert fake.started and fake.stopped
        write_calls = [r for r in fake.invocations if r.name == "writeFiles"]
        exec_calls = [r for r in fake.invocations if r.name == "executeCode"]
        assert len(write_calls) == 1
        assert len(exec_calls) == 2

    def test_csv_writes_raw_text_to_sandbox(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """CSV fast-path pushes the raw text directly — no base64, no
        bootstrap. Regression guard against a future "always run the
        bootstrap" refactor.
        """
        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])
        seed_s3_object(key="sessions/data.csv", body=b"col1,col2\n1,2\n")

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="data.csv"),
            user_out="done\n",
        )

        with ci_patch:
            call_analyze(
                filename="data.csv",
                python_code="print('done')",
                session_id="s1",
                user_id="u1",
            )

        write_call = next(r for r in fake.invocations if r.name == "writeFiles")
        files = write_call.payload["content"]
        assert len(files) == 1
        # Pushed as text, not base64.
        assert files[0]["path"] == "data.csv"
        assert files[0]["text"] == "col1,col2\n1,2\n"


# ---------------------------------------------------------------------------
# XLSX happy path — single-sheet and multi-sheet
# ---------------------------------------------------------------------------


XLSX_BYTES = b"\x50\x4b\x03\x04" + b"fake xlsx binary"  # PK... magic + payload


class TestXlsxSingleSheet:
    def test_single_sheet_xlsx_success(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Budget.xlsx")])
        seed_s3_object(key="sessions/Budget.xlsx", body=XLSX_BYTES)

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=1,
                sheets=[("Sheet1", "Budget.csv", 100, False, "")],
            ),
            schema_out=schema_stdout(file="Budget.csv", rows=100),
            user_out="sum=9999\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="Budget.xlsx",
                python_code="print('sum=9999')",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "sum=9999" in text
        # Single-sheet path does not emit the multi-sheet inventory.
        assert "Available sheets" not in text

    def test_xlsx_bootstrap_pushes_base64_blob(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Budget.xlsx")])
        seed_s3_object(key="sessions/Budget.xlsx", body=b"xlsx-binary-bytes")

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=1,
                sheets=[("Sheet1", "Budget.csv", 10, False, "")],
            ),
            schema_out=schema_stdout(file="Budget.csv"),
            user_out="",
        )

        with ci_patch:
            call_analyze(
                filename="Budget.xlsx",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        write_call = next(r for r in fake.invocations if r.name == "writeFiles")
        # Encoded blob written as text under _encoded.b64.
        entries = write_call.payload["content"]
        assert any(e["path"] == "_encoded.b64" for e in entries)


class TestXlsxMultiSheet:
    def test_multi_sheet_response_includes_inventory(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Budget.xlsx")])
        seed_s3_object(key="sessions/Budget.xlsx", body=XLSX_BYTES)

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=3,
                sheets=[
                    ("Summary", "Budget.summary.csv", 12, False, "Budget.csv"),
                    ("Transactions", "Budget.transactions.csv", 18_551, False, ""),
                    ("Notes", "Budget.notes.csv", 5, False, ""),
                ],
            ),
            schema_out=schema_stdout(file="Budget.csv"),
            user_out="analyzed\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="Budget.xlsx",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        text = result["content"][0]["text"]
        assert "Available sheets" in text
        assert "Summary" in text
        assert "Transactions" in text
        assert "Notes" in text
        assert "Budget.summary.csv" in text
        # Row counts are formatted with commas for readability.
        assert "18,551" in text

    def test_skipped_sheets_warning_surfaces(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Many.xlsx")])
        seed_s3_object(key="sessions/Many.xlsx", body=XLSX_BYTES)

        fake, ci_patch = fake_code_interpreter
        sheets = [
            (f"S{i}", f"Many.s{i}.csv", 10, False, "" if i > 1 else "Many.csv")
            for i in range(1, 26)
        ]
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=30,
                sheets=sheets,
                skipped_names=["S26", "S27", "S28", "S29", "S30"],
            ),
            schema_out=schema_stdout(file="Many.csv"),
            user_out="ok\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="Many.xlsx",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        text = result["content"][0]["text"]
        assert "30 sheets" in text
        assert "first 25" in text
        assert "S26" in text
        assert "S30" in text

    def test_truncated_sheet_warning_surfaces(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        """A sheet truncated at MAX_ROWS_PER_SHEET must be flagged in the
        inventory list so the user knows the analysis may be partial.
        """
        from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
            MAX_ROWS_PER_SHEET,
        )

        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Huge.xlsx")])
        seed_s3_object(key="sessions/Huge.xlsx", body=XLSX_BYTES)

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=2,
                sheets=[
                    ("BigSheet", "Huge.bigsheet.csv",
                     MAX_ROWS_PER_SHEET, True, "Huge.csv"),
                    ("SmallSheet", "Huge.smallsheet.csv", 10, False, ""),
                ],
            ),
            schema_out=schema_stdout(file="Huge.csv"),
            user_out="done\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="Huge.xlsx",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        text = result["content"][0]["text"]
        # Truncation tag for the big sheet; none for the small one.
        big_line = next(line for line in text.splitlines() if "BigSheet" in line)
        small_line = next(line for line in text.splitlines() if "SmallSheet" in line)
        assert "truncated" in big_line.lower()
        assert "truncated" not in small_line.lower()


# ---------------------------------------------------------------------------
# Filename aliasing
# ---------------------------------------------------------------------------


class TestFilenameAliasing:
    def test_csv_request_resolves_xlsx_source(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        """Model asks for ``Budget.csv`` (sandbox filename) when only
        ``Budget.xlsx`` was uploaded. ``_find_file`` aliases to the XLSX
        source; end-to-end the tool should still succeed.
        """
        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Budget.xlsx")])
        seed_s3_object(key="sessions/Budget.xlsx", body=XLSX_BYTES)

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=1,
                sheets=[("Sheet1", "Budget.csv", 10, False, "")],
            ),
            schema_out=schema_stdout(file="Budget.csv"),
            user_out="ok\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="Budget.csv",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "success"


class TestKnowledgeBaseDownload:
    def test_kb_source_downloads_from_kb_bucket(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        kb_bucket,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """KB-sourced files resolve their bucket from the
        ``S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME`` env var (set by the
        ``kb_bucket`` fixture) rather than from the file record's
        ``s3_bucket`` field. Covers the knowledge_base branch of
        ``_download_file`` which the session-attachment tests never
        exercise.
        """
        import boto3

        from tests.agents.builtin_tools.spreadsheet_analysis.conftest import (
            AWS_REGION,
        )

        # Seed the KB bucket directly (the kb_xlsx factory points s3_key
        # at assistants/ast-1/..., which is what _download_file reads).
        kb_file = file_factories["kb_xlsx"]("Ledger.csv")
        kb_file["content_type"] = "text/csv"  # simpler path — no XLSX bootstrap
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(
            Bucket=kb_bucket,
            Key=kb_file["s3_key"],
            Body=b"a,b,c\n1,2,3\n",
        )

        set_kb, set_session = file_sources
        set_kb([kb_file])
        set_session([])

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="Ledger.csv"),
            user_out="kb-analyzed\n",
        )

        with ci_patch:
            result = call_analyze(
                filename="Ledger.csv",
                python_code="pass",
                assistant_id="ast-1",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "success"
        assert "kb-analyzed" in result["content"][0]["text"]

    def test_kb_source_missing_env_var_surfaces_friendly_error(
        self,
        call_analyze,
        file_sources,
        file_factories,
        aws_mocked,
        monkeypatch,
        code_interpreter_id,
    ):
        """``_download_file`` raises ``ValueError`` when a KB file has no
        resolvable bucket. Tool wraps that in a graceful error rather
        than propagating the exception.
        """
        monkeypatch.delenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", raising=False)

        kb_file = file_factories["kb_xlsx"]("Ledger.csv")
        kb_file["content_type"] = "text/csv"

        set_kb, set_session = file_sources
        set_kb([kb_file])
        set_session([])

        result = call_analyze(
            filename="Ledger.csv",
            python_code="pass",
            assistant_id="ast-1",
            session_id="s1",
            user_id="u1",
        )

        assert result["status"] == "error"
        assert "Failed to download" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestFileNotFound:
    def test_unknown_file_returns_list_spreadsheets_hint(
        self,
        call_analyze,
        file_sources,
        code_interpreter_id,
    ):
        set_kb, set_session = file_sources
        set_session([])

        # No Code Interpreter patching because we never get that far.
        result = call_analyze(
            filename="missing.csv",
            python_code="print(1)",
            session_id="s1",
            user_id="u1",
        )
        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "not found" in text
        assert "list_spreadsheets" in text


class TestS3DownloadFailure:
    def test_s3_error_surfaces_friendly_message(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        code_interpreter_id,
    ):
        """File metadata points at a key that doesn't exist in the bucket.
        moto returns a NoSuchKey ClientError, which ``_download_file``
        wraps in a friendly message rather than crashing.
        """
        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])
        # Note: no seed_s3_object — the object doesn't exist, so
        # get_object raises.

        fake, ci_patch = fake_code_interpreter
        with ci_patch:
            result = call_analyze(
                filename="data.csv",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "error"
        assert "Failed to download" in result["content"][0]["text"]
        # The interpreter should never have been started for a download
        # failure — start() happens after _download_file succeeds.
        assert not fake.started


class TestCodeInterpreterUnavailable:
    def test_no_ci_id_returns_friendly_error(
        self,
        call_analyze,
        file_sources,
        file_factories,
        monkeypatch,
    ):
        """When ``_get_code_interpreter_id`` resolves to None (env unset,
        SSM lookup fails), the tool bails out with a contact-admin
        message instead of crashing.
        """
        monkeypatch.delenv("AGENTCORE_CODE_INTERPRETER_ID", raising=False)

        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])

        with patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value=None,
        ):
            result = call_analyze(
                filename="data.csv",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "error"
        assert "Code Interpreter is not configured" in result["content"][0]["text"]


class TestUserCodeError:
    def test_wrong_xlsx_filename_injects_hint(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        bootstrap_stdout,
        schema_stdout,
        reply_factory,
    ):
        """Classic failure: model wrote ``pd.read_csv('Budget.xlsx', ...)``
        but the sandbox has ``Budget.csv``. Error response must include
        the targeted retry hint naming the correct filename, not just
        dump the FileNotFoundError.
        """
        set_kb, set_session = file_sources
        set_session([file_factories["session_xlsx"]("Budget.xlsx")])
        seed_s3_object(key="sessions/Budget.xlsx", body=XLSX_BYTES)

        err_traceback = (
            "Traceback (most recent call last):\n"
            "  File \"/tmp/ipykernel_1/code.py\", line 1, in <module>\n"
            "    df = pd.read_csv('Budget.xlsx', low_memory=False)\n"
            "FileNotFoundError: [Errno 2] No such file or directory: "
            "'Budget.xlsx'\n"
        )

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            bootstrap_out=bootstrap_stdout(
                total=1,
                sheets=[("Sheet1", "Budget.csv", 10, False, "")],
            ),
            schema_out=schema_stdout(file="Budget.csv"),
            user_out="",
            user_err=err_traceback,
            user_is_error=True,
        )

        with ci_patch:
            result = call_analyze(
                filename="Budget.xlsx",
                python_code="df = pd.read_csv('Budget.xlsx')",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "FileNotFoundError" in text
        # The retry hint names the sandbox filename explicitly.
        assert "loaded as" in text
        assert "Budget.csv" in text
        # Schema footer should also be attached so the retry has the
        # load line.
        assert "Dataset info" in text or "use the `load:` line" in text

    def test_generic_user_error_attaches_schema(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """A KeyError on a CSV — no XLSX hint needed, but the schema
        footer with column list should land so the model can fix its
        column reference on retry.
        """
        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])
        seed_s3_object(key="sessions/data.csv", body=b"a,b,c\n1,2,3\n")

        err_traceback = (
            "Traceback (most recent call last):\n"
            "  File \"/tmp/ipykernel_1/code.py\", line 1, in <module>\n"
            "    print(df['WRONG_COL'].sum())\n"
            "KeyError: 'WRONG_COL'\n"
        )

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="data.csv", columns="a, b, c"),
            user_out="",
            user_err=err_traceback,
            user_is_error=True,
        )

        with ci_patch:
            result = call_analyze(
                filename="data.csv",
                python_code="print(df['WRONG_COL'].sum())",
                session_id="s1",
                user_id="u1",
            )

        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "KeyError" in text
        assert "Dataset info" in text
        assert "columns: a, b, c" in text
        # The xlsx hint must NOT appear on a CSV error.
        assert "loaded as" not in text


class TestInterpreterLifecycle:
    def test_interpreter_stopped_on_success(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])
        seed_s3_object(key="sessions/data.csv", body=b"a,b\n1,2\n")

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="data.csv"),
            user_out="done\n",
        )

        with ci_patch:
            call_analyze(
                filename="data.csv",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        assert fake.started
        assert fake.stopped

    def test_interpreter_stopped_on_user_error(
        self,
        call_analyze,
        file_sources,
        file_factories,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """The finally: stop() must run even when user code fails.
        Otherwise we'd leak interpreter sessions on every bad query.
        """
        set_kb, set_session = file_sources
        set_session([file_factories["session_csv"]("data.csv")])
        seed_s3_object(key="sessions/data.csv", body=b"a,b\n1,2\n")

        fake, ci_patch = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="data.csv"),
            user_out="",
            user_err="KeyError: 'x'\n",
            user_is_error=True,
        )

        with ci_patch:
            call_analyze(
                filename="data.csv",
                python_code="pass",
                session_id="s1",
                user_id="u1",
            )

        assert fake.stopped
