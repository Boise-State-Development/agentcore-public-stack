"""The eval harness CLI's offline paths: cases, the estimate, score's exit codes, baseline promotion.

``run`` and ``cleanup`` need a deployed stack; their logic lives in
``apis.shared.evals`` and is tested there.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import eval_harness  # noqa: E402

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _case(cid: str):
    return {
        "id": f"tool_selection.{cid}",
        "family": "tool_selection",
        "turns": [{"prompt": "q"}],
        "run": {"enabled_tools": ["calculator"]},
        "expect": {"tools": {"must_call": ["calculator"]}},
    }


@pytest.fixture
def golden(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "tool_selection.json").write_text(
        json.dumps([_case(c) for c in "abcdefgh"]), encoding="utf-8"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    baselines = tmp_path / "baselines"
    return ["--cases-dir", str(cases_dir), "--fixtures-dir", str(fixtures), "--baselines-dir", str(baselines)], baselines


def _write_run(out: Path, failing: str = "", k: int = 3, samples: int = 3):
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps({"model_id": MODEL, "k": k, "startedAt": "t", "user_id": "u"}),
                                   encoding="utf-8")
    with (out / "results.jsonl").open("w", encoding="utf-8") as fh:
        for cid in "abcdefgh":
            for s in range(samples):
                passed = cid not in failing
                fh.write(json.dumps({
                    "case_id": f"tool_selection.{cid}", "family": "tool_selection", "sample": s,
                    "model_id": MODEL, "passed": passed, "failures": [] if passed else ["did not call calculator"],
                    "final_message": "ok", "tool_calls": [],
                }) + "\n")


def test_cases_command_reports_the_committed_set(capsys):
    assert eval_harness.main(["cases"]) == 0
    out = capsys.readouterr().out
    assert "tool_selection" in out and "The eval user's role must grant" in out


def test_estimate_counts_every_turn_and_flags_an_unpriced_model():
    from apis.shared.evals.cases import parse_case

    case = parse_case({**_case("a"), "turns": [{"prompt": "1"}, {"prompt": "2"}]}, family="tool_selection")
    turns, dollars, note = eval_harness.estimate([(case, 0), (case, 1)], MODEL)
    assert turns == 4 and dollars > 0 and note == ""
    assert "priced at" in eval_harness.estimate([(case, 0)], "some.unpriced-model")[2]


def test_baseline_then_score_passes_then_fails_on_regression(tmp_path, golden, capsys):
    flags, baselines = golden
    run1 = tmp_path / "run1"
    _write_run(run1)
    assert eval_harness.main(flags + ["baseline", "--out", str(run1)]) == 0
    assert (baselines / eval_harness.baseline_filename(MODEL)).is_file()

    assert eval_harness.main(flags + ["score", "--out", str(run1)]) == 0
    assert "GATE: pass" in capsys.readouterr().out

    run2 = tmp_path / "run2"
    _write_run(run2, failing="abc")
    report = tmp_path / "report.json"
    assert eval_harness.main(
        flags + ["score", "--out", str(run2), "--explain", "tool_selection", "--json", str(report)]
    ) == 1
    out = capsys.readouterr().out
    assert "GATE: FAIL" in out and "LOST  tool_selection.a" in out
    assert json.loads(report.read_text())["gateFailed"] is True


def test_baseline_refuses_a_partial_run(tmp_path, golden, capsys):
    flags, baselines = golden
    run = tmp_path / "run"
    _write_run(run, samples=2)
    assert eval_harness.main(flags + ["baseline", "--out", str(run)]) == 2
    assert "fewer than k=3" in capsys.readouterr().err
    assert not baselines.exists()


def test_score_without_a_baseline_is_a_setup_error(tmp_path, golden, capsys):
    flags, _ = golden
    run = tmp_path / "run"
    _write_run(run)
    assert eval_harness.main(flags + ["score", "--out", str(run)]) == 2
    assert "no baseline" in capsys.readouterr().err


def test_run_refuses_production_before_anything_else(tmp_path, capsys):
    code = eval_harness.main([
        "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--user-id", "u", "--prefix", "prod-boisestateai",
    ])
    assert code == 2
    assert "production" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


def test_run_without_yes_only_estimates(tmp_path, capsys):
    code = eval_harness.main([
        "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--user-id", "u",
        "--prefix", "dev-example", "--family", "tool_selection",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimate only" in out
    assert not (tmp_path / "r").exists()
