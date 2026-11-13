"""What an eval case cost to answer, beside whether it was answered right.

``score`` says whether a run kept the baseline's quality. It could not say
what the run paid for it, so a change that makes every answer twice as slow or
twice as large passed as long as the answers stayed right, and a change meant
to make turns cheaper had nowhere to prove it kept quality while doing so.

This reduces each sample to five numbers a result row already carries, takes
each case's median over its samples, and compares families between a run and a
baseline over the cases that **pass in both**. A case that fails is left out
on purpose: a wrong answer is often a short one, and counting it would report
a regression in quality as a gain in efficiency.

Always advisory. Timing depends on the day's load and a nightly run has one
sample per case, so nothing here can fail the gate; a family is only marked as
moved when enough cases back it and the change is large.

Tokens, not dollars: concurrent samples share cache writes, so two runs of the
same case can be billed differently for the same work
(``apis.shared.evals.calls.CallSummary.input_tokens`` makes the same choice).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from apis.shared.evals.cases import FAMILIES

#: The measures, in report order: ``(name, column header)``.
METRICS: Tuple[Tuple[str, str], ...] = (
    ("outputTokens", "out tok"),
    ("inputTokens", "in tok"),
    ("toolCalls", "tools"),
    ("durationMs", "dur ms"),
    ("firstTokenMs", "ttft ms"),
)
METRIC_NAMES = tuple(name for name, _ in METRICS)
INPUT_USAGE_KEYS = ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")

#: A family's change on one measure is called out only with at least this many
#: paired cases behind it and at least this large a relative change.
MIN_PAIRED_CASES = 5
MOVED_THRESHOLD = 0.20


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def sample_efficiency(row: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """One sample's measures, from its result row. ``None`` where the row cannot say.

    - ``outputTokens`` / ``inputTokens``: the sample's token totals; input is
      uncached, cache-read and cache-written together, what was sent whichever
      way it was billed.
    - ``toolCalls``: tool calls over every turn of the sample.
    - ``durationMs``: every turn's duration as the Runtime measured it, summed;
      ``None`` if any turn has none (a cut stream), never a partial sum.
    - ``firstTokenMs``: the first turn's first token.
    """
    usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
    turns = row.get("turns") if isinstance(row.get("turns"), list) else []
    timing = row.get("timing") if isinstance(row.get("timing"), list) else []

    inputs = [_number(usage.get(key)) for key in INPUT_USAGE_KEYS]
    durations = [_number(t.get("turnDurationMs")) if isinstance(t, Mapping) else None for t in timing]
    first = timing[0] if timing and isinstance(timing[0], Mapping) else {}
    return {
        "outputTokens": _number(usage.get("outputTokens")),
        "inputTokens": sum(v for v in inputs if v is not None) if any(v is not None for v in inputs) else None,
        "toolCalls": (
            float(sum(len(t.get("tool_trace") or ()) for t in turns if isinstance(t, Mapping))) if turns else None
        ),
        "durationMs": sum(durations) if durations and all(d is not None for d in durations) else None,  # type: ignore[arg-type]
        "firstTokenMs": _number(first.get("firstTokenMs")),
    }


def case_efficiency(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, float]]:
    """``{case_id: {metric: median over the case's samples}}``.

    A metric no sample of a case could report is absent for that case, so a
    comparison pairs only what both sides measured.
    """
    values: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        for name, value in sample_efficiency(row).items():
            if value is not None:
                values[str(row["case_id"])][name].append(value)
    return {
        case_id: {name: round(float(median(samples)), 1) for name, samples in metrics.items()}
        for case_id, metrics in values.items()
    }


@dataclass(frozen=True)
class MetricComparison:
    """One measure for one family, over the cases both sides passed and measured."""

    n: int
    base: float
    run: float

    @property
    def delta(self) -> Optional[float]:
        """Relative change, ``None`` when the baseline is zero."""
        return (self.run - self.base) / self.base if self.base else None

    @property
    def moved(self) -> bool:
        delta = self.delta
        return self.n >= MIN_PAIRED_CASES and delta is not None and abs(delta) >= MOVED_THRESHOLD


@dataclass(frozen=True)
class FamilyEfficiency:
    family: str
    #: Cases that pass in both the baseline and the run.
    paired: int
    metrics: Mapping[str, MetricComparison]

    def to_dict(self) -> Dict[str, Any]:
        """Numbers only, for ``score --json`` and the ledger row."""
        return {
            "paired": self.paired,
            **{
                name: {"n": m.n, "base": round(m.base, 1), "run": round(m.run, 1)}
                for name, m in self.metrics.items()
            },
        }


def compare_efficiency(
    baseline: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    passed: Mapping[str, bool],
) -> List[FamilyEfficiency]:
    """Per family, each measure summed over the cases that pass in both and carry it in both.

    ``passed`` is the run's majority outcome per case (``report.case_outcomes``).
    A baseline promoted before efficiency was recorded has no numbers; its
    families come back with ``paired`` set and no metrics, which prints as
    "no baseline efficiency" rather than as no change.
    """
    run = case_efficiency(rows)
    base_cases: Mapping[str, Mapping[str, Any]] = baseline.get("cases") or {}
    family_of = {str(row["case_id"]): str(row.get("family") or "") for row in rows}

    paired: Dict[str, List[str]] = defaultdict(list)
    for case_id, family in family_of.items():
        entry = base_cases.get(case_id)
        if entry is not None and entry.get("passed") and passed.get(case_id):
            paired[family].append(case_id)

    comparisons = []
    for family in FAMILIES:
        if family not in set(family_of.values()):
            continue
        metrics: Dict[str, MetricComparison] = {}
        for name in METRIC_NAMES:
            pairs = [
                (float(base_value), run[case_id][name])
                for case_id in paired.get(family, ())
                if (base_value := _number((base_cases[case_id].get("efficiency") or {}).get(name))) is not None
                and name in run.get(case_id, {})
            ]
            if pairs:
                metrics[name] = MetricComparison(
                    n=len(pairs), base=sum(b for b, _ in pairs), run=sum(r for _, r in pairs)
                )
        comparisons.append(FamilyEfficiency(family=family, paired=len(paired.get(family, ())), metrics=metrics))
    return comparisons


def _cell(metric: Optional[MetricComparison]) -> str:
    if metric is None:
        return "—"
    delta = metric.delta
    change = "n/a" if delta is None else f"{delta:+.0%}"
    return f"{metric.run / metric.n:,.0f} ({change}{'*' if metric.moved else ''})"


def format_efficiency_table(comparisons: Sequence[FamilyEfficiency]) -> str:
    """Mean per paired case in the run, and the change against the baseline.

    ``*`` marks a change of :data:`MOVED_THRESHOLD` or more with at least
    :data:`MIN_PAIRED_CASES` cases behind it.
    """
    header = f"{'family':<22} {'n':>3}  " + "  ".join(f"{label:>18}" for _, label in METRICS)
    lines = [header, "-" * len(header)]
    for c in comparisons:
        if not c.metrics:
            lines.append(f"{c.family:<22} {c.paired:>3}  no baseline efficiency; promote a baseline to compare")
            continue
        cells = "  ".join(f"{_cell(c.metrics.get(name)):>18}" for name in METRIC_NAMES)
        lines.append(f"{c.family:<22} {c.paired:>3}  {cells}")
    return "\n".join(lines)


def moved(comparisons: Iterable[FamilyEfficiency]) -> List[str]:
    """``family metric +NN%`` for every marked change, for a one-line summary."""
    found = []
    for c in comparisons:
        for name in METRIC_NAMES:
            metric = c.metrics.get(name)
            if metric is not None and metric.moved:
                found.append(f"{c.family} {name} {metric.delta:+.0%}")
    return found


def run_efficiency(rows: Sequence[Mapping[str, Any]], passed: Mapping[str, bool]) -> Dict[str, Dict[str, Any]]:
    """The run's own numbers per family, with no baseline: ``{family: {metric: {n, sum}}}``
    over the cases the run passed. Two runs of the same model and track compare
    on these when the baseline has no efficiency yet.
    """
    run = case_efficiency(rows)
    totals: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"n": 0, "sum": 0.0}))
    for row in rows:
        case_id = str(row["case_id"])
        if not passed.get(case_id) or case_id not in run:
            continue
        family = str(row.get("family") or "")
        for name, value in run.pop(case_id).items():
            slot = totals[family][name]
            slot["n"] += 1
            slot["sum"] = round(slot["sum"] + value, 1)
    return {family: {name: dict(slot) for name, slot in metrics.items()} for family, metrics in totals.items()}
