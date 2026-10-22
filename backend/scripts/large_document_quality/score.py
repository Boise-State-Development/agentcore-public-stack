"""Scoring: per class, Tier 1 against Tier 1 + Tier 2, paired, with ``n`` on every row.

A question's outcome on an arm is the majority of its ``k`` samples (ties count
as wrong). Arms are compared per document class with an exact McNemar test on
the discordant questions, the same statistic the compaction harness uses. There
is no pooled score across classes: the spec's decision is per class ("ship Tier
2 if it measurably beats Tier 1 on the scanned and layout classes").

The operational columns say what each arm cost to get there: tool calls per
turn, how often the model used ``query``, tokens, dollars and seconds.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Dict, Iterable, List, Tuple

from compaction_quality.score import mcnemar_exact

CLASS_OF_DOC = {"manual": "text", "scanned": "scanned", "brochure": "layout", "handbook": "docx"}
CLASSES = ("text", "scanned", "layout", "docx")


def outcomes(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], bool]:
    """``(arm, qid) -> correct`` by majority of samples."""
    grouped: Dict[Tuple[str, str], List[bool]] = defaultdict(list)
    for row in rows:
        grouped[(row["arm"], row["qid"])].append(bool(row["correct"]))
    return {key: sum(v) * 2 > len(v) for key, v in grouped.items()}


#: The comparisons reported, when both arms are in the results.
PAIRS = (("tier1", "tier2"), ("tier1", "tier2-guided"), ("tier2", "tier2-guided"))


def compare(rows: List[Dict[str, Any]], *, control: str = "tier1", treatment: str = "tier2") -> List[Dict[str, Any]]:
    """One row per class (and ``wording`` within it): accuracy per arm, discordant counts, p."""
    meta = {r["qid"]: (CLASS_OF_DOC.get(r["doc"], r["doc"]), r["wording"]) for r in rows}
    result = outcomes(rows)
    table: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for qid, (cls, wording) in meta.items():
        if (control, qid) in result and (treatment, qid) in result:
            groups[(cls, "all")].append(qid)
            groups[(cls, wording)].append(qid)
    order = {c: i for i, c in enumerate(CLASSES)}
    for (cls, wording), qids in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[0][1] != "all", kv[0][1])):
        a = sum(result[(control, q)] for q in qids)
        b = sum(result[(treatment, q)] for q in qids)
        only_control = sum(result[(control, q)] and not result[(treatment, q)] for q in qids)
        only_treatment = sum(result[(treatment, q)] and not result[(control, q)] for q in qids)
        table.append({
            "control": control, "treatment": treatment, "class": cls, "wording": wording, "n": len(qids),
            control: a, treatment: b, "controlCorrect": a, "treatmentCorrect": b,
            "onlyControl": only_control, "onlyTreatment": only_treatment,
            "p": mcnemar_exact(only_control, only_treatment),
        })
    return table


def operations(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per arm and class: turns, tool calls, query use, tokens, dollars, seconds."""
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["arm"], CLASS_OF_DOC.get(row["doc"], row["doc"]))].append(row)
    out = []
    for (arm, cls), group in sorted(grouped.items()):
        costs = [r["costUsd"] for r in group if r.get("costUsd") is not None]
        out.append({
            "arm": arm, "class": cls, "turns": len(group),
            "errors": sum(1 for r in group if r.get("error")),
            "noAnswerLine": sum(1 for r in group if r.get("answerLine") is None),
            "medianToolCalls": median(len(r["modes"]) for r in group),
            "queryTurns": sum(1 for r in group if "query" in r["modes"]),
            "pageReadTurns": sum(1 for r in group if "page_range" in r["modes"]),
            "medianInputTokens": median(r["usage"].get("inputTokens", 0) for r in group),
            "usdPerTurn": (sum(costs) / len(costs)) if costs else None,
            "medianSeconds": median(r["seconds"] for r in group),
        })
    return out
