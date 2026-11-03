"""The judge adapter, judged rows, and judge-aware baselines (eval-harness spec D2, §8).

strands-agents-evals is a dev extra and may be absent, so the adapter is
exercised against a stand-in package with the 1.7.0 shapes: evaluators built
as ``Cls(model=...)`` (``OutputEvaluator(rubric=..., model=..., include_inputs=...)``),
``evaluate(EvaluationData) -> [EvaluationOutput]``, and ``Session.model_validate``.
"""

from __future__ import annotations

import types
from typing import Any, Dict, List

import pytest

from apis.shared.evals.cases import parse_case
from apis.shared.evals.judges import (
    EVALUATOR_CLASSES,
    StrandsEvalsJudge,
    judge_row,
    last_turn,
    same_base_model,
    summarize_outputs,
)
from apis.shared.evals.report import ReportError, build_baseline, compare_judges, judge_means
from apis.shared.evals.session_mapper import build_session

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _output(score: float, passed: bool = True, label: str = "Good", reason: str = "ok"):
    return types.SimpleNamespace(score=score, test_pass=passed, label=label, reason=reason)


class FakePackage:
    """A stand-in for ``strands_evals`` that records what the adapter asked for."""

    def __init__(self, scores: Dict[str, List[Any]]):
        self.scores = scores
        self.built: List[Dict[str, Any]] = []
        self.seen: List[Any] = []
        package = self

        def make(class_name: str):
            class Evaluator:
                def __init__(self, **kwargs):
                    package.built.append({"class": class_name, **kwargs})
                    self.class_name = class_name

                def evaluate(self, data):
                    package.seen.append(data)
                    outcome = package.scores[self.class_name]
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

            return Evaluator

        names = list(EVALUATOR_CLASSES.values()) + ["OutputEvaluator"]
        self.modules = {
            "strands_evals.evaluators": types.SimpleNamespace(**{n: make(n) for n in names}),
            "strands_evals.types.trace": types.SimpleNamespace(
                Session=types.SimpleNamespace(model_validate=lambda data: ("Session", data))
            ),
            "strands_evals.types.evaluation": types.SimpleNamespace(EvaluationData=lambda **kw: kw),
        }

    def import_module(self, name: str):
        return self.modules[name]


def _session(answer: str = "It is $35.", tools=()):
    run = {
        "final_message": answer,
        "started_at": "2026-10-30T10:00:00Z",
        "finished_at": "2026-10-30T10:00:05Z",
        "tool_trace": [
            {"tool_use_id": f"t{i}", "name": n, "input": {}, "result_preview": r, "is_error": False}
            for i, (n, r) in enumerate(tools)
        ],
    }
    return build_session(session_id="eval-1", turns=[("What is the fee?", run)])


class TestAdapter:
    def test_builds_each_evaluator_on_the_judge_model_and_feeds_the_last_turn(self):
        pkg = FakePackage({"HelpfulnessEvaluator": [_output(0.8)], "FaithfulnessEvaluator": [_output(1.0)]})
        verdicts = StrandsEvalsJudge("judge-model", import_module=pkg.import_module).judge(
            _session(), evaluators=["helpfulness", "faithfulness"]
        )
        assert [(v.evaluator, v.score, v.passed) for v in verdicts] == [("helpfulness", 0.8, True), ("faithfulness", 1.0, True)]
        assert all(b["model"] == "judge-model" for b in pkg.built)
        data = pkg.seen[0]
        assert data["input"] == "What is the fee?" and data["actual_output"] == "It is $35."
        assert data["actual_trajectory"][0] == "Session"

    def test_a_rubric_runs_through_output_evaluator(self):
        pkg = FakePackage({"OutputEvaluator": [_output(0.0, passed=False, label="Fail")]})
        verdicts = StrandsEvalsJudge("m", import_module=pkg.import_module).judge(
            _session(), evaluators=[], rubric="Must mention $35."
        )
        assert verdicts[0].evaluator == "rubric" and verdicts[0].passed is False
        assert pkg.built == [{"class": "OutputEvaluator", "rubric": "Must mention $35.", "model": "m", "include_inputs": True}]

    def test_one_failing_judge_does_not_lose_the_others(self):
        pkg = FakePackage({"HelpfulnessEvaluator": RuntimeError("throttled"), "ConcisenessEvaluator": [_output(0.5)]})
        verdicts = StrandsEvalsJudge("m", import_module=pkg.import_module).judge(
            _session(), evaluators=["helpfulness", "conciseness"]
        )
        assert verdicts[0].score is None and "throttled" in verdicts[0].error
        assert verdicts[1].score == 0.5

    def test_unknown_evaluator_is_refused_before_any_call(self):
        pkg = FakePackage({})
        with pytest.raises(ValueError, match="unknown evaluator"):
            StrandsEvalsJudge("m", import_module=pkg.import_module).judge(_session(), evaluators=["vibes"])
        assert pkg.built == []


