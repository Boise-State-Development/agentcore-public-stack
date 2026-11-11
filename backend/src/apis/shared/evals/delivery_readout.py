"""The ``attachment_delivery`` readout: quality, cost and speed per case (unified-attachment-delivery §8.5).

``score`` says whether a family regressed against its baseline. This says
what a delivery run cost and how long the user waited, per case: the pass
rate over its samples, dollars per sample, first token and turn duration,
the document tokens that were in context, the second turn's cache status,
and every cut and offload by its reason. Cost and the context facts come
from each sample's ``C#`` rows (``calls.summarize_calls``); timing from the
result rows the runner wrote (``execute.turn_timing``).

Content-free like its inputs: case ids, numbers and enum values. A sample's
session id is read to find its rows and never written out.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from apis.shared.evals.calls import CallSummary, summarize_calls


@dataclass
class CaseReadout:
    case_id: str
    #: The arm and ceiling the case ran under, and the model (``execute.run_sample``'s row).
    arm: Optional[str] = None
    ceiling: Optional[str] = None
    model_id: Optional[str] = None
    samples: int = 0
    passes: int = 0
    #: Mean ``C#`` cost per sample, over the samples whose rows were read.
    cost_per_sample: Optional[float] = None
    #: Medians over samples, of the first turn (the turn that carries the files).
    first_token_ms: Optional[int] = None
    client_first_token_ms: Optional[int] = None
    turn_duration_ms: Optional[int] = None
    upload_ms: Optional[int] = None
    #: Median of each sample's peak inline document tokens.
    document_tokens: Optional[int] = None
    #: How the files reached the model, as the rows show it (``delivery_label``).
    delivery: Counter = field(default_factory=Counter)
    #: The second turn's first-call ``cacheStatus``, counted over samples.
    second_turn_cache: Counter = field(default_factory=Counter)
    #: Multi-turn cases: the last turn's first token and duration (medians),
    #: and the fifth turn's ``cacheStatus`` (the turn a paced case sends after
    #: the cache TTL).
    last_first_token_ms: Optional[int] = None
    last_turn_duration_ms: Optional[int] = None
    fifth_turn_cache: Counter = field(default_factory=Counter)
    cuts: Counter = field(default_factory=Counter)
    offloads: Counter = field(default_factory=Counter)
    #: Why failed samples failed: the non-completed status, else "wrong answer".
    failures: Counter = field(default_factory=Counter)
    #: Median input tokens a sample sent over all its calls (``CallSummary.input_tokens``).
    input_tokens: Optional[int] = None
    #: The last turn's ``document_read`` calls by mode, over samples, and their
    #: median count: whether the model searched, grepped or paged (phase 5).
    last_turn_reads: Counter = field(default_factory=Counter)
    last_turn_read_calls: Optional[int] = None

    @property
    def pass_rate(self) -> float:
        return self.passes / self.samples if self.samples else 0.0

    @property
    def cuts_per_sample(self) -> float:
        return sum(self.cuts.values()) / self.samples if self.samples else 0.0

    @property
    def offloads_per_sample(self) -> float:
        return sum(self.offloads.values()) / self.samples if self.samples else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "caseId": self.case_id,
            "arm": self.arm,
            "ceiling": self.ceiling,
            "modelId": self.model_id,
            "samples": self.samples,
            "passes": self.passes,
            "passRate": round(self.pass_rate, 4),
            "costPerSampleUsd": round(self.cost_per_sample, 6) if self.cost_per_sample is not None else None,
            "firstTokenMs": self.first_token_ms,
            "clientFirstTokenMs": self.client_first_token_ms,
            "turnDurationMs": self.turn_duration_ms,
            "uploadMs": self.upload_ms,
            "documentTokens": self.document_tokens,
            "delivery": dict(self.delivery),
            "secondTurnCache": dict(self.second_turn_cache),
            "lastFirstTokenMs": self.last_first_token_ms,
            "lastTurnDurationMs": self.last_turn_duration_ms,
            "fifthTurnCache": dict(self.fifth_turn_cache),
            "cutsPerSample": round(self.cuts_per_sample, 3),
            "offloadsPerSample": round(self.offloads_per_sample, 3),
            "cuts": dict(self.cuts),
            "offloads": dict(self.offloads),
            "failures": dict(self.failures),
            "inputTokens": self.input_tokens,
            "lastTurnReads": dict(self.last_turn_reads),
            "lastTurnReadCalls": self.last_turn_read_calls,
        }


def delivery_label(summary: CallSummary) -> str:
    """One word for how a sample's files reached the model.

    The planner's ``attachmentPlan`` when the rows carry one; before it, the
    shape the context took: ``document_read`` slices, a digest, inline
    document tokens, or none of those (a tool read it, or nothing did).
    """
    if summary.attachment_plan:
        return "+".join(sorted({f.mode for f in summary.attachment_plan}))
    if summary.document_reads is not None and summary.document_reads.calls:
        return "on_demand"
    if summary.document_digests:
        return "digest"
    if summary.document_tokens:
        return "inline"
    return "none"


def _median(values: Sequence[Optional[float]]) -> Optional[int]:
    present = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return int(statistics.median(present)) if present else None


def _turn(row: Mapping[str, Any], key: str, index: int) -> Optional[int]:
    timing = row.get("timing")
    if isinstance(timing, list) and timing and -len(timing) <= index < len(timing):
        entry = timing[index]
        value = entry.get(key) if isinstance(entry, Mapping) else None
        return value if isinstance(value, int) else None
    return None


def _first_turn(row: Mapping[str, Any], key: str) -> Optional[int]:
    return _turn(row, key, 0)


def read_mode(call_input: Any) -> str:
    """Which ``document_read`` mode one call used: ``query``, ``pattern``,
    ``page_range``, ``offset`` or ``list`` (no argument names a document part)."""
    args = call_input if isinstance(call_input, Mapping) else {}
    for mode in ("query", "pattern", "page_range"):
        if str(args.get(mode) or "").strip():
            return mode
    if args.get("offset"):
        return "offset"
    return "read" if args.get("upload_id") else "list"


def _last_turn_reads(row: Mapping[str, Any]) -> List[str]:
    turns = row.get("turns")
    last = turns[-1] if isinstance(turns, list) and turns and isinstance(turns[-1], Mapping) else {}
    trace = last.get("tool_trace") if isinstance(last.get("tool_trace"), list) else []
    return [read_mode(e.get("input")) for e in trace if isinstance(e, Mapping) and e.get("name") == "document_read"]


def delivery_readout(
    rows: Sequence[Mapping[str, Any]],
    calls_by_session: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
) -> List[CaseReadout]:
    """One :class:`CaseReadout` per case in ``rows``, in first-seen order."""
    by_case: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_case[str(row.get("case_id"))].append(row)
    calls_by_session = calls_by_session or {}

    readouts = []
    for case_id, samples in by_case.items():
        out = CaseReadout(
            case_id=case_id, samples=len(samples), arm=samples[0].get("arm"), ceiling=samples[0].get("ceiling"),
            model_id=samples[0].get("model_id"),
        )
        costs, tokens, inputs, read_counts = [], [], [], []
        for row in samples:
            reads = _last_turn_reads(row)
            out.last_turn_reads.update(reads)
            read_counts.append(len(reads))
            if row.get("passed"):
                out.passes += 1
            else:
                status = str(row.get("status") or "")
                out.failures[status if status and status != "completed" else "wrong answer"] += 1
            calls = calls_by_session.get(str(row.get("session_id") or ""))
            if calls is None:
                continue
            summary = summarize_calls(calls, model_id=row.get("model_id"))
            if summary.calls:
                costs.append(summary.cost_usd)
                inputs.append(summary.input_tokens)
            tokens.append(summary.document_tokens)
            out.delivery[delivery_label(summary)] += 1
            if len(summary.turn_cache_statuses) > 1:
                out.second_turn_cache[summary.turn_cache_statuses[1]] += 1
            if len(summary.turn_cache_statuses) > 4:
                out.fifth_turn_cache[summary.turn_cache_statuses[4]] += 1
            out.cuts.update(dict(summary.cuts))
            out.offloads.update(dict(summary.offloads))
        out.cost_per_sample = sum(costs) / len(costs) if costs else None
        out.document_tokens = _median(tokens)
        out.input_tokens = _median(inputs)
        out.last_turn_read_calls = _median(read_counts) if any(read_counts) else None
        out.first_token_ms = _median([_first_turn(r, "firstTokenMs") for r in samples])
        out.client_first_token_ms = _median([_first_turn(r, "clientFirstTokenMs") for r in samples])
        out.turn_duration_ms = _median([_first_turn(r, "turnDurationMs") for r in samples])
        out.upload_ms = _median([r.get("upload_ms") for r in samples])
        if any(len(r.get("timing") or []) > 1 for r in samples):
            out.last_first_token_ms = _median([_turn(r, "firstTokenMs", -1) for r in samples])
            out.last_turn_duration_ms = _median([_turn(r, "turnDurationMs", -1) for r in samples])
        readouts.append(out)
    return readouts


def _ms(value: Optional[int]) -> str:
    return "—" if value is None else f"{value / 1000:.1f}s"


def _counts(counter: Counter) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(counter.items())) or "—"


def format_delivery_readout(readouts: Sequence[CaseReadout], *, title: Optional[str] = None) -> str:
    """A Markdown table, one row per case, then the run's totals."""
    lines = [f"### {title}", ""] if title else []
    lines += [
        "| Case | Arm | Ceiling | Pass | $/sample | Input tokens/sample | First token | Turn | "
        "Last turn (TTFT / duration) | Last-turn reads | Doc tokens | "
        "Delivery | Turn-2 cache | Turn-5 cache | Cuts/sample | Offloads/sample | Failures |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in readouts:
        name = r.case_id.split(".", 1)[-1]
        cost = "—" if r.cost_per_sample is None else f"${r.cost_per_sample:.4f}"
        doc_tokens = "—" if r.document_tokens is None else f"{r.document_tokens:,}"
        cuts = f"{r.cuts_per_sample:.1f}" + (f" ({_counts(r.cuts)})" if r.cuts else "")
        offloads = f"{r.offloads_per_sample:.1f}" + (f" ({_counts(r.offloads)})" if r.offloads else "")
        last = (
            "—" if r.last_first_token_ms is None and r.last_turn_duration_ms is None
            else f"{_ms(r.last_first_token_ms)} / {_ms(r.last_turn_duration_ms)}"
        )
        inputs = "—" if r.input_tokens is None else f"{r.input_tokens:,}"
        reads = "—" if r.last_turn_read_calls is None else f"{r.last_turn_read_calls} ({_counts(r.last_turn_reads)})"
        lines.append(
            f"| {name} | {r.arm or '—'} | {r.ceiling or '—'} | {r.passes}/{r.samples} | {cost} | {inputs} | "
            f"{_ms(r.first_token_ms)} | {_ms(r.turn_duration_ms)} | {last} | {reads} | "
            f"{doc_tokens} | {_counts(r.delivery)} | {_counts(r.second_turn_cache)} | "
            f"{_counts(r.fifth_turn_cache)} | {cuts} | {offloads} | {_counts(r.failures)} |"
        )
    samples = sum(r.samples for r in readouts)
    passes = sum(r.passes for r in readouts)
    priced = [r.cost_per_sample * r.samples for r in readouts if r.cost_per_sample is not None]
    lines += [
        "",
        f"{passes}/{samples} samples passed across {len(readouts)} cases"
        + (f"; ${sum(priced):.2f} in all" if priced else "")
        + ".",
    ]
    return "\n".join(lines)


__all__ = ["CaseReadout", "delivery_label", "delivery_readout", "format_delivery_readout", "read_mode"]
