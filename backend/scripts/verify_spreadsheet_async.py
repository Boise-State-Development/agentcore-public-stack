#!/usr/bin/env python3
"""
Local verification script for the async fix in list_spreadsheets_tool (issue #260).

Run from the backend/ directory:
    python scripts/verify_spreadsheet_async.py

No AWS credentials required — all external calls are mocked.
Works on any plain Python 3.9+ install with zero extra dependencies.

Checks:
  1. list_spreadsheets @tool is a coroutine function (async def).
  2. _get_session_files directly awaits the repository (no ThreadPoolExecutor).
  3. _get_kb_files uses asyncio.to_thread (event loop stays free during DynamoDB call).
  4. End-to-end: the tool correctly lists files from both KB and session sources.
  5. Regression: running the tool concurrently does not deadlock or raise RuntimeError.
"""

import asyncio
import importlib.util
import inspect
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Resolve the path to the module under test
# ---------------------------------------------------------------------------
_BACKEND = Path(__file__).resolve().parent.parent        # backend/
_MODULE_PATH = (
    _BACKEND / "src" / "agents" / "builtin_tools"
    / "spreadsheet_analysis" / "list_spreadsheets_tool.py"
)
assert _MODULE_PATH.exists(), f"Cannot find module: {_MODULE_PATH}"

# ---------------------------------------------------------------------------
# Stub ALL external dependencies before loading the module so no __init__.py
# chain is triggered.
# ---------------------------------------------------------------------------


def _stub(name: str) -> types.ModuleType:
    return types.ModuleType(name)


def _identity(fn):  # stand-in for @tool
    return fn


# strands
_strands = _stub("strands")
_strands.tool = _identity  # type: ignore
sys.modules["strands"] = _strands

# boto3
_boto3 = _stub("boto3")
_boto3.resource = MagicMock()  # type: ignore
sys.modules["boto3"] = _boto3

# apis.*
for _pkg in ["apis", "apis.shared", "apis.shared.files"]:
    sys.modules.setdefault(_pkg, _stub(_pkg))

_models_stub = _stub("apis.shared.files.models")
_models_stub.is_tabular_file = lambda fn, mime: fn.endswith((".csv", ".xlsx"))  # type: ignore
sys.modules["apis.shared.files.models"] = _models_stub

_repo_stub = _stub("apis.shared.files.repository")
_repo_stub.get_file_upload_repository = MagicMock()  # type: ignore
sys.modules["apis.shared.files.repository"] = _repo_stub

# agents.* parent stubs — needed so the module name resolves consistently
for _pkg in ["agents", "agents.builtin_tools", "agents.builtin_tools.spreadsheet_analysis"]:
    sys.modules.setdefault(_pkg, _stub(_pkg))

# ---------------------------------------------------------------------------
# Load the module directly from its file path (bypasses agents/__init__.py)
# ---------------------------------------------------------------------------
_MOD_NAME = "agents.builtin_tools.spreadsheet_analysis.list_spreadsheets_tool"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _MODULE_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = _mod
_spec.loader.exec_module(_mod)  # type: ignore

# Convenience aliases
make_list_spreadsheets_tool = _mod.make_list_spreadsheets_tool
_get_kb_files               = _mod._get_kb_files
_get_session_files          = _mod._get_session_files

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
_failures: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


@dataclass
class _FakeFile:
    """Minimal stand-in for a FileMetadata object returned by the repository."""
    filename: str
    mime_type: str
    size_bytes: int
    upload_id: str
    s3_key: str
    s3_bucket: str


# ---------------------------------------------------------------------------
# Check 1 — tool is a coroutine function
# ---------------------------------------------------------------------------


def verify_tool_is_async() -> None:
    print("\n[1] list_spreadsheets @tool must be async")
    fn = make_list_spreadsheets_tool(assistant_id=None, session_id="s1", user_id="u1")
    check(
        "make_list_spreadsheets_tool() returns a coroutine function",
        inspect.iscoroutinefunction(fn),
        "tool is still sync — Strands cannot await it",
    )


# ---------------------------------------------------------------------------
# Check 2 — _get_session_files directly awaits the repository
# ---------------------------------------------------------------------------


async def verify_session_files_awaits_repo() -> None:
    print("\n[2] _get_session_files must directly await the async repository")

    csv_file = _FakeFile("sales.csv", "text/csv", 8192, "u1", "s/sales.csv", "bkt")
    mock_repo = MagicMock()
    mock_repo.list_session_files = AsyncMock(return_value=[csv_file])

    # _get_session_files does a lazy `from apis.shared.files.repository import ...`
    # so we inject directly into the already-registered stub module.
    _orig_factory = _repo_stub.get_file_upload_repository
    _orig_itf     = _models_stub.is_tabular_file
    _repo_stub.get_file_upload_repository = MagicMock(return_value=mock_repo)
    _models_stub.is_tabular_file = lambda fn, mime: True  # type: ignore

    try:
        files = await _get_session_files("session-1")
    finally:
        _repo_stub.get_file_upload_repository = _orig_factory
        _models_stub.is_tabular_file = _orig_itf

    check(
        "repo.list_session_files was awaited exactly once",
        mock_repo.list_session_files.await_count == 1,
        f"await_count={mock_repo.list_session_files.await_count}",
    )
    check("returned 1 tabular file", len(files) == 1, f"got {len(files)}")
    check("source is 'chat_attachment'", files[0]["source"] == "chat_attachment")


