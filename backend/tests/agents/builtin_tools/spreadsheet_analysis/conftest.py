"""Shared fixtures for spreadsheet_analysis unit tests.

Central place to assemble the stack of mocks the analyze_spreadsheet tool
requires: the Code Interpreter client, the S3 client, and the file
resolution helpers (_get_kb_files / _get_session_files / _find_file).

Each fixture is small and composable so individual tests can swap in
exactly the behavior they want to assert on.

S3 and DynamoDB are handled with moto (see ``tests/shared/conftest.py``)
so that tests exercise real boto3 call paths rather than ad-hoc mocks.
The Code Interpreter client has no moto equivalent — it's an AgentCore
service — so ``FakeCodeInterpreter`` below is a hand-rolled stand-in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


# ---------------------------------------------------------------------------
# AWS mocks (moto) — S3 + DynamoDB tables
# ---------------------------------------------------------------------------


AWS_REGION = "us-east-1"
SESSIONS_BUCKET = "test-sessions-bucket"
KB_BUCKET = "test-kb-bucket"


@pytest.fixture
def aws_mocked(monkeypatch):
    """Activate moto's ``mock_aws`` for the duration of the test.

    Sets the minimum env vars boto3 clients expect. Any S3 / DynamoDB
    calls made by analyze_tool._download_file or _get_kb_files during
    the test execute against moto's in-process fakes, not real AWS.

    ``AWS_REGION`` is set alongside ``AWS_DEFAULT_REGION`` because some
    helpers (``_get_kb_files``, ``_download_file``) read ``AWS_REGION``
    explicitly and fall back to ``us-west-2`` — which would land on a
    different moto region than the fixtures use.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture
def sessions_bucket(aws_mocked):
    """Create the session-attachments S3 bucket. Tests push real objects
    in and analyze_tool downloads them through real boto3 calls.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.create_bucket(Bucket=SESSIONS_BUCKET)
    return SESSIONS_BUCKET


@pytest.fixture
def kb_bucket(aws_mocked, monkeypatch):
    """Create the assistant-KB S3 bucket and point the env var at it so
    ``_download_file`` can resolve the bucket for KB-source files.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.create_bucket(Bucket=KB_BUCKET)
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", KB_BUCKET)
    return KB_BUCKET


@pytest.fixture
def assistants_table(aws_mocked, monkeypatch):
    """Create the DynamoDB assistants table with the schema
    ``_get_kb_files`` queries against. Tests can ``put_item`` real
    document records and see them flow through the filter.
    """
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-assistants"
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", name)
    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture
