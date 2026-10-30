"""The judge seam and its strands-agents-evals adapter (eval-harness spec D2, D11).

A judge scores one scrubbed, ``Session``-shaped sample with named evaluators
and returns numbers. It is advisory: no family gates on a judge (spec D5).

``StrandsEvalsJudge`` imports ``strands_evals`` inside its methods, never at
module import, so ``apis.shared`` stays importable in every service image (the
package is a ``dev`` extra), and a breaking upstream release is this one file
to fix. Judging runs locally on Bedrock through the package's evaluators: the
session is built by ``session_mapper`` and scrubbed by ``scrub`` first, so a
judge never reads document bodies and no span ingestion is involved.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

#: Our evaluator names (the ones a case may list) → strands-evals class names.
#: Every one of these shares the constructor ``(version, model, system_prompt,
#: name)`` in strands-agents-evals 1.7.0.
EVALUATOR_CLASSES: Dict[str, str] = {
    "helpfulness": "HelpfulnessEvaluator",
    "faithfulness": "FaithfulnessEvaluator",
    "instruction_following": "InstructionFollowingEvaluator",
    "tool_selection": "ToolSelectionAccuracyEvaluator",
    "refusal": "RefusalEvaluator",
    "conciseness": "ConcisenessEvaluator",
}
#: The verdict name a case's ``judge.rubric`` is reported under (``OutputEvaluator``).
RUBRIC_EVALUATOR = "rubric"
DEFAULT_JUDGE_MODEL = "us.anthropic.claude-sonnet-4-6"
MAX_REASON_CHARS = 600


@dataclass(frozen=True)
class JudgeVerdict:
    """One evaluator's score for one sample. ``reason`` stays in the run directory, never in a baseline.

    ``score`` is ``None`` when the evaluator had nothing to judge (a tool-level
    judge on a turn with no tool calls) or failed; ``error`` says which.
    """

    evaluator: str
    score: Optional[float]
    passed: Optional[bool]
    label: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluator": self.evaluator,
            "score": self.score,
            "passed": self.passed,
            "label": self.label,
            "reason": self.reason,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class Judge(Protocol):
    def judge(
        self,
        session: Mapping[str, Any],
        *,
        evaluators: Sequence[str],
        rubric: Optional[str] = None,
    ) -> List[JudgeVerdict]:
        """Score one scrubbed session with each named evaluator (and the rubric, when given)."""


def last_turn(session: Mapping[str, Any]) -> tuple:
    """``(user_prompt, agent_response)`` of the session's last agent span."""
    for trace in reversed(list(session.get("traces") or [])):
        for span in reversed(list(trace.get("spans") or [])):
            if span.get("span_type") == "invoke_agent":
                return str(span.get("user_prompt") or ""), str(span.get("agent_response") or "")
    return "", ""


def summarize_outputs(evaluator: str, outputs: Sequence[Any]) -> JudgeVerdict:
    """Collapse an evaluator's outputs (one per tool call for a tool-level judge) to one verdict."""
    if not outputs:
        return JudgeVerdict(evaluator=evaluator, score=None, passed=None, error="nothing to judge")
    scores = [float(o.score) for o in outputs]
    reasons = [str(o.reason) for o in outputs if getattr(o, "reason", None)]
    labels = [str(o.label) for o in outputs if getattr(o, "label", None)]
    return JudgeVerdict(
        evaluator=evaluator,
        score=sum(scores) / len(scores),
        passed=all(bool(o.test_pass) for o in outputs),
        label=labels[-1] if labels else None,
        reason=(" | ".join(reasons))[:MAX_REASON_CHARS] or None,
        metadata={"outputs": len(outputs)},
    )


class StrandsEvalsJudge:
    """Judge through strands-agents-evals evaluators on a Bedrock model.

    ``import_module`` is the seam tests use to supply a stand-in package.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_JUDGE_MODEL,
        *,
        import_module: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        self.model_id = model_id
        self._import = import_module

    def _evaluator(self, name: str, rubric: Optional[str]) -> Any:
        evaluators = self._import("strands_evals.evaluators")
        if name == RUBRIC_EVALUATOR:
            return evaluators.OutputEvaluator(rubric=rubric, model=self.model_id, include_inputs=True)
        return getattr(evaluators, EVALUATOR_CLASSES[name])(model=self.model_id)

    def judge(
        self,
        session: Mapping[str, Any],
        *,
        evaluators: Sequence[str],
        rubric: Optional[str] = None,
    ) -> List[JudgeVerdict]:
        unknown = sorted(set(evaluators) - set(EVALUATOR_CLASSES))
        if unknown:
            raise ValueError(f"unknown evaluator(s) {unknown}; known: {sorted(EVALUATOR_CLASSES)}")

        trace_types = self._import("strands_evals.types.trace")
        evaluation = self._import("strands_evals.types.evaluation")
        prompt, answer = last_turn(session)
        data = evaluation.EvaluationData(
            input=prompt,
            actual_output=answer,
            actual_trajectory=trace_types.Session.model_validate(dict(session)),
            name=str(session.get("session_id") or ""),
        )

        names = list(evaluators) + ([RUBRIC_EVALUATOR] if rubric else [])
        verdicts: List[JudgeVerdict] = []
        for name in names:
            try:
                outputs = self._evaluator(name, rubric).evaluate(data)
            except Exception as exc:  # noqa: BLE001 — one failed judge must not lose the others
                verdicts.append(
                    JudgeVerdict(evaluator=name, score=None, passed=None, error=f"{type(exc).__name__}: {exc}"[:300])
                )
                continue
            verdicts.append(summarize_outputs(name, outputs))
        return verdicts


def judge_row(row: Mapping[str, Any], *, evaluators: Sequence[str], rubric: Optional[str], judge: Judge,
              judge_model: str) -> List[Dict[str, Any]]:
    """Scrub one result row's session, judge it, and return one judgement row per evaluator.

    The scrub happens here, not in the judge, so no judge implementation can
    read document bodies by forgetting to call it (spec §6.4).
    """
    from apis.shared.evals.scrub import scrub_session

    verdicts = judge.judge(scrub_session(row["session"]), evaluators=evaluators, rubric=rubric)
    return [
        {
            "case_id": row["case_id"],
            "family": row["family"],
            "sample": row["sample"],
            "judge_model": judge_model,
            **verdict.to_dict(),
        }
        for verdict in verdicts
    ]