# ---------------------------------------------------------------------------
# Check 3 — _get_kb_files uses asyncio.to_thread
# ---------------------------------------------------------------------------


async def verify_kb_files_uses_to_thread() -> None:
    print("\n[3] _get_kb_files must use asyncio.to_thread for the boto3 call")

    calls: List[str] = []
    fake_items = [{
        "status": "complete", "filename": "ledger.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "sizeBytes": 4096, "documentId": "doc-1", "s3Key": "ast-1/ledger.xlsx",
    }]
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": fake_items}
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    # Swap boto3.resource on the stub
    _orig_resource   = _boto3.resource
    _boto3.resource  = MagicMock(return_value=mock_dynamodb)  # type: ignore

    # Spy on asyncio.to_thread via the module's own asyncio reference
    _mod_asyncio         = _mod.asyncio  # type: ignore
    _orig_to_thread      = _mod_asyncio.to_thread

    async def _spy(fn, *args, **kwargs):
        calls.append("to_thread")
        return fn(*args, **kwargs)

    _mod_asyncio.to_thread = _spy

    _orig_itf = _models_stub.is_tabular_file
    _models_stub.is_tabular_file = lambda fn, mime: True  # type: ignore

    _old_env = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"] = "AssistantsTable"

    try:
        files = await _get_kb_files("ast-1")
    finally:
        _boto3.resource              = _orig_resource      # type: ignore
        _mod_asyncio.to_thread       = _orig_to_thread
        _models_stub.is_tabular_file = _orig_itf
        if _old_env is None:
            os.environ.pop("DYNAMODB_ASSISTANTS_TABLE_NAME", None)
        else:
            os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"] = _old_env

    check(
        "asyncio.to_thread was called (blocking IO offloaded from event loop)",
        "to_thread" in calls,
        "boto3 call ran directly on the event loop — event loop was blocked",
    )
    check("returned 1 KB file", len(files) == 1, f"got {len(files)}")
    check("source is 'knowledge_base'", files[0]["source"] == "knowledge_base")


# ---------------------------------------------------------------------------
# Check 4 — end-to-end tool execution
# ---------------------------------------------------------------------------


async def verify_end_to_end() -> None:
    print("\n[4] End-to-end: tool returns combined KB + session files")

    kb   = {"filename": "ledger.csv", "source": "knowledge_base", "size_bytes": 2048,
             "content_type": "text/csv", "document_id": "d1", "s3_key": "k1"}
    sess = {"filename": "budget.csv", "source": "chat_attachment", "size_bytes": 1024,
             "content_type": "text/csv", "document_id": "u1", "s3_key": "k2", "s3_bucket": "b"}

    _orig_kb, _orig_sess = _mod._get_kb_files, _mod._get_session_files  # type: ignore
    _mod._get_kb_files      = AsyncMock(return_value=[kb])    # type: ignore
    _mod._get_session_files = AsyncMock(return_value=[sess])  # type: ignore

    try:
        fn = make_list_spreadsheets_tool(assistant_id="ast-1", session_id="s1", user_id="u1")
        result = await fn()
    finally:
        _mod._get_kb_files      = _orig_kb    # type: ignore
        _mod._get_session_files = _orig_sess  # type: ignore

    check("status is 'success'", result["status"] == "success")
    files = result.get("files", [])
    check("2 files returned", len(files) == 2, f"got {len(files)}")
    check("both files present", {f["filename"] for f in files} == {"ledger.csv", "budget.csv"})


# ---------------------------------------------------------------------------
# Check 5 — concurrent calls don't deadlock
# ---------------------------------------------------------------------------


async def verify_no_deadlock_under_concurrency() -> None:
    print("\n[5] Concurrent tool calls must not deadlock")

    _orig_kb, _orig_sess = _mod._get_kb_files, _mod._get_session_files  # type: ignore
    _mod._get_kb_files      = AsyncMock(return_value=[])  # type: ignore
    _mod._get_session_files = AsyncMock(return_value=[])  # type: ignore

    try:
        fn = make_list_spreadsheets_tool(assistant_id=None, session_id="s1", user_id="u1")
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[fn() for _ in range(5)]),
                timeout=5.0,
            )
            check("5 concurrent calls completed without deadlock", len(results) == 5)
        except asyncio.TimeoutError:
            check("5 concurrent calls completed without deadlock",
                  False, "TIMEOUT — likely deadlock")
    finally:
        _mod._get_kb_files      = _orig_kb    # type: ignore
        _mod._get_session_files = _orig_sess  # type: ignore


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _main() -> None:
    print("=" * 60)
    print("  Async fix verification — list_spreadsheets_tool (#260)")
    print("=" * 60)

    verify_tool_is_async()
    await verify_session_files_awaits_repo()
    await verify_kb_files_uses_to_thread()
    await verify_end_to_end()
    await verify_no_deadlock_under_concurrency()

    print("\n" + "=" * 60)
    if _failures:
        print(f"  {FAIL}  {len(_failures)} check(s) FAILED:")
        for f in _failures:
            print(f"       - {f}")
        sys.exit(1)
    else:
        print(f"  {PASS}  All checks passed — async fix is working correctly.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
