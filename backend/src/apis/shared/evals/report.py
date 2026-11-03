"""Baselines, per-family comparison, and the gate (eval-harness spec §8).

A run directory holds ``meta.json`` and ``results.jsonl`` (one row per
sample, from ``execute.run_sample``). A baseline is the per-case majority
outcome of a promoted run on one model id, committed under
``backend/evals/baselines/``. ``score`` compares a run with the baseline for
its model, family by family, and never pools families.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from apis.shared.evals.calls import summarize_calls
from apis.shared.evals.cases import FAMILIES, JUDGED_FAMILIES, EvalCase
from apis.shared.evals.score import FamilyComparison, compare_family, majority

BASELINE_SCHEMA_VERSION = 1
#: A baseline keeps one answer per case for ``--explain``. The corpus is
#: synthetic (spec D6), so the text is safe to commit; the cap keeps diffs readable.
MAX_BASELINE_ANSWER_CHARS = 600


class ReportError(RuntimeError):
    """The inputs cannot support the comparison asked for."""


def baseline_filename(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model_id) + ".json"


def read_rows(results_path: Path) -> List[Dict[str, Any]]:
    if not results_path.is_file():
        return []
    rows = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    family: str
    samples: int
    passes: int
    passed: bool
    answer: str
    failures: Sequence[str]


def case_outcomes(rows: Iterable[Mapping[str, Any]]) -> Dict[str, CaseOutcome]:
    """Collapse each case's samples to its majority outcome (ties count as wrong).

    The kept answer is from a sample that agrees with the majority, so
    ``--explain`` shows a representative answer rather than an outlier.
    """
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["case_id"])].append(row)

    outcomes: Dict[str, CaseOutcome] = {}
    for case_id, samples in grouped.items():
        samples = sorted(samples, key=lambda r: int(r.get("sample", 0)))
        passes = [bool(r.get("passed")) for r in samples]
        outcome = majority(passes)
        representative = next((r for r in samples if bool(r.get("passed")) == outcome), samples[0])
        outcomes[case_id] = CaseOutcome(
            case_id=case_id,
            family=str(samples[0].get("family") or case_id.split(".", 1)[0]),
            samples=len(samples),
            passes=sum(passes),
            passed=outcome,
            answer=str(representative.get("final_message") or ""),
            failures=list(representative.get("failures") or ()),
        )
    return outcomes


def build_baseline(
    rows: Sequence[Mapping[str, Any]],
    *,
    cases: Sequence[EvalCase],
    meta: Mapping[str, Any],
    judgements: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """A promotable baseline from a complete run, or :class:`ReportError` saying why not.

    Complete means every case in ``cases`` has ``meta["k"]`` samples, all on
    the run's model. A baseline over a partial run would make every missing
    case look new next time, and a regression in it invisible.
    """
    model_id = meta.get("model_id")
    k = int(meta.get("k") or 0)
    if not model_id or k < 1:
        raise ReportError("meta.json must record model_id and k")
    other_models = sorted({str(r.get("model_id")) for r in rows} - {model_id})
    if other_models:
        raise ReportError(f"results mix models: {other_models} besides {model_id}")

    outcomes = case_outcomes(rows)
    short = sorted(c.id for c in cases if outcomes.get(c.id) is None or outcomes[c.id].samples < k)
    if short:
        preview = ", ".join(short[:5]) + (" …" if len(short) > 5 else "")
        raise ReportError(f"{len(short)} case(s) have fewer than k={k} samples: {preview}")

    families = sorted({c.family for c in cases}, key=FAMILIES.index)
    judged = sorted(c.id for c in cases if c.judge.evaluators or c.judge.rubric)
    scores = judge_means(judgements)
    # "Judged" means a judge ran, not that it scored: a tool-level judge on a
    # case that rightly calls no tool returns "nothing to judge".
    attempted = {str(j["case_id"]) for j in judgements}
    unjudged = [cid for cid in judged if cid not in attempted]
    if judgements and unjudged:
        preview = ", ".join(unjudged[:5]) + (" …" if len(unjudged) > 5 else "")
        raise ReportError(f"{len(unjudged)} judged case(s) have no judgements: {preview}; run `judge` first")
    judge_models = sorted({str(j.get("judge_model")) for j in judgements})
    if len(judge_models) > 1:
        raise ReportError(f"judgements mix judge models: {judge_models}")
    baseline: Dict[str, Any] = {
        "schemaVersion": BASELINE_SCHEMA_VERSION,
        "modelId": model_id,
        "k": k,
        "families": families,
        "promotedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {key: meta.get(key) for key in ("startedAt", "target", "gitCommit", "imageTags") if key in meta},
        "cases": {
            c.id: {
                "family": c.family,
                "passed": outcomes[c.id].passed,
                "passes": outcomes[c.id].passes,
                "samples": outcomes[c.id].samples,
                "answer": outcomes[c.id].answer[:MAX_BASELINE_ANSWER_CHARS],
            }
            for c in cases
        },
    }
    if scores:
        baseline["judgeModel"] = judge_models[0]
        baseline["judgeScores"] = {cid: scores[cid] for cid in sorted(scores)}
    return baseline


def load_baseline(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ReportError(f"no baseline at {path}; promote a complete run with `baseline` first")
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("schemaVersion") != BASELINE_SCHEMA_VERSION:
        raise ReportError(f"{path.name}: unsupported baseline schema {baseline.get('schemaVersion')}")
    return baseline


def compare_run(
    baseline: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    model_id: str,
) -> List[FamilyComparison]:
    """One comparison per family the run covers, in report order.

    Refuses a run on a different model: each model has its own baseline (spec
    D10), and comparing across them would report a model change as a regression.
    """
    if baseline.get("modelId") != model_id:
        raise ReportError(
            f"the baseline is for {baseline.get('modelId')}, the run is on {model_id}; "
            "compare against that model's baseline"
        )
    run_outcomes = case_outcomes(rows)
    base_cases: Mapping[str, Mapping[str, Any]] = baseline.get("cases") or {}

    by_family_base: Dict[str, Dict[str, bool]] = defaultdict(dict)
    for case_id, entry in base_cases.items():
        by_family_base[str(entry.get("family"))][case_id] = bool(entry.get("passed"))
    by_family_run: Dict[str, Dict[str, bool]] = defaultdict(dict)
    for case_id, outcome in run_outcomes.items():
        by_family_run[outcome.family][case_id] = outcome.passed

    comparisons = []
    for family in FAMILIES:
        if family not in by_family_run:
            continue
        comparisons.append(
            compare_family(
                family,
                by_family_base.get(family, {}),
                by_family_run[family],
                gating=family not in JUDGED_FAMILIES,
            )
        )
    return comparisons


def format_table(comparisons: Sequence[FamilyComparison]) -> str:
    header = f"{'family':<22} {'n':>3} {'base':>5} {'run':>5} {'+/-':>7} {'p':>7}  gate"
    lines = [header, "-" * len(header)]
    for c in comparisons:
        gate = "FAIL" if c.failed else ("pass" if c.gating else "advisory")
        flips = f"{c.gains}/{c.losses}"
        lines.append(
            f"{c.family:<22} {c.n:>3} {c.base_pass:>5} {c.run_pass:>5} {flips:>7} {c.p_value:>7.3f}  {gate}  {c.reason}"
        )
        if c.missing_in_run:
            lines.append(f"{'':<22} not run: {', '.join(c.missing_in_run)}")
        if c.new_in_run:
            lines.append(f"{'':<22} not in baseline: {', '.join(c.new_in_run)}")
    return "\n".join(lines)


def explain_family(
    family: str,
    baseline: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = 400,
    calls_by_session: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
) -> str:
    """Each case whose outcome flipped, with both answers and the run's failures and tool calls.

    With ``calls_by_session`` (``{session_id: C# rows}``, see ``evals.calls``)
    each flipped case also lists its samples' model calls; a session missing
    from the map prints as unavailable.
    """
    run_outcomes = case_outcomes(r for r in rows if r.get("family") == family)
    base_cases = baseline.get("cases") or {}
    tools_by_case: Dict[str, List[str]] = {}
    samples_by_case: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("family") != family:
            continue
        samples_by_case[str(row["case_id"])].append(row)
        if str(row["case_id"]) not in tools_by_case:
            tools_by_case[str(row["case_id"])] = [str(c.get("name")) for c in row.get("tool_calls") or []]

    blocks = []
    for case_id in sorted(run_outcomes):
        base = base_cases.get(case_id)
        run = run_outcomes[case_id]
        if base is None or bool(base.get("passed")) == run.passed:
            continue
        direction = "LOST" if base.get("passed") else "GAINED"
        lines = [
            f"{direction}  {case_id}  ({run.passes}/{run.samples} samples passed)",
            f"  failures: {'; '.join(run.failures) or '—'}",
            f"  tools:    {', '.join(tools_by_case.get(case_id) or []) or '—'}",
            f"  baseline: {str(base.get('answer') or '')[:max_chars]!r}",
            f"  run:      {run.answer[:max_chars]!r}",
        ]
        if calls_by_session is not None:
            lines.append("  calls:")
            for sample in sorted(samples_by_case[case_id], key=lambda r: int(r.get("sample") or 0)):
                session_id = str(sample.get("session_id") or "")
                outcome = "pass" if sample.get("passed") else "FAIL"
                if session_id in calls_by_session:
                    detail = summarize_calls(calls_by_session[session_id], model_id=sample.get("model_id")).line()
                else:
                    detail = "unavailable"
                lines.append(f"    [{sample.get('sample')}] {outcome}  {detail}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or f"no flipped cases in {family}"


def any_gate_failed(comparisons: Iterable[FamilyComparison]) -> bool:
    return any(c.failed for c in comparisons)


# --------------------------------------------------------------------------- judges


def judge_means(judgements: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, float]]:
    """``{case_id: {evaluator: mean score}}`` over a run's samples, skipping errors and empty verdicts."""
    sums: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for j in judgements:
        if j.get("score") is None:
            continue
        sums[str(j["case_id"])][str(j["evaluator"])].append(float(j["score"]))
    return {
        cid: {ev: round(sum(v) / len(v), 4) for ev, v in sorted(evs.items())}
        for cid, evs in sums.items()
    }


@dataclass(frozen=True)
class JudgeComparison:
    family: str
    evaluator: str
    n: int
    base_mean: float
    run_mean: float

    @property
    def delta(self) -> float:
        return self.run_mean - self.base_mean


def compare_judges(
    baseline: Mapping[str, Any],
    judgements: Sequence[Mapping[str, Any]],
    *,
    judge_model: Optional[str] = None,
) -> List[JudgeComparison]:
    """Paired mean judge scores per (family, evaluator), over the cases judged in both. Advisory only.

    A different judge model is a different scale, so it is refused rather than compared.
    """
    base_scores: Mapping[str, Mapping[str, float]] = baseline.get("judgeScores") or {}
    if not base_scores or not judgements:
        return []
    base_model = baseline.get("judgeModel")
    if judge_model and base_model and judge_model != base_model:
        raise ReportError(f"the baseline was judged by {base_model}, this run by {judge_model}; not comparable")
    run_scores = judge_means(judgements)
    family_of = {str(j["case_id"]): str(j["family"]) for j in judgements}

    pairs: Dict[tuple, List[tuple]] = defaultdict(list)
    for cid, evs in run_scores.items():
        for ev, run_mean in evs.items():
            base_mean = (base_scores.get(cid) or {}).get(ev)
            if base_mean is not None:
                pairs[(family_of[cid], ev)].append((float(base_mean), run_mean))
    out = []
    for (family, ev) in sorted(pairs, key=lambda k: (FAMILIES.index(k[0]) if k[0] in FAMILIES else 99, k[1])):
        values = pairs[(family, ev)]
        out.append(
            JudgeComparison(
                family=family,
                evaluator=ev,
                n=len(values),
                base_mean=sum(b for b, _ in values) / len(values),
                run_mean=sum(r for _, r in values) / len(values),
            )
        )
    return out


def format_judge_table(comparisons: Sequence[JudgeComparison]) -> str:
    header = f"{'family':<22} {'judge':<22} {'n':>3} {'base':>6} {'run':>6} {'delta':>7}"
    lines = [header, "-" * len(header)]
    for c in comparisons:
        lines.append(
            f"{c.family:<22} {c.evaluator:<22} {c.n:>3} {c.base_mean:>6.2f} {c.run_mean:>6.2f} {c.delta:>+7.2f}"
        )
    return "\n".join(lines)