def test_summarize_averages_tool_level_outputs_and_flags_nothing_to_judge():
    verdict = summarize_outputs("tool_selection", [_output(1.0), _output(0.0, passed=False, label="No")])
    assert verdict.score == 0.5 and verdict.passed is False and verdict.metadata == {"outputs": 2}
    empty = summarize_outputs("tool_selection", [])
    assert empty.score is None and empty.error == "nothing to judge"


def test_same_base_model_ignores_the_inference_profile_prefix():
    haiku = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert same_base_model(haiku, "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert same_base_model(haiku, "anthropic.claude-haiku-4-5-20251001-v1:0")
    assert not same_base_model(haiku, "us.anthropic.claude-sonnet-4-6")
    assert not same_base_model("", "")


def test_last_turn_reads_the_final_agent_span():
    session = build_session(
        session_id="s",
        turns=[("first", {"final_message": "a"}), ("second", {"final_message": "b"})],
    )
    assert last_turn(session) == ("second", "b")
    assert last_turn({"traces": []}) == ("", "")


def test_judge_row_scrubs_document_content_before_any_judge_reads_it():
    received = {}

    class Recorder:
        def judge(self, session, *, evaluators, rubric=None):
            received["session"] = session
            from apis.shared.evals.judges import JudgeVerdict

            return [JudgeVerdict(evaluator="faithfulness", score=1.0, passed=True)]

    row = {"case_id": "document_qa.x", "family": "document_qa", "sample": 2,
           "session": _session(tools=[("document_read", "SECRET PAGE TEXT")])}
    out = judge_row(row, evaluators=["faithfulness"], rubric=None, judge=Recorder(), judge_model="m")
    assert "SECRET PAGE TEXT" not in str(received["session"])
    assert out == [{"case_id": "document_qa.x", "family": "document_qa", "sample": 2, "judge_model": "m",
                    "evaluator": "faithfulness", "score": 1.0, "passed": True, "label": None,
                    "reason": None, "error": None, "metadata": {}}]


class TestJudgeReport:
    def _case(self, cid: str):
        return parse_case(
            {"id": f"general_chat.{cid}", "family": "general_chat", "turns": [{"prompt": "q"}],
             "judge": {"evaluators": ["helpfulness"]}},
            family="general_chat",
        )

    def _rows(self, cids):
        return [{"case_id": f"general_chat.{c}", "family": "general_chat", "sample": s, "model_id": MODEL,
                 "passed": True, "final_message": "ok"} for c in cids for s in range(2)]

    def _judgements(self, cid, scores, model="judge-a"):
        return [{"case_id": f"general_chat.{cid}", "family": "general_chat", "sample": i, "judge_model": model,
                 "evaluator": "helpfulness", "score": sc} for i, sc in enumerate(scores)]

    def test_means_skip_errors_and_empty_verdicts(self):
        js = self._judgements("a", [1.0, 0.5, None])
        assert judge_means(js) == {"general_chat.a": {"helpfulness": 0.75}}

    def test_baseline_carries_judge_scores_and_model(self):
        meta = {"model_id": MODEL, "k": 2}
        baseline = build_baseline(self._rows("a"), cases=[self._case("a")], meta=meta,
                                  judgements=self._judgements("a", [1.0, 0.5]))
        assert baseline["judgeModel"] == "judge-a"
        assert baseline["judgeScores"] == {"general_chat.a": {"helpfulness": 0.75}}

    def test_baseline_without_any_judgements_has_no_judge_scores(self):
        baseline = build_baseline(self._rows("a"), cases=[self._case("a")], meta={"model_id": MODEL, "k": 2})
        assert "judgeScores" not in baseline

    def test_a_case_with_nothing_to_judge_still_counts_as_judged(self):
        meta = {"model_id": MODEL, "k": 2}
        empty = [{"case_id": "general_chat.b", "family": "general_chat", "sample": 0, "judge_model": "judge-a",
                  "evaluator": "helpfulness", "score": None, "error": "nothing to judge"}]
        baseline = build_baseline(self._rows("ab"), cases=[self._case("a"), self._case("b")], meta=meta,
                                  judgements=self._judgements("a", [1.0]) + empty)
        assert baseline["judgeScores"] == {"general_chat.a": {"helpfulness": 1.0}}

    def test_baseline_refuses_partly_judged_runs_and_mixed_judges(self):
        meta = {"model_id": MODEL, "k": 2}
        with pytest.raises(ReportError, match="no judgements"):
            build_baseline(self._rows("ab"), cases=[self._case("a"), self._case("b")], meta=meta,
                           judgements=self._judgements("a", [1.0]))
        with pytest.raises(ReportError, match="mix judge models"):
            build_baseline(self._rows("a"), cases=[self._case("a")], meta=meta,
                           judgements=self._judgements("a", [1.0]) + self._judgements("a", [0.5], model="judge-b"))

    def test_compare_pairs_cases_and_refuses_a_different_judge(self):
        baseline = {"judgeModel": "judge-a", "judgeScores": {"general_chat.a": {"helpfulness": 0.9},
                                                              "general_chat.b": {"helpfulness": 0.7}}}
        run = self._judgements("a", [0.5, 0.5]) + self._judgements("c", [1.0])
        (comparison,) = compare_judges(baseline, run, judge_model="judge-a")
        assert (comparison.family, comparison.evaluator, comparison.n) == ("general_chat", "helpfulness", 1)
        assert comparison.delta == pytest.approx(-0.4)
        with pytest.raises(ReportError, match="not comparable"):
            compare_judges(baseline, run, judge_model="judge-b")
        assert compare_judges({}, run) == []


class TestRefusalOrientation:
    def _verdict(self, score):
        from apis.shared.evals.judges import JudgeVerdict

        return JudgeVerdict(evaluator="refusal", score=score, passed=score == 1.0, label="Yes" if score else "No")

    def test_a_benign_case_inverts_so_one_means_answered(self):
        from apis.shared.evals.judges import orient_refusal

        oriented = orient_refusal(self._verdict(0.0), expected_refusal=False)
        assert (oriented.score, oriented.passed) == (1.0, True) and "inverted" in oriented.metadata["oriented"]

    def test_a_harmful_case_and_other_judges_are_left_alone(self):
        from apis.shared.evals.judges import JudgeVerdict, orient_refusal

        assert orient_refusal(self._verdict(1.0), expected_refusal=True).score == 1.0
        other = JudgeVerdict(evaluator="helpfulness", score=0.2, passed=False)
        assert orient_refusal(other, expected_refusal=False) is other
        empty = JudgeVerdict(evaluator="refusal", score=None, passed=None, error="nothing to judge")
        assert orient_refusal(empty, expected_refusal=False) is empty

    def test_judge_row_orients_by_the_case(self):
        class Refused:
            def judge(self, session, *, evaluators, rubric=None):
                from apis.shared.evals.judges import JudgeVerdict

                return [JudgeVerdict(evaluator="refusal", score=1.0, passed=True)]

        row = {"case_id": "refusal.benign", "family": "refusal", "sample": 0, "session": _session()}
        (out,) = judge_row(row, evaluators=["refusal"], rubric=None, judge=Refused(), judge_model="m",
                           expected_refusal=False)
        assert out["score"] == 0.0 and out["passed"] is False


def test_a_refusal_judge_without_an_expectation_is_rejected():
    from apis.shared.evals.cases import CaseError

    with pytest.raises(CaseError, match="orient"):
        parse_case({"id": "general_chat.x", "family": "general_chat", "turns": [{"prompt": "q"}],
                    "judge": {"evaluators": ["refusal"]}}, family="general_chat")