def files_table(aws_mocked, monkeypatch):
    """Create the user-files DynamoDB table with the SessionIndex GSI
    that ``FileUploadRepository.list_session_files`` queries.
    """
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-user-files"
    monkeypatch.setenv("DYNAMODB_USER_FILES_TABLE_NAME", name)
    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "SessionIndex",
            "KeySchema": [
                {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture
def file_repository(files_table):
    """A real ``FileUploadRepository`` pointed at the moto-backed table."""
    from apis.shared.files.repository import FileUploadRepository

    return FileUploadRepository(table_name="test-user-files")


# ---------------------------------------------------------------------------
# Seed helpers — write KB docs / session files into moto-backed stores
# ---------------------------------------------------------------------------


def put_kb_doc(
    table,
    *,
    assistant_id: str,
    filename: str,
    content_type: str,
    status: str = "complete",
    size_bytes: int = 1024,
    document_id: str | None = None,
    s3_key: str | None = None,
    use_snake_case: bool = False,
) -> None:
    """Write a completed (or failed) KB document row to the assistants
    table in the shape ``_get_kb_files`` queries.

    ``use_snake_case`` lets tests pin the legacy field-name behavior —
    some older items store ``content_type`` / ``size_bytes`` / ``s3_key``
    / ``document_id`` instead of the camelCase defaults.
    """
    doc_id = document_id or f"doc-{filename}"
    key = s3_key or f"assistants/{assistant_id}/{filename}"
    item = {
        "PK": f"AST#{assistant_id}",
        "SK": f"DOC#{doc_id}",
        "status": status,
        "filename": filename,
    }
    if use_snake_case:
        item.update({
            "content_type": content_type,
            "size_bytes": size_bytes,
            "document_id": doc_id,
            "s3_key": key,
        })
    else:
        item.update({
            "contentType": content_type,
            "sizeBytes": size_bytes,
            "documentId": doc_id,
            "s3Key": key,
        })
    table.put_item(Item=item)


async def put_session_file(
    file_repository,
    *,
    session_id: str,
    user_id: str = "u1",
    upload_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int = 1024,
    s3_bucket: str = SESSIONS_BUCKET,
    s3_key: str | None = None,
) -> None:
    """Create a READY file record in the files repository so
    ``FileUploadRepository.list_session_files`` returns it.
    """
    from apis.shared.files.models import FileMetadata, FileStatus

    key = s3_key or f"sessions/{session_id}/{filename}"
    await file_repository.create_file(FileMetadata(
        upload_id=upload_id,
        user_id=user_id,
        session_id=session_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        s3_key=key,
        s3_bucket=s3_bucket,
        status=FileStatus.READY,
    ))


@pytest.fixture
def seed_kb_doc(assistants_table):
    """Tiny helper so tests read like ``seed_kb_doc(filename=..., ...)``
    without threading the table fixture through every call site.
    """
    def _seed(**kwargs):
        put_kb_doc(assistants_table, **kwargs)
    return _seed


@pytest.fixture
def seed_session_file(file_repository):
    """Async-aware helper; tests should ``await seed_session_file(...)``."""
    async def _seed(**kwargs):
        await put_session_file(file_repository, **kwargs)
    return _seed


# ---------------------------------------------------------------------------
# Fake CodeInterpreter
# ---------------------------------------------------------------------------


@dataclass
class InvocationRecord:
    """One call to the fake CodeInterpreter's ``invoke`` method."""

    name: str
    payload: dict


@dataclass
class FakeCodeInterpreter:
    """Drop-in stand-in for bedrock_agentcore's CodeInterpreter client.

    Tests can:
    - install a ``reply_for`` callback that returns the canned stream
      response for a given (invocation_name, payload) pair; or
    - rely on the default empty-success behavior (``executeCode`` returns
      an empty stdout non-error stream; ``writeFiles`` / ``readFiles``
      return empty streams).

    The ``invocations`` list preserves call order so tests can assert on
    the full sequence, not just the last call.
    """

    reply_for: Callable[[str, dict], dict] | None = None
    invocations: list[InvocationRecord] = field(default_factory=list)
    started: bool = False
    stopped: bool = False

    # Inputs the test doesn't care about — bedrock_agentcore exposes these
    # as construction / lifecycle hooks. We keep no-op stubs.
    def __init__(self, *_args, reply_for=None, **_kwargs):
        self.reply_for = reply_for
        self.invocations = []
        self.started = False
        self.stopped = False

    def start(self, identifier: str) -> None:  # noqa: D401 — mock signature
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def invoke(self, name: str, payload: dict) -> dict:
        self.invocations.append(InvocationRecord(name=name, payload=payload))
        if self.reply_for is not None:
            return self.reply_for(name, payload)
        # Default: empty successful response.
        return {"stream": [{"result": {"isError": False, "structuredContent": {"stdout": ""}}}]}

    # --- Handy helpers for test assertions ---

    def bootstrap_payload(self) -> str | None:
        """Return the code string passed to the first executeCode call
        (the XLSX bootstrap), or None if nothing was executed.
        """
        for rec in self.invocations:
            if rec.name == "executeCode":
                return rec.payload.get("code")
        return None

    def executed_codes(self) -> list[str]:
        return [r.payload.get("code", "") for r in self.invocations if r.name == "executeCode"]


def _stream_response(stdout: str = "", *, is_error: bool = False, stderr: str = "") -> dict:
    """Build a minimally valid stream response from CodeInterpreter."""
    return {
        "stream": [
            {
                "result": {
                    "isError": is_error,
                    "structuredContent": {"stdout": stdout, "stderr": stderr},
                }
            }
        ]
    }


@pytest.fixture
def fake_code_interpreter():
    """Return a FakeCodeInterpreter instance + a patch context that
    substitutes it for the real client used by analyze_tool.

    Usage:
        def test_it(fake_code_interpreter):
            fake, patcher = fake_code_interpreter
            with patcher:
                ...
    """
    fake = FakeCodeInterpreter()

    def _factory(*_args, **_kwargs):
        return fake

    patcher = patch(
        "bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter",
        side_effect=_factory,
    )
    return fake, patcher


# ---------------------------------------------------------------------------
# S3 object helpers (moto-backed)
# ---------------------------------------------------------------------------


def put_s3_object(bucket: str, key: str, body: bytes) -> None:
    """Push a real object into a moto-backed bucket."""
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=bucket, Key=key, Body=body)


@pytest.fixture
def seed_s3_object(sessions_bucket):
    """Drop an object into the sessions bucket. ``analyze_tool._download_file``
    will pick it up via real boto3 through moto's interceptor.
    """
    def _seed(key: str, body: bytes = b"fake bytes", bucket: str = SESSIONS_BUCKET):
        put_s3_object(bucket, key, body)
    return _seed


# ---------------------------------------------------------------------------
# File sources (KB + session)
# ---------------------------------------------------------------------------


@pytest.fixture
def file_sources():
    """Patch ``_get_kb_files`` and ``_get_session_files`` together.

    Patches both modules that import these helpers — analyze_tool
    (for _find_file and the tabular inventory) and list_spreadsheets_tool
    (for the tool factory's direct calls). Tests can configure both
    sides cleanly:

        def test_it(file_sources):
            set_kb, set_session = file_sources
            set_session([{...}])

    The helpers are ``async def`` (see #260 — sync boto3 was blocking
    the event loop), so the patches install async side-effects. Returning
    a plain list from an ``async def`` gives the callers an awaitable
    they can ``await`` exactly like the real helpers.
    """
    kb_files: list[dict[str, Any]] = []
    session_files: list[dict[str, Any]] = []

    def set_kb(files):
        kb_files[:] = list(files)

    def set_session(files):
        session_files[:] = list(files)

    async def _kb_side_effect(_aid):
        return list(kb_files)

    async def _session_side_effect(_sid):
        return list(session_files)

    patchers = [
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_kb_files",
            side_effect=_kb_side_effect,
        ),
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_session_files",
            side_effect=_session_side_effect,
        ),
        patch(
            "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_kb_files",
            side_effect=_kb_side_effect,
        ),
        patch(
            "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool._get_session_files",
            side_effect=_session_side_effect,
        ),
    ]
    for p in patchers:
        p.start()
    try:
        yield set_kb, set_session
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture
def code_interpreter_id(monkeypatch):
    """Set a sentinel Code Interpreter id so ``_get_code_interpreter_id``
    short-circuits to the env branch (avoiding the SSM fallback).
    """
    monkeypatch.setenv("AGENTCORE_CODE_INTERPRETER_ID", "ci-test-123")


