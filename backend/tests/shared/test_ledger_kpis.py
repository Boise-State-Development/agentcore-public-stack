"""The release ledger's derived KPIs (docs/specs/release-ledger.md §6.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apis.shared.costs import ledger_kpis as k


def _version(**counters):
    base = {"PK": "LEDGER#VERSION", "SK": "2026-11-06#1.33.0#b", "date": "2026-11-06",
            "appVersion": "1.33.0", "build": "b"}
    base.update(counters)
    return base


def test_sum_rows_adds_counters_and_skips_labels():
    totals = k.sum_rows([
        _version(calls=3, cost=Decimal("0.3")),
        _version(calls=2, cost=Decimal("0.2"), **{"callKind.chat": 2}),
    ])
    assert totals == {"calls": 5.0, "cost": pytest.approx(0.5), "callKind.chat": 2.0}


def test_version_kpis_on_a_full_window():
    totals = k.sum_rows([_version(
        calls=100, turns=40, outcomeTurns=40, cost=Decimal("2.0"),
        cacheReadTokens=900, cacheWriteTokens=100,
        wastedUsd=Decimal("0.5"), agentSwitchUsd=Decimal("0.1"), modelSwitchUsd=Decimal("0.1"),
        partialMissCount=5, toolCalls=50, toolErrors=5, toolResultChars=50_000,
        documentTurns=10, firstTokenMsSum=80_000, firstTokenCount=100, turnDurationMsSum=200_000,
        emptyAnswerTurns=2, stoppedTurns=1, errorTurns=1, interruptedTurns=2, pausedTurns=4,
        compactionEvents=20, forcedCompactions=2,
    )])
    kpis = k.version_kpis(totals)
    values = {name: kpi["value"] for name, kpi in kpis.items()}
    assert values == pytest.approx({
        "costPerTurn": 0.05,
        "costPerCall": 0.02,
        "cacheEfficiency": 0.9,
        "unexplainedWasteShare": 0.15,
        "partialMissShare": 0.05,
        "toolErrorRate": 0.1,
        "toolPayloadPerCall": 1000.0,
        "documentTurnShare": 0.25,
        "meanFirstTokenMs": 800.0,
        "meanTurnDurationMs": 5000.0,
        "emptyAnswerShare": 0.05,
        "interruptedShare": 0.1,  # paused is not an interruption
        "pausedShare": 0.1,
        "forcedCompactionRate": 0.1,
    })
    assert kpis["costPerTurn"]["n"] == 40
    assert kpis["interruptedShare"]["n"] == 40


def test_an_empty_window_is_none_everywhere_never_zero():
    kpis = k.version_kpis({})
    assert all(kpi["value"] is None for kpi in kpis.values())
    assert all(kpi["n"] == 0 for kpi in kpis.values())


def test_a_share_under_its_floor_is_none_but_a_mean_is_not():
    totals = k.sum_rows([_version(calls=5, turns=5, outcomeTurns=5, cost=Decimal("0.5"),
                                  stoppedTurns=1, emptyAnswerTurns=0)])
    kpis = k.version_kpis(totals)
    assert kpis["costPerTurn"]["value"] == pytest.approx(0.1)
    assert kpis["interruptedShare"] == {"value": None, "n": 5}
    assert kpis["emptyAnswerShare"] == {"value": None, "n": 5}
    totals["outcomeTurns"] = k.MIN_SHARE_N
    assert k.version_kpis(totals)["emptyAnswerShare"] == {"value": 0.0, "n": k.MIN_SHARE_N}


def test_shares_divide_by_turns_that_carry_an_outcome():
    """Turns from before PR-2 have no outcome and must not dilute the share."""
    totals = {"turns": 100, "outcomeTurns": 20, "emptyAnswerTurns": 2}
    assert k.version_kpis(totals)["emptyAnswerShare"]["value"] == pytest.approx(0.1)


def test_tool_kpis_mean_latency_divides_by_timed_calls():
    totals = k.sum_rows([{"PK": "LEDGER#TOOL#2026-11", "toolId": "browse_web", "calls": 30, "errors": 3,
                          "resultChars": 60_000, "inputChars": 900, "durationMsSum": 10_000,
                          "timedCalls": 20, "estInputUsd": Decimal("0.0165")}])
    kpis = k.tool_kpis(totals)
    assert kpis["errorRate"]["value"] == pytest.approx(0.1)
    assert kpis["resultCharsPerCall"]["value"] == pytest.approx(2000.0)
    assert kpis["meanDurationMs"] == {"value": pytest.approx(500.0), "n": 20}
    assert kpis["estInputUsd"]["value"] == pytest.approx(0.0165)


def test_group_kpis():
    kpis = k.group_kpis({"turns": 4, "cost": 1.0, "wastedUsd": 0.2, "toolCalls": 3, "toolErrors": 1})
    assert kpis["costPerTurn"]["value"] == pytest.approx(0.25)
    assert kpis["wastePerTurn"]["value"] == pytest.approx(0.05)
    assert kpis["toolErrorRate"]["value"] is None


@pytest.mark.parametrize(
    "baseline, candidate, delta",
    [(0.1, 0.12, 0.2), (None, 0.1, None), (0.1, None, None), (0.0, 0.1, None)],
)
def test_relative_delta(baseline, candidate, delta):
    result = k.relative_delta(baseline, candidate)
    assert result == (pytest.approx(delta) if delta is not None else None)


def test_the_module_is_stdlib_only():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(k.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert imported <= {"__future__", "decimal", "math", "typing"}
