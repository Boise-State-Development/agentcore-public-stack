"""The quiet watcher triage shadow readout (docs/specs/quiet-watcher-triage.md §6.5).

Every rule the readout applies, with no I/O: ``scripts/watch_triage_readout.py`` reads the
``SCHEDRUN#`` rows and the catalog's rates and hands them here. One :class:`WatcherReadout` per
watcher:

- **Verdicts** are counted from each run's ``triage`` map (``quiet``, ``escalate``, ``skipped``),
  and reasons likewise. A run with no ``triage`` (the flag was off, or the row predates T.2)
  counts as ``none``.
- **Settle rate** is ``quiet / (quiet + escalate)``: the share of triaged runs tier 1 would have
  settled. Skipped runs are not in it. ``None`` under ``MIN_TRIAGED`` triaged runs, never ``0``.
- **A shadow miss** is a run whose triage said ``quiet`` and whose full run notified (§6.5). The
  readout lists their run ids, so each can be read by hand.
- **Triage time** is the p50 and p95 of ``ms`` over the runs that sent a triage.
- **Cost** is the full runs' own: each row's whole-run tokens priced at the model's catalog rates
  (``CostCalculator.calculate_message_cost``). The triage spends no model call, so it adds none.

Ids and enums only, never content.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence

#: Fewer triaged runs than this and the settle rate is no evidence.
MIN_TRIAGED = 20


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """The nearest-rank ``p``-th percentile (0 < p ≤ 100), or ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil without float drift
    return ordered[int(rank) - 1]


@dataclass
class WatcherReadout:
    label: str
    model_id: Optional[str]
    runs: int = 0
    statuses: Counter = field(default_factory=Counter)
    verdicts: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    triage_ms: List[int] = field(default_factory=list)
    shadow_misses: List[str] = field(default_factory=list)
    tokens: Counter = field(default_factory=Counter)
    cost_usd: Optional[float] = None

    @property
    def triaged(self) -> int:
        return self.verdicts["quiet"] + self.verdicts["escalate"]

    @property
    def settle_rate(self) -> Optional[float]:
        if self.triaged < MIN_TRIAGED:
            return None
        return self.verdicts["quiet"] / self.triaged

    @property
    def cost_per_run(self) -> Optional[float]:
        if self.cost_usd is None or not self.runs:
            return None
        return self.cost_usd / self.runs


def _field(row: Any, name: str, alias: Optional[str] = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(alias or name, row.get(name))
    return getattr(row, name, None)


def summarize(
    label: str,
    model_id: Optional[str],
    rows: Iterable[Any],
    *,
    pricing: Optional[Mapping[str, Any]] = None,
) -> WatcherReadout:
    """One watcher's readout from its ``ScheduleRun`` rows (models or raw items)."""
    from apis.shared.costs.calculator import CostCalculator

    out = WatcherReadout(label=label, model_id=model_id)
    for row in rows:
        out.runs += 1
        status = _field(row, "status") or "unknown"
        out.statuses[status] += 1
        triage = _field(row, "triage") or None
        if not isinstance(triage, Mapping):
            out.verdicts["none"] += 1
        else:
            verdict = str(triage.get("verdict") or "none")
            out.verdicts[verdict] += 1
            out.reasons[str(triage.get("reason") or "none")] += 1
            if verdict in ("quiet", "escalate") and triage.get("ms") is not None:
                out.triage_ms.append(int(triage["ms"]))
            if verdict == "quiet" and status == "notify":
                out.shadow_misses.append(str(_field(row, "run_id", "runId") or "?"))
        for name, alias in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("cache_read_tokens", "cacheReadTokens"),
            ("cache_write_tokens", "cacheWriteTokens"),
        ):
            out.tokens[alias] += int(_field(row, name, alias) or 0)
    if pricing:
        usage = {
            "inputTokens": out.tokens["inputTokens"],
            "outputTokens": out.tokens["outputTokens"],
            "cacheReadInputTokens": out.tokens["cacheReadTokens"],
            "cacheWriteInputTokens": out.tokens["cacheWriteTokens"],
        }
        # Priced run by run would be exact for a model with a long-context card; a watcher's
        # whole run stays far under any threshold, so the total is priced once.
        total, _ = CostCalculator.calculate_message_cost(usage, dict(pricing))
        out.cost_usd = float(total)
    return out


def _counts(counter: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))) or "—"


def _money(value: Optional[float], digits: int = 5) -> str:
    return "—" if value is None else f"${value:.{digits}f}"


def render(readouts: Sequence[WatcherReadout], *, window: str) -> str:
    """A markdown readout: one table row per watcher, then the misses to read by hand."""
    lines = [
        f"# Quiet watcher triage shadow readout ({window})",
        "",
        "| Watcher | Model | Runs | Outcomes | Triage verdicts | Reasons | Settle rate | Triage ms p50 / p95 | Shadow misses | Cost | Per run |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in readouts:
        rate = "—" if r.settle_rate is None else f"{r.settle_rate:.0%} of {r.triaged}"
        p50, p95 = percentile(r.triage_ms, 50), percentile(r.triage_ms, 95)
        ms = "—" if p50 is None else f"{int(p50):,} / {int(p95):,}"
        lines.append(
            f"| {r.label} | {r.model_id or 'default'} | {r.runs} | {_counts(r.statuses)} | {_counts(r.verdicts)} "
            f"| {_counts(r.reasons)} | {rate} | {ms} | {len(r.shadow_misses)} | {_money(r.cost_usd, 4)} "
            f"| {_money(r.cost_per_run)} |"
        )
    total = sum(r.cost_usd or 0.0 for r in readouts)
    lines += ["", f"Full runs' model cost in the window: {_money(total, 4)}. The triage itself makes no model call."]
    misses = [(r.label, run_id) for r in readouts for run_id in r.shadow_misses]
    lines += ["", "## Shadow misses", ""]
    if misses:
        lines += [f"- {label}: {run_id}" for label, run_id in misses]
        lines += ["", "Read each by hand: a change between the triage and the full run is the one expected false alarm (§6.5)."]
    else:
        lines.append("None.")
    lines += ["", f"A settle rate under {MIN_TRIAGED} triaged runs is shown as `—`: no evidence, never 0%."]
    return "\n".join(lines) + "\n"


__all__ = ["MIN_TRIAGED", "WatcherReadout", "percentile", "render", "summarize"]