# ---------------------------------------------------------------------------
# Canned file records
# ---------------------------------------------------------------------------


def make_session_csv(filename: str = "data.csv", size: int = 1024) -> dict:
    return {
        "filename": filename,
        "source": "chat_attachment",
        "content_type": "text/csv",
        "size_bytes": size,
        "document_id": f"upload-{filename}",
        "s3_key": f"sessions/{filename}",
        "s3_bucket": SESSIONS_BUCKET,
    }


def make_session_xlsx(filename: str = "workbook.xlsx", size: int = 1024 * 500) -> dict:
    return {
        "filename": filename,
        "source": "chat_attachment",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size_bytes": size,
        "document_id": f"upload-{filename}",
        "s3_key": f"sessions/{filename}",
        "s3_bucket": SESSIONS_BUCKET,
    }


def make_kb_xlsx(filename: str = "kb_workbook.xlsx", size: int = 1024 * 200) -> dict:
    return {
        "filename": filename,
        "source": "knowledge_base",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size_bytes": size,
        "document_id": f"doc-{filename}",
        "s3_key": f"assistants/ast-1/{filename}",
    }


@pytest.fixture
def file_factories():
    """Expose the canned-file helpers to tests without an import dance."""
    return {
        "session_csv": make_session_csv,
        "session_xlsx": make_session_xlsx,
        "kb_xlsx": make_kb_xlsx,
    }


# ---------------------------------------------------------------------------
# Tool invocation helper
# ---------------------------------------------------------------------------


def _unwrap(tool_obj: Any) -> Callable[..., Any]:
    """Strands' @tool wraps the original function; our tests call the raw
    function so we bypass framework marshalling.
    """
    return getattr(tool_obj, "__wrapped__", None) or tool_obj


@pytest.fixture
def call_analyze():
    """Shortcut for ``unwrap(tool)(**kwargs)`` — builds the analyze tool
    via the factory and invokes it in one go.

    ``analyze_spreadsheet`` is ``async def`` (see #260) so we run the
    returned coroutine to completion here; tests stay sync and assert
    on the resolved result, keeping the call-site unchanged from the
    pre-refactor shape.

        def test_it(call_analyze, ...):
            result = call_analyze(
                filename="x.csv",
                python_code="print(1)",
                assistant_id=None, session_id="s1", user_id="u1",
            )
    """
    from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
        make_analyze_tool,
    )

    def _call(*, filename, python_code, output_filename=None,
              assistant_id=None, session_id="s1", user_id="u1"):
        tool = make_analyze_tool(assistant_id, session_id, user_id)
        fn = _unwrap(tool)
        return asyncio.run(fn(filename=filename, python_code=python_code,
                              output_filename=output_filename))

    return _call


