"""A release's eval results, shaped for publication (eval-harness spec §19).

The golden set is synthetic by design (spec D6): every case, prompt, fixture and
expected answer was written for the harness, so its results contain no user's
data and can be published as they are. This module turns one or more run
directories into the document the docs site renders and the README's headline
table is cut from.

It publishes three pillars per model and family, all measured, none estimated:

- **Quality**: cases passing out of cases run, at the run's ``k`` samples per
  case, and the ids of the cases that failed. Failures are listed, never hidden.
- **Speed**: median and p90 turn time, and the median first token, as the
  deployed Runtime measured them.
- **Cost**: mean dollars per sample as the platform metered them at its catalog
  rates, and the median model calls, output and input tokens per sample.

Cost covers every sample, passing or failing: it is what running the workflow
cost, which is what a reader is pricing. (``efficiency.compare_efficiency``
pairs only passing cases, because it answers a different question.)

**What never leaves.** Session ids, user ids, the deployment's resource prefix,
answers, failure sentences and tool inputs. :func:`assert_publishable` checks a
built document against the run's own ids before anything is written.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from apis.shared.evals.cases import ADVISORY_FAMILIES, FAMILIES, JUDGED_FAMILIES
from apis.shared.evals.efficiency import sample_efficiency, sample_sessions
from apis.shared.evals.report import case_outcomes

SCHEMA_VERSION = 1
#: Top-level keys of a published document, and of each run in it.
DOCUMENT_FIELDS = frozenset({"schemaVersion", "generatedAt", "runs"})
RUN_FIELDS = frozenset({
    "model", "k", "date", "appVersion", "gitCommit", "runtimeImage", "environment", "totals", "families",
})
#: How the environment is described in public: what it is, never what it is called.
ENVIRONMENT = "development deployment, AWS us-west-2, AgentCore Runtime"
_ID_SHAPED = re.compile(r"^[A-Za-z0-9_.:#@/+\-, ]{0,200}$")


class PublishError(ValueError):
    """The document would carry something that must not be published."""


def _p90(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]


def _median(values: Sequence[float]) -> Optional[float]:
    return float(median(values)) if values else None


def _round(value: Optional[float], digits: int = 0) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits) if digits else float(round(value))


def _family_kind(family: str) -> str:
    if family in JUDGED_FAMILIES:
        return "judged"
    return "advisory" if family in ADVISORY_FAMILIES else "gating"


def run_document(
    meta: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    calls: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """One run, as published. ``calls`` is the run's ``calls.json``."""
    outcomes = case_outcomes(rows)
    by_family: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_family[str(row.get("family") or "")].append(row)

    families = []
    totals = {"cases": 0, "passing": 0, "samples": 0, "costUsd": 0.0, "modelCalls": 0.0,
              "outputTokens": 0.0, "inputTokens": 0.0, "measuredSamples": 0}
    all_turns: List[float] = []
    all_first: List[float] = []
    for family in FAMILIES:
        samples = by_family.get(family)
        if not samples:
            continue
        case_ids = sorted({str(r["case_id"]) for r in samples})
        failing = [cid for cid in case_ids if not outcomes[cid].passed]
        measures: Dict[str, List[float]] = defaultdict(list)
        cost = 0.0
        costed = 0
        for row in samples:
            measured = sample_efficiency(row, calls)
            for name, value in measured.items():
                if value is not None:
                    measures[name].append(value)
            sessions = sample_sessions(row)
            if sessions and all("costUsd" in calls.get(s, {}) for s in sessions):
                cost += sum(float(calls[s]["costUsd"]) for s in sessions)
                costed += 1
        families.append({
            "family": family,
            "kind": _family_kind(family),
            "cases": len(case_ids),
            "passing": len(case_ids) - len(failing),
            "failing": failing,
            "samples": len(samples),
            "costPerSampleUsd": round(cost / costed, 5) if costed else None,
            "modelCalls": _round(_median(measures["modelCalls"]), 1),
            "outputTokens": _round(_median(measures["outputTokens"])),
            "inputTokens": _round(_median(measures["inputTokens"])),
            "turnMs": _round(_median(measures["durationMs"])),
            "turnMsP90": _round(_p90(measures["durationMs"])),
            "firstTokenMs": _round(_median(measures["firstTokenMs"])),
        })
        totals["cases"] += len(case_ids)
        totals["passing"] += len(case_ids) - len(failing)
        totals["samples"] += len(samples)
        totals["costUsd"] += cost
        totals["measuredSamples"] += costed
        totals["modelCalls"] += sum(measures["modelCalls"])
        totals["outputTokens"] += sum(measures["outputTokens"])
        totals["inputTokens"] += sum(measures["inputTokens"])
        all_turns.extend(measures["durationMs"])
        all_first.extend(measures["firstTokenMs"])

    image_tags = meta.get("imageTags") if isinstance(meta.get("imageTags"), Mapping) else {}
    return {
        "model": str(meta.get("model_id") or ""),
        "k": int(meta.get("k") or 0),
        "date": str(meta.get("startedAt") or "")[:10],
        "appVersion": str(meta.get("appVersion") or ""),
        "gitCommit": str(meta.get("gitCommit") or ""),
        "runtimeImage": str(image_tags.get("inference-api") or ""),
        "environment": ENVIRONMENT,
        "totals": {
            **{key: int(value) for key, value in totals.items() if key in ("cases", "passing", "samples", "measuredSamples")},
            "costUsd": round(totals["costUsd"], 4),
            "modelCalls": int(totals["modelCalls"]),
            "outputTokens": int(totals["outputTokens"]),
            "inputTokens": int(totals["inputTokens"]),
            "turnMs": _round(_median(all_turns)),
            "firstTokenMs": _round(_median(all_first)),
        },
        "families": families,
    }


