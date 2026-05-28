"""Tests for the file-size guard in analyze_spreadsheet (issue #258, items 2+3).

Covers:
- Oversize files (> ANALYZE_MAX_FILE_SIZE_BYTES) are rejected before any
  S3 download or Code Interpreter call.
- Files between ANALYZE_WARN_FILE_SIZE_BYTES and the hard cap attach a soft
  warning to both success and error responses.
- Files with missing / zero size_bytes pass through without being blocked
  (regression guard for file sources that don't populate the field).
- Threshold overrides via module-level monkeypatching.
"""

from __future__ import annotations

import agents.builtin_tools.spreadsheet_analysis.analyze_tool as analyze_tool
from tests.agents.builtin_tools.spreadsheet_analysis.conftest import make_session_csv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_25MB = 25 * 1024 * 1024
_10MB = 10 * 1024 * 1024
_30MB = 30 * 1024 * 1024
_15MB = 15 * 1024 * 1024


# ---------------------------------------------------------------------------
# Test: oversize file rejected before download
# ---------------------------------------------------------------------------


class TestOversizeRejected:
    def test_oversize_file_returns_error_before_download(
        self,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        code_interpreter_id,
    ):
        """A file above ANALYZE_MAX_FILE_SIZE_BYTES must be rejected with a
        descriptive error. The Code Interpreter must never be started and
        S3 must not be read (we deliberately don't seed the object so any
        accidental GetObject call raises a NoSuchKey and would fail the test).
        """
        _, set_session = file_sources
        set_session([make_session_csv("huge.csv", size=_30MB)])

        fake, patcher = fake_code_interpreter
        with patcher:
            result = call_analyze(filename="huge.csv", python_code="print(1)")

        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "exceeds the analysis size limit" in text
        assert "huge.csv" in text
        # Size in MB should appear in both actual and limit
        assert "30.0 MB" in text
        assert "25.0 MB" in text
        # Code Interpreter must not have been touched
        assert not fake.started

    def test_oversize_error_mentions_remediation(
        self,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        code_interpreter_id,
    ):
        """The error message should suggest how to fix the problem."""
        _, set_session = file_sources
        set_session([make_session_csv("report.csv", size=_30MB)])

        fake, patcher = fake_code_interpreter
        with patcher:
            result = call_analyze(filename="report.csv", python_code="print(1)")

        text = result["content"][0]["text"]
        # The message should hint at filtering / sampling
        assert any(word in text.lower() for word in ("filter", "sample", "split"))


# ---------------------------------------------------------------------------
# Test: soft warning attached to success response
# ---------------------------------------------------------------------------


class TestSoftWarningOnSuccess:
    def test_warning_attached_to_success_response(
        self,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """Files between ANALYZE_WARN_FILE_SIZE_BYTES and the hard cap should
        proceed, but the success response must include the soft-warning text.
        """
        _, set_session = file_sources
        set_session([make_session_csv("medium.csv", size=_15MB)])
        seed_s3_object(key="sessions/medium.csv", body=b"col1,col2\n1,2\n")

        fake, patcher = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="medium.csv"),
            user_out="done",
        )
        with patcher:
            result = call_analyze(filename="medium.csv", python_code="print('done')")

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "analysis may be slow" in text or "may be slow" in text
        assert "medium.csv" in text


# ---------------------------------------------------------------------------
# Test: soft warning attached to error response
# ---------------------------------------------------------------------------


class TestSoftWarningOnError:
    def test_warning_attached_to_error_response(
        self,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """When user code raises an error, the soft warning should still
        appear in the error response text.
        """
        _, set_session = file_sources
        set_session([make_session_csv("medium.csv", size=_15MB)])
        seed_s3_object(key="sessions/medium.csv", body=b"col1,col2\n1,2\n")

        fake, patcher = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="medium.csv"),
            user_is_error=True,
            user_err="Traceback (most recent call last):\n  File '/tmp/ipykernel_1.py', line 1, in <module>\n    df['missing_col']\nKeyError: 'missing_col'",
        )
        with patcher:
            result = call_analyze(filename="medium.csv", python_code="df['missing_col']")

        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "analysis may be slow" in text or "may be slow" in text


