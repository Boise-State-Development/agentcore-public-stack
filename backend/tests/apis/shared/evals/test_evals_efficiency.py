"""Eval efficiency: what a case cost to answer, compared only where it was answered right.

The properties, in the order they would mislead if wrong:

1. A case that fails on either side is never paired: a wrong answer is often a
   short one, and pairing it would report lost quality as gained efficiency.
2. A measure a side could not report is absent, never zero.
3. A change is marked only with enough cases and a large enough move.
4. A baseline promoted before efficiency existed reads as "no baseline
   efficiency", never as no change.
"""

from __future__ import annotations

from apis.shared.evals.efficiency import (
    MIN_PAIRED_CASES,
    case_efficiency,
    compare_efficiency,
    format_efficiency_table,
    moved,
    run_efficiency,
    sample_efficiency,
)


def _row(case_id, *, sample=0, out=100, inp=1000, read=0, write=0, tools=1, duration=2000, first=500, turns=1):
    return {
        "case_id": case_id,
        "family": case_id.split(".", 1)[0],
        "sample": sample,
        "usage": {
            "outputTokens": out, "inputTokens": inp,
            "cacheReadInputTokens": read, "cacheWriteInputTokens": write,
        },
        "timing": [{"firstTokenMs": first, "turnDurationMs": duration} for _ in range(turns)],
        "turns": [{"tool_trace": [{"name": "calculator"}] * tools} for _ in range(turns)],
    }


def _baseline(cases):
    return {"cases": {
        case_id: {"family": case_id.split(".", 1)[0], "passed": passed, **({"efficiency": eff} if eff else {})}
        for case_id, (passed, eff) in cases.items()
    }}


def test_a_sample_sums_input_however_it_was_billed_and_every_turns_tools_and_time():
    measured = sample_efficiency(_row("tool_selection.a", inp=100, read=900, write=50, tools=2, duration=1500, turns=2))
    assert measured == {
        "outputTokens": 100.0, "inputTokens": 1050.0, "toolCalls": 4.0, "durationMs": 3000.0, "firstTokenMs": 500.0,
    }


def test_a_turn_without_a_duration_makes_the_samples_duration_unknown_not_partial():
    row = _row("tool_selection.a", turns=2)
    row["timing"][1]["turnDurationMs"] = None
    assert sample_efficiency(row)["durationMs"] is None


def test_a_row_with_nothing_measured_reports_nothing():
    assert sample_efficiency({"case_id": "x.y"}) == {
        "outputTokens": None, "inputTokens": None, "toolCalls": None, "durationMs": None, "firstTokenMs": None,
    }
    assert case_efficiency([{"case_id": "x.y"}]) == {}


def test_a_case_is_its_median_sample():
    rows = [_row("tool_selection.a", sample=i, out=out) for i, out in enumerate((100, 900, 120))]
    assert case_efficiency(rows)["tool_selection.a"]["outputTokens"] == 120.0


def test_only_cases_that_pass_on_both_sides_are_compared():
    eff = {"outputTokens": 100.0}
    baseline = _baseline({
        "tool_selection.both": (True, eff),
        "tool_selection.lost": (True, eff),
        "tool_selection.gained": (False, eff),
    })
    rows = [_row(f"tool_selection.{name}", out=200) for name in ("both", "lost", "gained", "new")]
    passed = {
        "tool_selection.both": True, "tool_selection.lost": False,
        "tool_selection.gained": True, "tool_selection.new": True,
    }

    (family,) = compare_efficiency(baseline, rows, passed)
    assert family.paired == 1
    assert (family.metrics["outputTokens"].n, family.metrics["outputTokens"].base,
            family.metrics["outputTokens"].run) == (1, 100.0, 200.0)


def test_a_measure_missing_on_one_side_is_left_out_of_that_measure_only():
    baseline = _baseline({"tool_selection.a": (True, {"outputTokens": 100.0})})
    (family,) = compare_efficiency(baseline, [_row("tool_selection.a")], {"tool_selection.a": True})
    assert set(family.metrics) == {"outputTokens"}


def test_a_baseline_without_efficiency_says_so():
    baseline = _baseline({"tool_selection.a": (True, None)})
    comparisons = compare_efficiency(baseline, [_row("tool_selection.a")], {"tool_selection.a": True})
    assert comparisons[0].paired == 1 and not comparisons[0].metrics
    assert "no baseline efficiency" in format_efficiency_table(comparisons)
    assert moved(comparisons) == []


def test_a_change_is_marked_only_with_enough_cases_and_a_large_move():
    def compare(n, run_out):
        ids = [f"tool_selection.c{i}" for i in range(n)]
        baseline = _baseline({cid: (True, {"outputTokens": 100.0}) for cid in ids})
        return compare_efficiency(baseline, [_row(cid, out=run_out) for cid in ids], {cid: True for cid in ids})

    assert moved(compare(MIN_PAIRED_CASES, 130)) == ["tool_selection outputTokens +30%"]
    assert moved(compare(MIN_PAIRED_CASES - 1, 130)) == []
    assert moved(compare(MIN_PAIRED_CASES, 110)) == []
    assert "*" in format_efficiency_table(compare(MIN_PAIRED_CASES, 60))


def test_the_runs_own_numbers_count_each_passing_case_once():
    rows = [_row("tool_selection.a", sample=i, out=100) for i in range(3)] + [_row("tool_selection.b", out=50)]
    numbers = run_efficiency(rows, {"tool_selection.a": True, "tool_selection.b": False})
    assert numbers == {"tool_selection": {
        "outputTokens": {"n": 1, "sum": 100.0}, "inputTokens": {"n": 1, "sum": 1000.0},
        "toolCalls": {"n": 1, "sum": 1.0}, "durationMs": {"n": 1, "sum": 2000.0},
        "firstTokenMs": {"n": 1, "sum": 500.0},
    }}


def test_the_ledger_form_is_numbers_only():
    baseline = _baseline({"tool_selection.a": (True, {"outputTokens": 100.0})})
    (family,) = compare_efficiency(baseline, [_row("tool_selection.a", out=80)], {"tool_selection.a": True})
    assert family.to_dict() == {"paired": 1, "outputTokens": {"n": 1, "base": 100.0, "run": 80.0}}