def build_document(runs: Iterable[Mapping[str, Any]], generated_at: str) -> Dict[str, Any]:
    """The published document: every run, cheapest model first."""
    ordered = sorted(runs, key=lambda r: (r["totals"]["costUsd"] / max(r["totals"]["samples"], 1), r["model"]))
    return {"schemaVersion": SCHEMA_VERSION, "generatedAt": generated_at, "runs": ordered}


def _strings(value: Any, path: str = "") -> Iterable[tuple]:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            yield f"{path}.{key}", str(key)
            yield from _strings(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _strings(inner, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def assert_publishable(
    document: Mapping[str, Any],
    *,
    known_case_ids: Iterable[str],
    forbidden: Iterable[str] = (),
) -> None:
    """Raise :class:`PublishError` unless the document holds only what may be published.

    ``forbidden`` is every session id, user id and resource prefix the runs
    used: none may appear in any string. Every failing-case entry must be a case
    id from the committed golden set, so nothing else can ride in that list.
    """
    extra = sorted(set(document) - DOCUMENT_FIELDS)
    if extra:
        raise PublishError(f"unexpected top-level field(s): {extra}")
    known = set(known_case_ids)
    for run in document.get("runs") or ():
        extra = sorted(set(run) - RUN_FIELDS)
        if extra:
            raise PublishError(f"unexpected run field(s): {extra}")
        for family in run.get("families") or ():
            unknown = [cid for cid in family.get("failing") or () if cid not in known]
            if unknown:
                raise PublishError(f"failing ids not in the golden set: {unknown}")
    needles = [n for n in dict.fromkeys(str(f) for f in forbidden if f) if len(n) >= 6]
    for path, text in _strings(document):
        if not _ID_SHAPED.match(text):
            raise PublishError(f"{path or 'document'} is not id-shaped; published results carry ids and numbers only")
        for needle in needles:
            if needle in text:
                raise PublishError(f"{path or 'document'} contains one of the runs' own ids or the deployment prefix")


def _fmt_ms(value: Optional[float]) -> str:
    return "—" if value is None else f"{value / 1000:.1f} s"


def headline_markdown(document: Mapping[str, Any]) -> str:
    """The README's headline table: one row per model, from the document alone."""
    lines = [
        "| Model | Cases passing | Cost per task | Median task time | Median first token | Measured |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for run in document.get("runs") or ():
        totals = run["totals"]
        turn, first = totals.get("turnMs"), totals.get("firstTokenMs")
        per_task = totals["costUsd"] / totals["measuredSamples"] if totals["measuredSamples"] else None
        lines.append(
            f"| `{run['model']}` | {totals['passing']} of {totals['cases']} (k={run['k']}) | "
            f"{'—' if per_task is None else f'${per_task:.4f}'} | {_fmt_ms(turn)} | {_fmt_ms(first)} | "
            f"{run['date']}, v{run['appVersion']} |"
        )
    return "\n".join(lines)


def dumps(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"