# ---------------------------------------------------------------------------
# Test: missing size_bytes does not block the tool
# ---------------------------------------------------------------------------


class TestMissingSizeBytes:
    def test_missing_size_bytes_does_not_block(
        self,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """If size_bytes is absent or zero, the tool must not be blocked.
        This is a regression guard for file sources that don't populate
        the field (e.g. legacy KB records).
        """
        _, set_session = file_sources
        # Explicitly set size_bytes to 0 (missing / unpopulated)
        record = make_session_csv("legacy.csv", size=0)
        set_session([record])
        seed_s3_object(key="sessions/legacy.csv", body=b"a,b\n1,2\n")

        fake, patcher = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="legacy.csv"),
            user_out="ok",
        )
        with patcher:
            result = call_analyze(filename="legacy.csv", python_code="print('ok')")

        assert result["status"] == "success"
        # No soft-warning should appear for a zero-size file
        text = result["content"][0]["text"]
        assert "analysis may be slow" not in text

    def test_none_size_bytes_does_not_block(
        self,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """None size_bytes should be treated the same as zero — no block."""
        _, set_session = file_sources
        record = make_session_csv("no_size.csv")
        record["size_bytes"] = None
        set_session([record])
        seed_s3_object(key="sessions/no_size.csv", body=b"x\n1\n")

        fake, patcher = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="no_size.csv"),
            user_out="ok",
        )
        with patcher:
            result = call_analyze(filename="no_size.csv", python_code="print('ok')")

        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Test: threshold can be overridden via module attribute
# ---------------------------------------------------------------------------


class TestThresholdOverride:
    def test_lowered_max_threshold_rejects_smaller_file(
        self,
        monkeypatch,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        code_interpreter_id,
    ):
        """Patching ANALYZE_MAX_FILE_SIZE_BYTES on the module should cause
        a file that would normally pass to be rejected.
        """
        # Lower the hard cap to 1 KB
        monkeypatch.setattr(analyze_tool, "ANALYZE_MAX_FILE_SIZE_BYTES", 1024)

        _, set_session = file_sources
        # 2 KB — under the default 25 MB cap, but over our patched 1 KB cap
        set_session([make_session_csv("small_but_over.csv", size=2048)])

        fake, patcher = fake_code_interpreter
        with patcher:
            result = call_analyze(
                filename="small_but_over.csv", python_code="print(1)"
            )

        assert result["status"] == "error"
        assert "exceeds the analysis size limit" in result["content"][0]["text"]
        assert not fake.started

    def test_raised_warn_threshold_suppresses_warning(
        self,
        monkeypatch,
        call_analyze,
        file_sources,
        fake_code_interpreter,
        sessions_bucket,
        seed_s3_object,
        code_interpreter_id,
        schema_stdout,
        reply_factory,
    ):
        """Raising ANALYZE_WARN_FILE_SIZE_BYTES above the file size should
        suppress the soft warning even for a file that would normally trigger it.
        """
        # Raise the warn threshold above our 15 MB test file
        monkeypatch.setattr(analyze_tool, "ANALYZE_WARN_FILE_SIZE_BYTES", _30MB)

        _, set_session = file_sources
        set_session([make_session_csv("medium.csv", size=_15MB)])
        seed_s3_object(key="sessions/medium.csv", body=b"col1,col2\n1,2\n")

        fake, patcher = fake_code_interpreter
        fake.reply_for = reply_factory(
            schema_out=schema_stdout(file="medium.csv"),
            user_out="done",
        )
        with patcher:
            result = call_analyze(filename="medium.csv", python_code="print('done')")

        assert result["status"] == "success"
        text = result["content"][0]["text"]
        assert "analysis may be slow" not in text
