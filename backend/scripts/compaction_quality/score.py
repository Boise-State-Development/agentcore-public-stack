"""Exact-match scoring and paired statistics.

No model judges anything in slice 1: every task has an authored answer. A task
is correct when the answer contains an expected value (word-bounded, after
normalization) and contains none of the forbidden ones — so a superseded task
answered with the old value, or with both values, fails.

Arms are compared **paired, per family** with an exact McNemar test, and ``n``
is reported with every number. There is deliberately no pooled score across
families (response-feedback spec §9: every number is a comparison between arms
with its ``n``, or it is not reported).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

# One definition for every harness: these moved to the shared eval harness
# (``apis/shared/evals/score.py``) and are re-exported here for the callers
# that already import them from this module.
from apis.shared.evals.score import contains_any, is_correct, mcnemar_exact, normalize  # noqa: F401


def task_outcomes(results: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, int, str], Dict[str, Any]]:
    """Collapse k samples per (arm, variant, plant) to a majority-vote outcome."""
    grouped: Dict[Tuple[str, int, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in results:
        grouped[(row["arm"], int(row["variant"]), row["plantId"])].append(row)
    outcomes = {}
    for key, rows in grouped.items():
        hits = sum(1 for r in rows if r["correct"])
        outcomes[key] = {
            "family": rows[0]["family"],
            "correct": hits * 2 > len(rows),
            "hits": hits,
            "k": len(rows),
            "statementRetained": rows[0].get("statementRetained"),
        }
    return outcomes


def compare(
    outcomes: Dict[Tuple[str, int, str], Dict[str, Any]],
    arm: str,
    baseline: str,
    *,
    family: Optional[str] = None,
) -> Dict[str, Any]:
    """Paired comparison of ``arm`` against ``baseline`` on the tasks both ran."""
    b = c = both = neither = 0
    for (a, variant, plant_id), outcome in outcomes.items():
        if a != arm or (family and outcome["family"] != family):
            continue
        base = outcomes.get((baseline, variant, plant_id))
        if base is None:
            continue
        mine, theirs = outcome["correct"], base["correct"]
        if mine and theirs:
            both += 1
        elif not mine and not theirs:
            neither += 1
        elif theirs and not mine:
            b += 1  # baseline right, arm wrong: a loss for the arm
        else:
            c += 1
    n = b + c + both + neither
    return {
        "arm": arm,
        "baseline": baseline,
        "family": family or "all",
        "n": n,
        "armAccuracy": (both + c) / n if n else None,
        "baselineAccuracy": (both + b) / n if n else None,
        "lossesVsBaseline": b,
        "winsVsBaseline": c,
        "pValue": mcnemar_exact(b, c),
    }


def report(results: List[Dict[str, Any]], baseline: str = "full") -> Dict[str, Any]:
    outcomes = task_outcomes(results)
    arms = sorted({a for a, _, _ in outcomes} - {baseline})
    families = sorted({o["family"] for o in outcomes.values()})
    rows = [compare(outcomes, arm, baseline, family=fam) for arm in arms for fam in families]
    # Where the loss sits: tasks whose statement turn was sliced away (the
    # summary had to carry it) versus kept (the model had the original text).
    split = []
    for arm in arms:
        for retained in (True, False):
            subset = {
                k: v for k, v in outcomes.items()
                if k[0] == arm and v.get("statementRetained") is retained
            }
            n = len(subset)
            split.append({
                "arm": arm,
                "statementRetained": retained,
                "n": n,
                "accuracy": (sum(1 for v in subset.values() if v["correct"]) / n) if n else None,
            })
    return {"baseline": baseline, "byFamily": rows, "byRetention": split}