# ---------------------------------------------------------------------------
# Bootstrap stdout builder (multi-sheet / single-sheet)
# ---------------------------------------------------------------------------


def build_bootstrap_stdout(
    *,
    total: int,
    sheets: list[tuple[str, str, int, bool, str]],
    skipped_names: list[str] | None = None,
) -> str:
    """Build the stdout the XLSX bootstrap emits inside its ``[__SHEETS__]``
    block.

    Each ``sheets`` entry is ``(name, path, rows, truncated, alias)``.
    The function assembles the block in the exact shape the real
    bootstrap writes, so ``_parse_sheet_inventory`` can round-trip it.
    """
    from agents.builtin_tools.spreadsheet_analysis.analyze_tool import _SHEETS_MARKER

    lines = [
        _SHEETS_MARKER,
        f"total: {total}",
        f"converted: {len(sheets)}",
        f"skipped: {total - len(sheets)}",
    ]
    if skipped_names:
        lines.append(f"skipped_names: {skipped_names!r}")
    for name, path, rows, truncated, alias in sheets:
        flag = "1" if truncated else "0"
        lines.append(f"sheet|{name}|{path}|{rows}|{flag}|{alias}")
    lines.append(_SHEETS_MARKER)
    return "\n".join(lines) + "\n"


@pytest.fixture
def bootstrap_stdout():
    """Expose ``build_bootstrap_stdout`` to tests."""
    return build_bootstrap_stdout


# ---------------------------------------------------------------------------
# Schema-preview stdout builder
# ---------------------------------------------------------------------------


def build_schema_stdout(
    *,
    file: str,
    rows: int = 100,
    cols: int = 3,
    load: str | None = None,
    columns: str = "a, b, c",
    first_row: str = "{'a': 1, 'b': 2, 'c': 3}",
) -> str:
    """Build the stdout the schema-preview probe emits inside its
    ``[__SCHEMA__]`` block.
    """
    from agents.builtin_tools.spreadsheet_analysis.analyze_tool import _SCHEMA_MARKER

    load_line = load or f"pd.read_csv('{file}', low_memory=False)"
    return "\n".join([
        _SCHEMA_MARKER,
        f"file: {file} ({rows} rows x {cols} cols)",
        f"load: {load_line}",
        f"columns: {columns}",
        f"first_row: {first_row}",
        _SCHEMA_MARKER,
    ]) + "\n"


@pytest.fixture
def schema_stdout():
    return build_schema_stdout


# ---------------------------------------------------------------------------
# Default stream reply dispatcher
# ---------------------------------------------------------------------------


def default_reply_factory(
    *,
    bootstrap_out: str = "",
    schema_out: str = "",
    user_out: str = "",
    user_err: str = "",
    user_is_error: bool = False,
) -> Callable[[str, dict], dict]:
    """Return a ``reply_for`` callback suitable for ``FakeCodeInterpreter``.

    Reads the invocation ordering the tool performs — ``writeFiles`` for
    the base64 blob / raw CSV, then executeCode for the bootstrap,
    executeCode for the schema probe, executeCode for the user code —
    and emits the matching stdout/stderr.

    Ignores ``readFiles`` (used for chart downloads) unless a caller
    explicitly overrides.
    """
    state = {"execute_calls": 0}

    def _reply(name: str, _payload: dict) -> dict:
        if name == "executeCode":
            state["execute_calls"] += 1
            # Order: 1) XLSX bootstrap (or none for CSV), 2) schema probe,
            # 3) user code. For CSV inputs, the bootstrap is skipped so
            # call #1 is schema, call #2 is user code.
            call_idx = state["execute_calls"]
            if bootstrap_out and call_idx == 1:
                return _stream_response(bootstrap_out)
            if bootstrap_out and call_idx == 2:
                return _stream_response(schema_out)
            if bootstrap_out and call_idx == 3:
                return _stream_response(user_out, is_error=user_is_error, stderr=user_err)
            # CSV path — no bootstrap.
            if not bootstrap_out and call_idx == 1:
                return _stream_response(schema_out)
            if not bootstrap_out and call_idx == 2:
                return _stream_response(user_out, is_error=user_is_error, stderr=user_err)
        return _stream_response()

    return _reply


@pytest.fixture
def reply_factory():
    return default_reply_factory
