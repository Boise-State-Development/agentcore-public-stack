"""Tests for the spreadsheet-analysis tool's policy gate.

The analyze tool forwards the supplied ``python_code`` to the Bedrock
Code Interpreter sandbox. These tests assert that policy violations
short-circuit *before* any sandbox lookup or file resolution work runs.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    make_analyze_tool,
)


def _underlying(tool_obj):
    inner = getattr(tool_obj, "_tool_func", None) or getattr(tool_obj, "func", None) or tool_obj
    if hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    return inner


@pytest.mark.asyncio
async def test_subprocess_in_analyze_code_rejected_before_file_resolution() -> None:
    tool = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    impl = _underlying(tool)

    with (
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value="ci-test",
        ),
        patch("agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file") as find_file,
    ):
        result = await impl(
            filename="ledger.csv",
            python_code="import subprocess\nsubprocess.run(['id'])",
        )

    assert result["status"] == "error"
    assert "policy" in result["content"][0]["text"].lower()
    # Policy gate fires before file-resolution work.
    find_file.assert_not_called()


@pytest.mark.asyncio
async def test_eval_in_analyze_code_rejected() -> None:
    tool = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    impl = _underlying(tool)

    with (
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value="ci-test",
        ),
        patch("agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file") as find_file,
    ):
        result = await impl(
            filename="ledger.csv",
            python_code="eval('1+1')",
        )

    assert result["status"] == "error"
    assert "policy" in result["content"][0]["text"].lower()
    find_file.assert_not_called()


@pytest.mark.asyncio
async def test_legitimate_pandas_code_passes_policy() -> None:
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
        result = await impl(filename="ledger.csv", python_code=src)

    # Policy passed; we land in the file-not-found branch.
    assert result["status"] == "error"
    text = result["content"][0]["text"].lower()
    assert "policy" not in text
    assert "not found" in text or "not accessible" in text


async def _rejection(python_code: str, filename: str = "Budget.xlsx") -> str:
    tool = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    impl = _underlying(tool)
    with (
        patch(
            "agents.builtin_tools.spreadsheet_analysis.analyze_tool._get_code_interpreter_id",
            return_value="ci-test",
        ),
        patch("agents.builtin_tools.spreadsheet_analysis.analyze_tool._find_file") as find_file,
    ):
        result = await impl(filename=filename, python_code=python_code)
    assert result["status"] == "error"
    find_file.assert_not_called()
    return result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "import glob\nprint(glob.glob('*.csv'))",
        "import os\nprint(os.listdir('.'))",
        "from pathlib import Path\nprint(list(Path('.').iterdir()))",
        "with open('Budget.csv') as f:\n    print(f.read())",
    ],
)
async def test_looking_for_the_file_is_told_where_it_already_is(code: str) -> None:
    """Two thirds of prod's rejections: the model went looking for the file."""
    text = await _rejection(code)
    assert "already in the working directory as `Budget.csv`" in text
    assert "pd.read_csv('Budget.csv', low_memory=False)" in text


@pytest.mark.asyncio
async def test_openpyxl_is_named_as_blocked_and_the_csv_offered() -> None:
    text = await _rejection("import openpyxl\nwb = openpyxl.load_workbook('Budget.xlsx')")
    assert "`openpyxl` is blocked" in text
    assert "pd.read_csv('Budget.csv', low_memory=False)" in text


@pytest.mark.asyncio
async def test_re_compile_points_at_the_module_functions() -> None:
    text = await _rejection("import re\npat = re.compile(r'\\d+')\nprint(pat)", filename="ledger.csv")
    assert "`re.compile` is blocked" in text
    assert "re.search" in text


@pytest.mark.asyncio
async def test_another_import_lists_what_is_allowed() -> None:
    from apis.shared.security import ALLOWED_IMPORT_ROOTS

    text = await _rejection("import difflib\nprint(difflib.get_close_matches('a', ['b']))")
    assert "`difflib` can't be imported" in text
    for root in ALLOWED_IMPORT_ROOTS:
        assert root in text


def test_timing_a_loop_with_time_passes_the_policy() -> None:
    """A bootstrap benchmark timed with ``time.time()`` was refused outright."""
    from apis.shared.security import validate_diagram_code

    validate_diagram_code(
        "import pandas as pd, random, time\n"
        "df = pd.read_csv('benchmark-seed.csv', low_memory=False)\n"
        "start = time.time()\n"
        "means = [sum(random.choices(df['value'].tolist(), k=10)) / 10 for _ in range(100)]\n"
        "print(f'Elapsed: {time.time() - start:.2f}s', time.perf_counter())\n"
    )


@pytest.mark.asyncio
async def test_a_syntax_error_names_the_line() -> None:
    text = await _rejection("import pandas as pd\ndf = pd.read_csv('x.csv'\n")
    assert "syntax error (line" in text


def test_the_spec_names_exactly_the_policy_allowlist_and_not_openpyxl() -> None:
    """The docstring said openpyxl was available while the policy refused it."""
    import re as _re

    from apis.shared.security import ALLOWED_IMPORT_ROOTS

    spec = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1").tool_spec
    description = " ".join(spec["description"].split())
    listed = _re.search(r"Imports are limited to (.+?)\. ", description)
    assert listed is not None
    names = set(_re.findall(r"[a-z][a-z0-9_]*", listed.group(1).replace("the stdlib modules", ""))) - {"and"}
    assert names == set(ALLOWED_IMPORT_ROOTS)
    assert "openpyxl" not in listed.group(1)
    assert "`openpyxl`" in description and "blocked" in description


#: The spec sits in the cacheable ``toolConfig``. A change here is a deliberate
#: prefix rewrite at deploy: update the pin with it and state the token delta in
#: the PR. Last changed when ``time`` joined the import allowlist.
SPEC_DIGEST = "bc8cd6a5877645b5a086a8298cb64cc0f10c37edd9dfc5d82f2868e3c8d3bb6b"


def test_the_spec_is_pinned_and_constant_per_deployment() -> None:
    import hashlib
    import json

    def digest(tool) -> str:
        return hashlib.sha256(json.dumps(tool.tool_spec, sort_keys=True).encode()).hexdigest()

    one = make_analyze_tool(assistant_id=None, session_id="s1", user_id="u1")
    other = make_analyze_tool(assistant_id="a2", session_id="s2", user_id="u2")
    assert digest(one) == digest(other) == SPEC_DIGEST
