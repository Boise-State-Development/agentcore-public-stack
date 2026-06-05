"""Tests for the spreadsheet-analysis tool's policy gate.

The analyze tool forwards the supplied ``python_code`` to the Bedrock
Code Interpreter sandbox. These tests assert that policy violations
short-circuit *before* any sandbox lookup or file resolution work runs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    make_analyze_tool,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _underlying(tool_obj):
    inner = getattr(tool_obj, "_tool_func", None) or getattr(tool_obj, "func", None) or tool_obj
    if hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    return inner


def test_subprocess_in_analyze_code_rejected_before_file_resolution() -> None:
    tool = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    impl = _underlying(tool)

    with (
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value="ci-test",
        ),
        patch("agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file") as find_file,
    ):
        result = _run(
            impl(
                filename="ledger.csv",
                python_code="import subprocess\nsubprocess.run(['id'])",
            )
        )

    assert result["status"] == "error"
    assert "policy" in result["content"][0]["text"].lower()
    # Policy gate fires before file-resolution work.
    find_file.assert_not_called()


def test_eval_in_analyze_code_rejected() -> None:
    tool = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    impl = _underlying(tool)

    with (
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value="ci-test",
        ),
        patch("agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file") as find_file,
    ):
        result = _run(
            impl(
                filename="ledger.csv",
                python_code="eval('1+1')",
            )
        )

    assert result["status"] == "error"
    assert "policy" in result["content"][0]["text"].lower()
    find_file.assert_not_called()


def test_legitimate_pandas_code_passes_policy() -> None:
    """Realistic analysis code makes it past policy and proceeds to file
    resolution (which we short-circuit with a not-found result).
    """
    tool = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    impl = _underlying(tool)

    src = "import pandas as pd\n" "df = pd.read_csv('ledger.csv')\n" "summary = df.groupby('category').sum()\n" "print(summary)\n"

    async def _no_file(*_args, **_kw):
        return None

    with (
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value="ci-test",
        ),
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file",
            side_effect=_no_file,
        ),
    ):
        result = _run(impl(filename="ledger.csv", python_code=src))

    # Policy passed; we land in the file-not-found branch.
    assert result["status"] == "error"
    text = result["content"][0]["text"].lower()
    assert "policy" not in text
    assert "not found" in text or "not accessible" in text
