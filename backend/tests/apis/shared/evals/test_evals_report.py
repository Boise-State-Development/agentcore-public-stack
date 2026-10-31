"""Baselines and the per-family comparison (eval-harness spec §8)."""

from __future__ import annotations

import json

import pytest

from apis.shared.evals.cases import parse_case
from apis.shared.evals.report import (
    MAX_BASELINE_ANSWER_CHARS,
    ReportError,
    baseline_filename,
    build_baseline,
    case_outcomes,
    compare_run,
    explain_family,
    format_table,
    load_baseline,
    read_rows,
)

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _case(cid: str, family: str = "tool_selection"):
    return parse_case(
        {"id": f"{family}.{cid}", "family": family, "turns": [{"prompt": "q"}], "expect": {"contains_any": ["x"]}},
        family=family,
    )


def _row(cid: str, sample: int, passed: bool, family: str = "tool_selection", answer: str = "", model: str = MODEL):
    return {
        "case_id": f"{family}.{cid}",
        "family": family,
        "sample": sample,
        "model_id": model,
        "passed": passed,
        "failures": [] if passed else ["missing expected value"],
        "final_message": answer or ("yes" if passed else "no"),
        "tool_calls": [{"name": "calculator"}],
    }


def _meta(k: int = 3):
    return {"model_id": MODEL, "k": k, "startedAt": "2026-10-29T10:00:00Z", "target": "runtime:dev",
            "gitCommit": "abc", "user_id": "should-not-be-copied"}


def test_baseline_filename_is_safe_for_any_model_id():
    assert baseline_filename(MODEL) == "us.anthropic.claude-haiku-4-5-20251001-v1_0.json"


def test_case_outcomes_take_the_majority_and_a_representative_answer():
    rows = [_row("a", 0, True, answer="right"), _row("a", 1, False, answer="wrong"), _row("a", 2, True, answer="right")]
    outcome = case_outcomes(rows)["tool_selection.a"]
    assert outcome.passed and outcome.passes == 2 and outcome.samples == 3
    assert outcome.answer == "right"


class TestBuildBaseline:
    def test_complete_run_promotes_without_copying_the_user(self):
        cases = [_case("a"), _case("b")]
        rows = [_row(c, s, c == "a", answer="z" * 2000) for c in ("a", "b") for s in range(3)]
        baseline = build_baseline(rows, cases=cases, meta=_meta())
        assert baseline["modelId"] == MODEL and baseline["k"] == 3
        assert baseline["cases"]["tool_selection.a"]["passed"] is True
        assert baseline["cases"]["tool_selection.b"]["passed"] is False
        assert len(baseline["cases"]["tool_selection.a"]["answer"]) == MAX_BASELINE_ANSWER_CHARS
        assert "user_id" not in json.dumps(baseline)

    def test_partial_run_is_refused(self):
        cases = [_case("a"), _case("b")]
        rows = [_row("a", s, True) for s in range(3)] + [_row("b", 0, True)]
        with pytest.raises(ReportError, match="fewer than k=3"):
            build_baseline(rows, cases=cases, meta=_meta())

    def test_mixed_models_are_refused(self):
        rows = [_row("a", s, True) for s in range(3)] + [_row("a", 3, True, model="other")]
        with pytest.raises(ReportError, match="mix models"):
            build_baseline(rows, cases=[_case("a")], meta=_meta())


class TestCompareRun:
    def _baseline(self, passes):
        cases = [_case(c) for c in passes]
        rows = [_row(c, s, ok) for c, ok in passes.items() for s in range(3)]
        return build_baseline(rows, cases=cases, meta=_meta())

    def test_a_different_model_is_refused(self):
        with pytest.raises(ReportError, match="compare against that model's baseline"):
            compare_run(self._baseline({"a": True}), [_row("a", 0, True)], model_id="other")

    def test_a_regression_shows_up_in_its_family(self):
        baseline = self._baseline({c: True for c in "abcdefgh"})
        rows = [_row(c, s, c not in "abc") for c in "abcdefgh" for s in range(3)]
        (comparison,) = compare_run(baseline, rows, model_id=MODEL)
        assert comparison.family == "tool_selection" and comparison.failed
        table = format_table([comparison])
        assert "FAIL" in table and "tool_selection" in table

    def test_families_not_run_are_not_reported(self):
        baseline = self._baseline({"a": True})
        rows = [_row("x", 0, True, family="retention")]
        comparisons = compare_run(baseline, rows, model_id=MODEL)
        assert [c.family for c in comparisons] == ["retention"]
        assert comparisons[0].new_in_run == ("retention.x",)

    def test_explain_lists_only_flips_with_both_answers(self):
        baseline = self._baseline({"a": True, "b": True})
        rows = [_row("a", s, False, answer="bad answer") for s in range(3)] + [_row("b", s, True) for s in range(3)]
        text = explain_family("tool_selection", baseline, rows)
        assert "LOST  tool_selection.a" in text and "bad answer" in text
        assert "tool_selection.b" not in text
        assert "calculator" in text
        assert "calls:" not in text

    def test_explain_lists_each_samples_calls_when_given_cost_rows(self):
        baseline = self._baseline({"a": True})
        rows = [{**_row("a", s, s == 2), "session_id": f"eval-{s}"} for s in range(3)]
        calls = {
            "eval-0": [{"cacheStatus": "hit", "compactionEvents": [{"kind": "applied"}], "cost": {"total": 0.002}}],
            "eval-2": [],
        }
        text = explain_family("tool_selection", baseline, rows, calls_by_session=calls)
        lines = text.splitlines()
        assert "  calls:" in lines
        assert any(line.startswith("    [0] FAIL") and "compaction applied 1" in line for line in lines)
        assert "    [1] FAIL  unavailable" in lines
        assert "    [2] pass  no cost rows" in lines


def test_load_baseline_checks_existence_and_schema(tmp_path):
    with pytest.raises(ReportError, match="no baseline"):
        load_baseline(tmp_path / "missing.json")
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
    with pytest.raises(ReportError, match="unsupported"):
        load_baseline(path)


def test_read_rows_skips_blank_lines_and_tolerates_a_missing_file(tmp_path):
    assert read_rows(tmp_path / "none.jsonl") == []
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(_row("a", 0, True)) + "\n\n", encoding="utf-8")
    assert len(read_rows(path)) == 1
