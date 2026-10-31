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


class TestRunAuthArguments:
    BASE = ["run", "--out", "x", "--model-id", MODEL, "--prefix", "dev-example"]

    def test_headless_grant_needs_a_user_id(self):
        with pytest.raises(SystemExit):
            eval_harness.parse_args(self.BASE)
        assert eval_harness.parse_args(self.BASE + ["--user-id", "u"]).auth == "headless-grant"

    def test_cognito_needs_a_base_url_and_no_user_id(self):
        with pytest.raises(SystemExit):
            eval_harness.parse_args(self.BASE + ["--auth", "cognito"])
        args = eval_harness.parse_args(self.BASE + ["--auth", "cognito", "--base-url", "https://dev.example/api"])
        assert args.user_id is None and args.base_url == "https://dev.example/api"

    def test_a_production_base_url_is_refused(self, tmp_path, capsys):
        code = eval_harness.main([
            "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--prefix", "dev-example",
            "--auth", "cognito", "--base-url", "https://boisestate.ai/api",
        ])
        assert code == 2 and "production" in capsys.readouterr().err


class TestJudgeCommand:
    def _run_dir(self, tmp_path):
        out = tmp_path / "run"
        out.mkdir()
        (out / "meta.json").write_text(json.dumps({"model_id": MODEL, "k": 1}), encoding="utf-8")
        rows = [
            {"case_id": "general_chat.explain_mutex_vs_semaphore", "family": "general_chat", "sample": 0,
             "model_id": MODEL, "passed": True, "final_message": "ok", "tool_calls": [], "session": {}},
            {"case_id": "tool_selection.square_root_reaches_for_calculator", "family": "tool_selection",
             "sample": 0, "model_id": MODEL, "passed": True, "final_message": "87",
             "tool_calls": [{"name": "calculator"}, {"name": "calculator"}], "session": {}},
        ]
        (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return out

    def test_estimates_one_call_per_evaluator_and_per_tool_call(self, tmp_path, capsys):
        out = self._run_dir(tmp_path)
        assert eval_harness.main(["judge", "--out", str(out)]) == 0
        printed = capsys.readouterr().out
        # general_chat case: helpfulness + conciseness = 2; tool case: two tool calls = 2.
        assert "2 samples to judge" in printed and "~4 judge calls" in printed and "estimate only" in printed

    def test_spending_without_the_package_is_a_clear_setup_error(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "strands_evals", None)  # import now raises ImportError
        out = self._run_dir(tmp_path)
        assert eval_harness.main(["judge", "--out", str(out), "--yes"]) == 2
        assert "dev extra" in capsys.readouterr().err
