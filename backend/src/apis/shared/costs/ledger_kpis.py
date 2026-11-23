"""The release ledger's derived KPIs (docs/specs/release-ledger.md §6.2, D10).

This is the **only** place a ledger KPI is computed. The admin API, the
Releases tab and ``scripts/release_readout.py`` all read ``LEDGER#`` rows and
pass them through here, so a page, a script and a model can never disagree
about a number.

Pure and stdlib-only: rows in, numbers out. A row is a ``LEDGER#VERSION``,
``#MODEL``, ``#TOOL``, ``#AGENT`` or ``#PROJECT`` item as stored (counters are
``int`` or ``Decimal``; an absent counter is zero).

**A rate is ``None`` below its floor, never ``0``.** A KPI is ``{"value", "n"}``
where ``n`` is the denominator it was computed over. A *share* (some of the
calls or turns did X) needs at least :data:`MIN_SHARE_N` of them, the same floor
the feedback arm uses (response-feedback spec §9): a 1-of-3 interrupted share
is not evidence of anything, and printing ``0.33`` would read as if it were. A
*mean* (dollars per turn, characters per call, milliseconds per first token)
needs only a non-zero denominator. Either way a missing denominator is
``None`` with ``n: 0``, because ``0`` is a value a reader would believe.

**Turn KPIs are chat-only.** ``costPerTurn`` and ``meanFirstTokenMs`` read the
writer's chat split (``chatCost`` over ``splitTurns``, ``chatFirstTokenMsSum``
over ``chatFirstTokenCount``), never ``cost`` or ``firstTokenMsSum``, which
add every call kind: a workflow prompt step is a call, not a turn, and its
seconds-long first byte is not a regression of the turn path. A row written
before the split carries none of those counters, so it adds nothing to either
side of the ratio and the KPI covers only the rows that have them; a window
with none reads ``None``. :func:`chat_split_coverage` says how much of a
window that is, for the readout's ``predates_chat_split`` caveat. The other
kinds are reported on their own by :func:`call_kind_kpis`.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Optional

#: The smallest denominator a share is reported over.
MIN_SHARE_N = 20

#: Characters per token for the tool-payload estimate (D4); the writer's
#: ``ledger_rollups.CHARS_PER_TOKEN``, restated so this module stays stdlib-only.
CHARS_PER_TOKEN = 4

#: Attributes on a ledger row that are labels, not counters.
_LABELS = frozenset({
    "PK", "SK", "date", "appVersion", "build", "modelId", "toolId", "agentId", "projectId",
})

KPI = Dict[str, Any]


def _num(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, InvalidOperation):
        return 0.0
    return number if math.isfinite(number) else 0.0


def sum_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
    """Add every numeric counter across ``rows`` (labels and keys are skipped)."""
    totals: Dict[str, float] = {}
    for row in rows:
        for name, value in row.items():
            if name in _LABELS or isinstance(value, (str, bool)) or value is None:
                continue
            if isinstance(value, (int, float, Decimal)):
                totals[name] = totals.get(name, 0.0) + _num(value)
    return totals


def _ratio(numerator: float, denominator: float, floor: float) -> KPI:
    n = int(denominator)
    if denominator <= 0 or denominator < floor:
        return {"value": None, "n": n}
    return {"value": numerator / denominator, "n": n}


def mean(totals: Mapping[str, Any], numerator: str, denominator: str) -> KPI:
    """``numerator / denominator`` as a mean: ``None`` only when the denominator is zero."""
    return _ratio(_num(totals.get(numerator)), _num(totals.get(denominator)), 1)


def share(totals: Mapping[str, Any], numerator: str, denominator: str, floor: int = MIN_SHARE_N) -> KPI:
    """``numerator / denominator`` as a share: ``None`` below ``floor``."""
    return _ratio(_num(totals.get(numerator)), _num(totals.get(denominator)), floor)


#: The KPIs that read the chat-only split instead of every call.
CHAT_SPLIT_KPIS = ("costPerTurn", "meanFirstTokenMs")

#: The counter prefixes of the by-kind split (``ledger_rollups``): calls per
#: kind exist on every row; cost and first byte only on rows the split wrote.
_KIND_CALLS = "callKind."
_KIND_COST = "callKindCost."
_KIND_FT_SUM = "callKindFirstTokenMsSum."
_KIND_FT_COUNT = "callKindFirstTokenCount."
CHAT_KIND = "chat"


def chat_split_coverage(totals: Mapping[str, Any]) -> Dict[str, int]:
    """How much of a window the chat split covers: ``{splitTurns, turns, splitCalls, calls}``.

    ``splitTurns < turns`` (or ``splitCalls < calls``) means some rows predate
    the split; the chat-only KPIs cover the ``split`` part only.
    """
    return {name: int(_num(totals.get(name))) for name in ("splitTurns", "turns", "splitCalls", "calls")}


def call_kind_kpis(totals: Mapping[str, Any]) -> Dict[str, Dict[str, KPI]]:
    """Calls, cost and mean model first byte per call kind, from summed ``LEDGER#VERSION`` counters.

    ``chat`` reads the same split the turn KPIs read; every other kind (a
    workflow prompt or checklist step, a handoff brief, voice, api-converse,
    ``document_extract``) reads its ``callKindCost.<kind>`` and
    ``callKindFirstTokenMs*.<kind>``. ``calls`` counts every row's calls; the
    cost and first byte only the rows the split wrote, so a window with none
    reads ``None`` for both (a dollar total is a value too, and ``0`` would be
    believed).
    """
    t = totals
    split = _num(t.get("splitCalls")) > 0
    kinds = sorted({name[len(_KIND_CALLS):] for name in t if name.startswith(_KIND_CALLS)} | (
        {name[len(_KIND_COST):] for name in t if name.startswith(_KIND_COST)}
    ))
    out: Dict[str, Dict[str, KPI]] = {}
    for kind in kinds:
        calls = int(_num(t.get(f"{_KIND_CALLS}{kind}")))
        if kind == CHAT_KIND:
            cost, ft_sum, ft_count = "chatCost", "chatFirstTokenMsSum", "chatFirstTokenCount"
        else:
            cost, ft_sum, ft_count = f"{_KIND_COST}{kind}", f"{_KIND_FT_SUM}{kind}", f"{_KIND_FT_COUNT}{kind}"
        out[kind] = {
            "calls": {"value": float(calls), "n": calls},
            "cost": {"value": _num(t.get(cost)) if split else None, "n": calls},
            "meanFirstTokenMs": mean(t, ft_sum, ft_count),
        }
    return out


def version_kpis(totals: Mapping[str, Any]) -> Dict[str, KPI]:
    """The §6.2 KPIs from summed ``LEDGER#VERSION`` counters.

    ``emptyAnswerShare`` and ``interruptedShare`` divide by ``outcomeTurns``
    (turns whose row carries an ``outcome``), not by ``turns``: rows written
    before the outcome existed would otherwise count as clean answers.
    ``paused`` is not an interruption. ``meanFirstTokenMs`` is the model's own
    time to first byte per **chat** call; the turn's server TTFT percentiles
    come from CloudWatch at read time (§6.1). It and ``costPerTurn`` read the
    chat split only (module docstring); ``costPerCall`` stays over every call.
    """
    t = totals
    cache_read = _num(t.get("cacheReadTokens"))
    cache_write = _num(t.get("cacheWriteTokens"))
    cost = _num(t.get("cost"))
    unexplained = (
        _num(t.get("wastedUsd")) - _num(t.get("agentSwitchUsd")) - _num(t.get("modelSwitchUsd"))
    )
    cut_off = (
        _num(t.get("stoppedTurns")) + _num(t.get("errorTurns")) + _num(t.get("interruptedTurns"))
    )
    outcome_turns = _num(t.get("outcomeTurns"))
    return {
        "costPerTurn": mean(t, "chatCost", "splitTurns"),
        "costPerCall": mean(t, "cost", "calls"),
        "cacheEfficiency": _ratio(cache_read, cache_read + cache_write, 1),
        # A share of dollars, so its `n` is the calls those dollars came from.
        "unexplainedWasteShare": {
            "value": unexplained / cost if cost > 0 else None,
            "n": int(_num(t.get("calls"))),
        },
        "partialMissShare": share(t, "partialMissCount", "calls"),
        "toolErrorRate": share(t, "toolErrors", "toolCalls"),
        "toolPayloadPerCall": mean(t, "toolResultChars", "toolCalls"),
        "documentTurnShare": share(t, "documentTurns", "turns"),
        "meanFirstTokenMs": mean(t, "chatFirstTokenMsSum", "chatFirstTokenCount"),
        "meanTurnDurationMs": mean(t, "turnDurationMsSum", "turns"),
        "emptyAnswerShare": share(t, "emptyAnswerTurns", "outcomeTurns"),
        "interruptedShare": _ratio(cut_off, outcome_turns, MIN_SHARE_N),
        "pausedShare": share(t, "pausedTurns", "outcomeTurns"),
        "forcedCompactionRate": share(t, "forcedCompactions", "compactionEvents"),
    }


def tool_kpis(totals: Mapping[str, Any]) -> Dict[str, KPI]:
    """Per-tool KPIs from summed ``LEDGER#TOOL`` counters.

    Mean latency divides by ``timedCalls``, never ``calls``: a call cancelled
    before it ran reported no duration. ``estInputUsd`` is an estimate
    (result characters / 4, at the call's model input rate) and is passed
    through as a total, not a rate.
    """
    t = totals
    return {
        "errorRate": share(t, "errors", "calls"),
        "resultCharsPerCall": mean(t, "resultChars", "calls"),
        "inputCharsPerCall": mean(t, "inputChars", "calls"),
        "meanDurationMs": mean(t, "durationMsSum", "timedCalls"),
        "estInputUsd": {"value": _num(t.get("estInputUsd")), "n": int(_num(t.get("calls")))},
        # The same estimate in tokens: result characters / CHARS_PER_TOKEN.
        "estInputTokens": {
            "value": _num(t.get("resultChars")) / CHARS_PER_TOKEN,
            "n": int(_num(t.get("calls"))),
        },
    }


def model_kpis(totals: Mapping[str, Any]) -> Dict[str, KPI]:
    """Per-model KPIs from summed ``LEDGER#MODEL`` counters (the readout's per-model break-out).

    ``costPerTurn`` and ``meanFirstTokenMs`` read the chat split, as
    :func:`version_kpis` does.
    """
    t = totals
    cache_read = _num(t.get("cacheReadTokens"))
    cache_write = _num(t.get("cacheWriteTokens"))
    return {
        "costPerTurn": mean(t, "chatCost", "splitTurns"),
        "costPerCall": mean(t, "cost", "calls"),
        "cacheEfficiency": _ratio(cache_read, cache_read + cache_write, 1),
        "partialMissShare": share(t, "partialMissCount", "calls"),
        "meanFirstTokenMs": mean(t, "chatFirstTokenMsSum", "chatFirstTokenCount"),
    }


def group_kpis(totals: Mapping[str, Any]) -> Dict[str, KPI]:
    """Per-agent or per-project KPIs from summed ``LEDGER#AGENT`` / ``#PROJECT`` counters."""
    t = totals
    return {
        "costPerTurn": mean(t, "cost", "turns"),
        "wastePerTurn": mean(t, "wastedUsd", "turns"),
        "toolErrorRate": share(t, "toolErrors", "toolCalls"),
    }


def relative_delta(baseline: Optional[float], candidate: Optional[float]) -> Optional[float]:
    """``(candidate − baseline) / baseline``; ``None`` when either side is ``None`` or the baseline is 0."""
    if baseline is None or candidate is None or baseline == 0:
        return None
    return (candidate - baseline) / baseline
