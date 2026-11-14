"""Finding a workbook's other sheets without a wasted call.

On the 2026-11 nightly evals (``usage_patterns.spreadsheet_cross_sheet_lookup``)
Haiku 5.5 and Sonnet 5.5 both went looking for a two-sheet workbook's second
sheet: a ``glob`` was rejected with only the first sheet's name, and the next
guess used the sheet's own capitalisation (``grant_expenses.Expenses.csv``),
which the case-sensitive sandbox does not have. These pin both fixes: the
rejection says how sheets are named, and the cased guess reads the same file.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from agents.builtin_tools.spreadsheet_analysis import analyze_tool, sandbox_pool
from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    _build_bootstrap_code,
    _parse_sheet_inventory,
    _policy_rejection,
)
from agents.builtin_tools.spreadsheet_analysis.sandbox_pool import StagedFile
from apis.shared.security import PolicyError, validate_diagram_code


def _rejection_for(code: str, filename: str, sheet_note: str = "") -> str:
    with pytest.raises(PolicyError) as info:
        validate_diagram_code(code)
    return _policy_rejection(info.value, filename, sheet_note)


def test_a_workbook_rejection_names_the_per_sheet_pattern() -> None:
    text = _rejection_for("import glob\nprint(glob.glob('*'))", "grant_expenses.xlsx")
    assert "`grant_expenses.csv`" in text
    assert "`grant_expenses.<sheet>.csv`" in text
    assert "`grant_expenses.q1_budget.csv`" in text


def test_the_openpyxl_rejection_names_the_pattern_too() -> None:
    text = _rejection_for("import openpyxl", "grant_expenses.xlsx")
    assert "`grant_expenses.<sheet>.csv`" in text


def test_a_staged_sheet_list_replaces_the_rule() -> None:
    note = "**Available sheets (load via `pd.read_csv`):**\n- `Expenses` → `grant_expenses.expenses.csv` (9 rows)"
    text = _rejection_for("import os\nos.listdir('.')", "grant_expenses.xlsx", note)
    assert note in text
    assert "<sheet>" not in text


def test_a_csv_rejection_says_nothing_about_sheets() -> None:
    text = _rejection_for("import glob\nprint(glob.glob('*'))", "ledger.csv")
    assert "sheet" not in text.lower()


def test_the_staged_note_comes_from_this_turns_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Entry:
        staged = {
            "grant_expenses.csv": StagedFile(
                source="chat_attachment",
                s3_key="k",
                size_bytes=1,
                is_xlsx=True,
                csv_filename="grant_expenses.csv",
                multi_sheet_note="SHEETS",
                schema_preview="",
                paths=[],
                files={},
            )
        }

    monkeypatch.setattr(sandbox_pool, "acquire", lambda session_id, user_id: _Entry())
    assert analyze_tool._staged_sheet_note("s", "u", "grant_expenses.xlsx") == "SHEETS"
    assert analyze_tool._staged_sheet_note("s", "u", "other.xlsx") == ""
    monkeypatch.setattr(sandbox_pool, "acquire", lambda session_id, user_id: None)
    assert analyze_tool._staged_sheet_note("s", "u", "grant_expenses.xlsx") == ""


def _case_sensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("")
    sensitive = not (path / "caseprobe").exists()
    probe.unlink()
    return sensitive


def _temp_fs_is_case_sensitive() -> bool:
    # A private directory per probe: xdist workers import this module at the
    # same time, and a shared probe path let one delete another's file.
    with tempfile.TemporaryDirectory() as d:
        return _case_sensitive(Path(d))


# The sandbox is Linux (case-sensitive), where the alias is a symlink. On a
# case-insensitive filesystem (a developer's Mac) the cased name already is
# the CSV, so nothing is linked and the read still has to work.
needs_case_sensitive_fs = pytest.mark.skipif(
    not _temp_fs_is_case_sensitive(),
    reason="the alias only exists on a case-sensitive filesystem",
)


def _run_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sheets: dict[str, list[list]]) -> str:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "_encoded.b64").write_text(base64.b64encode(buf.getvalue()).decode("ascii"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    exec(_build_bootstrap_code("grant_expenses", "grant_expenses.csv"), {})  # noqa: S102 - our own generated code
    monkeypatch.undo()
    return out.getvalue()


def test_the_sheets_own_capitalisation_reads_the_same_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _run_bootstrap(
        tmp_path,
        monkeypatch,
        {"Expenses": [["Line", "Amount"], ["EXP-1", 10]], "Approvals": [["Line", "Code"], ["EXP-1", "AP-5"]]},
    )
    inventory = _parse_sheet_inventory(stdout)
    assert [s["path"] for s in inventory["sheets"]] == [
        "grant_expenses.expenses.csv",
        "grant_expenses.approvals.csv",
    ]
    cased = tmp_path / "grant_expenses.Approvals.csv"
    assert cased.is_symlink() == _case_sensitive(tmp_path)
    assert cased.read_text() == (tmp_path / "grant_expenses.approvals.csv").read_text()
    assert "AP-5" in cased.read_text()


@needs_case_sensitive_fs
def test_no_alias_for_a_lowercase_or_deduplicated_sheet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_bootstrap(tmp_path, monkeypatch, {"notes": [["a"]], "Q1": [["b"]], "q1 ": [["c"]]})
    names = sorted(p.name for p in tmp_path.iterdir() if p.name != "_encoded.b64")
    # "q1 " sanitizes to q1 like "Q1", so it is deduplicated to q1_2 and gets no alias.
    assert names == [
        "grant_expenses.Q1.csv",
        "grant_expenses.csv",
        "grant_expenses.notes.csv",
        "grant_expenses.q1.csv",
        "grant_expenses.q1_2.csv",
    ]
    assert os.path.islink(tmp_path / "grant_expenses.Q1.csv")


@needs_case_sensitive_fs
def test_a_real_file_at_the_alias_is_never_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "grant_expenses.Expenses.csv").write_text("the user's own file")
    _run_bootstrap(tmp_path, monkeypatch, {"Expenses": [["a"]], "Other": [["b"]]})
    assert (tmp_path / "grant_expenses.Expenses.csv").read_text() == "the user's own file"
