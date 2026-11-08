"""The release readout: candidate vs baseline, in equal windows (spec §6.3, D5, D10).

This is the only place a ledger *comparison* is made. KPI values come from
:mod:`apis.shared.costs.ledger_kpis`; this module picks the two units and
their windows, pairs the values as ``[baseline, candidate]``, and generates the
caveats. ``GET /admin/costs/ledger/readout`` and ``scripts/release_readout.py``
call :func:`select_windows` and :func:`build_readout`, so a page, a script and
a model all see the same numbers with the same warnings in the same words.

Pure: rows in, bundle out. The caller does the reading.

**Windows (D5).** The candidate's *first* ``days`` full days are compared with
the baseline's *last* ``days`` full days. A full day is a UTC date before today
on which the unit was the only one of its granularity to write a row: a deploy
day has rows from both and is a boundary, not a sample. When a unit has no
full day (common for a dev build, which may live for an hour) the window falls
back to the days it was observed, and a caveat says so.

**Caveats are generated, not written.** Each is ``{code, message}`` from a
fixed set (:data:`CAVEAT_CODES`), detected from the data.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from apis.shared.costs import ledger_kpis
from apis.shared.files.source_provenance import STATE_QUOTE_UNSUPPORTED
from apis.shared.costs.ledger_reader import (
    Unit,
    build_kind,
    export_row,
    group_rows,
    parse_date,
    spec_tokens,
    totals,
    unit_of,
)

DEFAULT_DAYS = 7
MAX_DAYS = 30
#: A model's call share, or the active-user count, moving by more than this
#: (relative) between windows is a mix shift (D5).
MIX_SHIFT_THRESHOLD = 0.2
#: Models below this share in both windows are not mix-shift evidence.
MIX_SHIFT_MIN_SHARE = 0.05
TOP_TOOLS = 30
TOP_GROUPS = 20

CAVEAT_CODES = (
    "no_baseline",
    "no_full_day",
    "short_window",
    "deploy_in_window",
    "mix_shift",
    "below_floor",
    "thumbs_below_floor",
    "estimated_rows",
    "unmetered_rows",
    "commit_attributed",
    "payload_not_measured",
    "latency_unavailable",
    "latency_mixed_window",
    "coverage_truncated",
)

Row = Dict[str, Any]
Pair = List[Any]


def caveat(code: str, message: str) -> Dict[str, str]:
    assert code in CAVEAT_CODES, code
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# Picking the units and windows
# ---------------------------------------------------------------------------


def _ordered_units(rows: Sequence[Row], by_build: bool, markers: Sequence[Row]) -> List[Tuple[Unit, List[str]]]:
    """Units oldest first, each with its observed dates.

    Ordered by first date, then by when it first showed up within that day:
    the earliest deploy marker or first live write (a backfilled build has
    neither, and sorts by key). Same-day builds are otherwise unordered, so a
    caller comparing two of them should name both.
    """
    seen_at: Dict[str, str] = {}
    for marker in markers:
        build = str(marker.get("build") or "")
        stamp = str(marker.get("firstSeenAt") or "")
        if build and stamp and (build not in seen_at or stamp < seen_at[build]):
            seen_at[build] = stamp
    units = []
    for unit, unit_rows in group_rows(rows, lambda r: unit_of(r, by_build)).items():
        dates = sorted({str(r.get("date") or "") for r in unit_rows})
        stamps = [str(r["firstLiveAt"]) for r in unit_rows if r.get("firstLiveAt")]
        stamps += [seen_at[str(r.get("build"))] for r in unit_rows if str(r.get("build")) in seen_at]
        units.append((dates[0], min(stamps) if stamps else "￿", unit.key, unit, dates))
    units.sort(key=lambda u: u[:3])
    return [(u[3], u[4]) for u in units]


def _full_days(unit: Unit, dates: Sequence[str], rows: Sequence[Row], by_build: bool, today: date) -> List[str]:
    others: Dict[str, set] = {}
    for row in rows:
        other = unit_of(row, by_build)
        if other != unit:
            others.setdefault(str(row.get("date") or ""), set()).add(other.key)
    return [d for d in dates if parse_date(d) < today and d not in others]


def _window(unit: Unit, dates: Sequence[str], rows: Sequence[Row], by_build: bool, today: date,
            days: int, earliest: bool) -> Dict[str, Any]:
    full = _full_days(unit, dates, rows, by_build, today)
    partial = not full
    pool = full or [d for d in dates if parse_date(d) < today] or list(dates)
    chosen = pool[:days] if earliest else pool[-days:]
    shared = sorted({
        str(r.get("date")) for r in rows
        if str(r.get("date")) in chosen and unit_of(r, by_build) != unit
    })
    return {
        "unit": unit,
        "dates": chosen,
        "window": [chosen[0], chosen[-1]] if chosen else [None, None],
        "days": len(chosen),
        "fullDays": len(full),
        "partial": partial,
        "sharedDates": shared,
        "includesToday": today.isoformat() in chosen,
    }


def select_windows(
    version_rows: Sequence[Row],
    *,
    candidate: Optional[Unit] = None,
    baseline: Optional[Unit] = None,
    days: int = DEFAULT_DAYS,
    today: date,
    markers: Sequence[Row] = (),
) -> Dict[str, Any]:
    """Pick the candidate, the baseline and their windows. Raises ``LookupError`` for an unknown unit.

    The granularity follows the candidate: a ``1.33.0#<build>`` candidate is
    compared with a build, a bare ``1.33.0`` with a version.
    """
    days = max(1, min(int(days), MAX_DAYS))
    by_build = bool(candidate and candidate.is_build) or bool(baseline and baseline.is_build)
    if candidate and baseline and candidate.is_build != baseline.is_build:
        raise ValueError("candidate and baseline must both be versions or both be builds")
    ordered = _ordered_units(version_rows, by_build, markers)
    if not ordered:
        raise LookupError("no ledger rows in range")
    index = {unit: position for position, (unit, _) in enumerate(ordered)}

    if candidate is None:
        with_full = [u for u, d in ordered if _full_days(u, d, version_rows, by_build, today)]
        candidate = with_full[-1] if with_full else ordered[-1][0]
    if candidate not in index:
        raise LookupError(f"no ledger rows for {candidate.key}")
    if baseline is None:
        position = index[candidate]
        baseline = ordered[position - 1][0] if position > 0 else None
    elif baseline not in index:
        raise LookupError(f"no ledger rows for {baseline.key}")

    dates_of = dict(ordered)
    return {
        "byBuild": by_build,
        "requestedDays": days,
        "candidate": _window(candidate, dates_of[candidate], version_rows, by_build, today, days, True),
        "baseline": (
            _window(baseline, dates_of[baseline], version_rows, by_build, today, days, False)
            if baseline else None
        ),
    }


def in_window(row: Mapping[str, Any], side: Optional[Mapping[str, Any]]) -> bool:
    return bool(side) and side["unit"].matches(row) and str(row.get("date") or "") in side["dates"]


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def _side_rows(rows: Iterable[Row], side: Optional[Mapping[str, Any]]) -> List[Row]:
    return [r for r in rows if in_window(r, side)] if side else []


def _pair(baseline: Any, candidate: Any) -> Pair:
    return [baseline, candidate]


def _value(kpi: Optional[Mapping[str, Any]]) -> Optional[float]:
    return None if kpi is None else kpi.get("value")


def _n(kpi: Optional[Mapping[str, Any]]) -> int:
    return 0 if kpi is None else int(kpi.get("n") or 0)


def _compare(base: Mapping[str, Any], cand: Mapping[str, Any], name: str) -> Dict[str, Any]:
    b, c = base.get(name), cand.get(name)
    return {
        "baseline": _value(b),
        "candidate": _value(c),
        "delta": ledger_kpis.relative_delta(_value(b), _value(c)),
        "n": _pair(_n(b), _n(c)),
    }


def _side_summary(side: Optional[Mapping[str, Any]], rows: Sequence[Row]) -> Optional[Dict[str, Any]]:
    if not side:
        return None
    unit: Unit = side["unit"]
    builds = sorted({str(r.get("build") or "") for r in rows})
    return {
        "key": unit.key,
        "appVersion": unit.app_version,
        "build": unit.build,
        "builds": len(builds),
        "window": side["window"],
        "days": side["days"],
        "fullDays": side["fullDays"],
    }


def _model_shares(model_rows: Sequence[Row]) -> Dict[str, float]:
    by_model = {m: ledger_kpis.sum_rows(rs).get("calls", 0.0) for m, rs in
                group_rows(model_rows, lambda r: str(r.get("modelId") or "unknown")).items()}
    total = sum(by_model.values())
    return {m: calls / total for m, calls in by_model.items()} if total else {}


def _mean_active_users(daily: Sequence[Row], side: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not side:
        return None
    values = [float(r.get("activeUsers") or 0) for r in daily if str(r.get("SK") or r.get("date")) in side["dates"]]
    return round(sum(values) / len(values), 1) if values else None


def _relative_shift(b: Optional[float], c: Optional[float]) -> bool:
    if b is None or c is None:
        return False
    top = max(b, c)
    return top > 0 and abs(c - b) > MIX_SHIFT_THRESHOLD * top


def _tools(tool_b: Sequence[Row], tool_c: Sequence[Row]) -> List[Dict[str, Any]]:
    tokens = spec_tokens()
    grouped_b = group_rows(tool_b, lambda r: str(r.get("toolId") or "unknown"))
    grouped_c = group_rows(tool_c, lambda r: str(r.get("toolId") or "unknown"))
    out = []
    for tool_id in set(grouped_b) | set(grouped_c):
        tb, tc = totals(grouped_b.get(tool_id, [])), totals(grouped_c.get(tool_id, []))
        kb, kc = ledger_kpis.tool_kpis(tb), ledger_kpis.tool_kpis(tc)
        out.append({
            "toolId": tool_id,
            "calls": _pair(int(tb.get("calls", 0)), int(tc.get("calls", 0))),
            "errorRate": _pair(_value(kb["errorRate"]), _value(kc["errorRate"])),
            "resultCharsPerCall": _pair(_value(kb["resultCharsPerCall"]), _value(kc["resultCharsPerCall"])),
            "meanDurationMs": _pair(_value(kb["meanDurationMs"]), _value(kc["meanDurationMs"])),
            "estInputTokens": _pair(_value(kb["estInputTokens"]), _value(kc["estInputTokens"])),
            "estInputUsd": _pair(_value(kb["estInputUsd"]), _value(kc["estInputUsd"])),
            "payloadMeasured": _pair(
                bool(tb.get("resultChars") or tb.get("timedCalls")) if tb.get("calls") else None,
                bool(tc.get("resultChars") or tc.get("timedCalls")) if tc.get("calls") else None,
            ),
            "specTokens": tokens.get(tool_id),
        })
    out.sort(key=lambda t: (-(t["estInputUsd"][1] or 0.0), -(t["calls"][1] or 0), t["toolId"]))
    return out


def _groups(rows_b: Sequence[Row], rows_c: Sequence[Row], label: str) -> List[Dict[str, Any]]:
    grouped_b = group_rows(rows_b, lambda r: str(r.get(label) or "unknown"))
    grouped_c = group_rows(rows_c, lambda r: str(r.get(label) or "unknown"))
    out = []
    for key in set(grouped_b) | set(grouped_c):
        tb, tc = totals(grouped_b.get(key, [])), totals(grouped_c.get(key, []))
        kb, kc = ledger_kpis.group_kpis(tb), ledger_kpis.group_kpis(tc)
        out.append({
            "id": key,
            "calls": _pair(int(tb.get("calls", 0)), int(tc.get("calls", 0))),
            "turns": _pair(int(tb.get("turns", 0)), int(tc.get("turns", 0))),
            "cost": _pair(round(tb.get("cost", 0.0), 6), round(tc.get("cost", 0.0), 6)),
            "costPerTurn": _compare(kb, kc, "costPerTurn"),
            "wastePerTurn": _compare(kb, kc, "wastePerTurn"),
            "toolErrorRate": _pair(_value(kb["toolErrorRate"]), _value(kc["toolErrorRate"])),
        })
    out.sort(key=lambda g: (-(g["cost"][1] or 0.0), g["id"]))
    return out[:TOP_GROUPS]


#: The key prefix of an item whose cost row predates PR-1's stamps.
UNSTAMPED_PREFIX = "unstamped#"


def sole_units_by_date(version_rows: Sequence[Row], by_build: bool) -> Dict[str, str]:
    """``{date: unit key}`` for every date on which exactly one unit wrote ledger rows.

    The fallback for an item (a thumb, a verdict) whose cost row has no stamp:
    the ledger attributed that row by the deploy-time map, which is not stored,
    so the item is attributed by the ledger itself, and only where that is
    unambiguous. On a date two units share, it is left out, never guessed.
    """
    units: Dict[str, set] = {}
    for row in version_rows:
        units.setdefault(str(row.get("date") or ""), set()).add(unit_of(row, by_build).key)
    return {day: next(iter(keys)) for day, keys in units.items() if len(keys) == 1}


def counts_in(
    by_key: Mapping[str, Mapping[str, int]],
    side: Optional[Mapping[str, Any]],
    sole_by_date: Optional[Mapping[str, str]] = None,
) -> Dict[str, int]:
    """Sum the ``"<appVersion>#<build>|<date>"``-keyed counts falling in a side's unit and dates.

    ``side`` is ``{"unit": Unit, "dates": <collection of YYYY-MM-DD>}``. An
    ``unstamped#…`` key counts when ``sole_by_date`` names the side's unit for
    its date (:func:`sole_units_by_date`).
    """
    total: Dict[str, int] = {}
    if not side:
        return total
    unit: Unit = side["unit"]
    for key, counts in by_key.items():
        build_key, _, day = key.partition("|")
        if day not in side["dates"]:
            continue
        if build_key.startswith(UNSTAMPED_PREFIX):
            if not sole_by_date or sole_by_date.get(day) != unit.key:
                continue
        else:
            version, _, build = build_key.partition("#")
            if version != unit.app_version or (unit.build and build != unit.build):
                continue
        for name, value in counts.items():
            total[name] = total.get(name, 0) + int(value)
    return total


def build_readout(
    selection: Mapping[str, Any],
    *,
    version_rows: Sequence[Row],
    model_rows: Sequence[Row] = (),
    tool_rows: Sequence[Row] = (),
    agent_rows: Sequence[Row] = (),
    project_rows: Sequence[Row] = (),
    daily_rollups: Sequence[Row] = (),
    latency: Optional[Mapping[str, Any]] = None,
    eval_rows: Sequence[Row] = (),
    smoke_rows: Sequence[Row] = (),
    thumbs: Optional[Mapping[str, Any]] = None,
    provenance: Optional[Mapping[str, Any]] = None,
    platform: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The §6.3 bundle. Every pair is ``[baseline, candidate]``; ``None`` is below a floor, never 0.

    Thumbs and verdicts whose cost row predates the stamps are attributed by
    :func:`sole_units_by_date`.

    ``latency`` is ``{"baseline": <window latency>, "candidate": …}`` from
    :mod:`ledger_latency`. ``thumbs`` and ``provenance`` are ``{byKey, coverage}``
    with keys ``"<appVersion>#<build>|<date>"`` (attributed through each
    item's own cost row), so a side counts only what fell inside its window.
    """
    cand, base = selection["candidate"], selection["baseline"]
    requested = selection["requestedDays"]
    caveats: List[Dict[str, str]] = []

    v_c, v_b = _side_rows(version_rows, cand), _side_rows(version_rows, base)
    t_c, t_b = totals(v_c), totals(v_b)
    k_c, k_b = ledger_kpis.version_kpis(t_c), ledger_kpis.version_kpis(t_b)
    m_c, m_b = _side_rows(model_rows, cand), _side_rows(model_rows, base)

    # -- windows -------------------------------------------------------------
    if base is None:
        caveats.append(caveat("no_baseline", f"no version before {cand['unit'].key} in range; nothing to compare"))
    for name, side in (("candidate", cand), ("baseline", base)):
        if not side:
            continue
        if side["partial"]:
            caveats.append(caveat(
                "no_full_day",
                f"{name} {side['unit'].key} has no full day; its window is {side['days']} "
                f"partial day{'s' if side['days'] != 1 else ''} shared with other builds"
                + (", including today (still accumulating)" if side.get("includesToday") else ""),
            ))
        elif side["days"] < requested:
            caveats.append(caveat(
                "short_window",
                f"{name} window is {side['days']} day{'s' if side['days'] != 1 else ''}, not {requested}",
            ))
        if side["sharedDates"]:
            caveats.append(caveat(
                "deploy_in_window",
                f"a deploy is inside the {name} window ({', '.join(side['sharedDates'])}): "
                "latency and active users on those days mix builds",
            ))
        commit_builds = sorted({str(r.get("build")) for r in (v_c if side is cand else v_b)
                                if build_kind(str(r.get("build") or "")) == "commit"})
        if commit_builds:
            caveats.append(caveat(
                "commit_attributed",
                f"{name} rows are attributed by the deploy-time map ({len(commit_builds)} "
                f"commit build{'s' if len(commit_builds) != 1 else ''}), not by their own stamp",
            ))

    # -- mix shift -------------------------------------------------------------
    shares_b, shares_c = _model_shares(m_b), _model_shares(m_c)
    models = {}
    shifted = []
    for model_id in sorted(set(shares_b) | set(shares_c), key=lambda m: -shares_c.get(m, 0.0)):
        b, c = round(shares_b.get(model_id, 0.0), 4), round(shares_c.get(model_id, 0.0), 4)
        models[model_id] = _pair(b, c)
        if base and max(b, c) >= MIX_SHIFT_MIN_SHARE and _relative_shift(b, c):
            shifted.append(f"{model_id} {b:.2f} → {c:.2f} of calls")
    users = _pair(_mean_active_users(daily_rollups, base), _mean_active_users(daily_rollups, cand))
    users_shifted = bool(base) and _relative_shift(users[0], users[1])
    if shifted:
        caveats.append(caveat("mix_shift", "model mix shifted: " + "; ".join(shifted)))
    if users_shifted:
        caveats.append(caveat("mix_shift", f"mean daily active users shifted: {users[0]} → {users[1]}"))

    # -- KPIs --------------------------------------------------------------------
    kpis: Dict[str, Any] = {name: _compare(k_b, k_c, name) for name in k_c}
    by_model: Dict[str, Any] = {}
    grouped_mb = group_rows(m_b, lambda r: str(r.get("modelId") or "unknown"))
    grouped_mc = group_rows(m_c, lambda r: str(r.get("modelId") or "unknown"))
    for model_id in sorted(set(grouped_mb) | set(grouped_mc)):
        mb = ledger_kpis.model_kpis(totals(grouped_mb.get(model_id, [])))
        mc = ledger_kpis.model_kpis(totals(grouped_mc.get(model_id, [])))
        by_model[model_id] = _compare(mb, mc, "costPerTurn")
    kpis["costPerTurn"]["byModel"] = by_model
    under = [name for name, k in kpis.items()
             if base and (k["baseline"] is None or k["candidate"] is None) and max(k["n"]) > 0]
    for name in under:
        caveats.append(caveat("below_floor", f"{name} under its floor (n={kpis[name]['n'][0]} / {kpis[name]['n'][1]})"))

    for name, totals_side in (("baseline", t_b), ("candidate", t_c)):
        if totals_side.get("estimatedCalls"):
            caveats.append(caveat(
                "estimated_rows",
                f"{name} estimatedCalls: {int(totals_side['estimatedCalls'])} rows carry attributed, not measured, usage",
            ))
        if totals_side.get("unmeteredCalls"):
            caveats.append(caveat(
                "unmetered_rows",
                f"{name} unmeteredCalls: {int(totals_side['unmeteredCalls'])} calls priced to nothing "
                "(a model with no catalog row)",
            ))

    # -- tools, agents, projects ---------------------------------------------------
    tools = _tools(_side_rows(tool_rows, base), _side_rows(tool_rows, cand))
    unmeasured = [t["toolId"] for t in tools if False in t["payloadMeasured"]]
    if unmeasured:
        caveats.append(caveat(
            "payload_not_measured",
            f"payload not measured for {len(unmeasured)} tool{'s' if len(unmeasured) != 1 else ''} "
            "(rows from before the census weighed results)",
        ))

    # -- latency -------------------------------------------------------------------
    latency_out: Dict[str, Any] = {"available": False}
    if latency is not None:
        lb, lc = latency.get("baseline") or {}, latency.get("candidate") or {}
        available = bool(lc.get("available")) and (base is None or bool(lb.get("available")))
        latency_out = {"available": available}
        for metric in (lc.get("metrics") or lb.get("metrics") or {}):
            mb_, mc_ = (lb.get("metrics") or {}).get(metric, {}), (lc.get("metrics") or {}).get(metric, {})
            latency_out[metric] = {
                "p50": _pair(mb_.get("p50"), mc_.get("p50")),
                "p90": _pair(mb_.get("p90"), mc_.get("p90")),
                "n": _pair(int(mb_.get("n") or 0), int(mc_.get("n") or 0)),
            }
        if not available:
            caveats.append(caveat("latency_unavailable", "CloudWatch latency could not be read for a window"))
        elif any(side and side["sharedDates"] for side in (cand, base)):
            caveats.append(caveat(
                "latency_mixed_window",
                "latency percentiles are by time, not by build: a window with a deploy in it mixes builds",
            ))

    # -- quality ---------------------------------------------------------------------
    sole = sole_units_by_date(version_rows, selection["byBuild"])
    thumbs_b = counts_in((thumbs or {}).get("byKey", {}), base, sole)
    thumbs_c = counts_in((thumbs or {}).get("byKey", {}), cand, sole)

    def _thumbs(side_counts: Mapping[str, int]) -> Dict[str, Any]:
        up, down = int(side_counts.get("up", 0)), int(side_counts.get("down", 0))
        rate = ledger_kpis.share({"down": down, "n": up + down}, "down", "n")
        return {"up": up, "down": down, "n": up + down, "downRate": rate["value"],
                "retries": int(side_counts.get("retries", 0))}

    tb_, tc_ = _thumbs(thumbs_b), _thumbs(thumbs_c)
    thumbs_out = {name: _pair(tb_[name], tc_[name]) for name in ("up", "down", "n", "downRate", "retries")}
    thumbs_out["minimumN"] = ledger_kpis.MIN_SHARE_N
    thumbs_out["coverage"] = (thumbs or {}).get("coverage")
    for name, t in (("baseline", tb_), ("candidate", tc_)):
        if (base or name == "candidate") and t["n"] < ledger_kpis.MIN_SHARE_N:
            caveats.append(caveat(
                "thumbs_below_floor", f"{name} thumbs arm under the floor (n={t['n']}): counts only, no rate",
            ))

    prov_b = counts_in((provenance or {}).get("byKey", {}), base, sole)
    prov_c = counts_in((provenance or {}).get("byKey", {}), cand, sole)
    share_b = ledger_kpis.share(prov_b, STATE_QUOTE_UNSUPPORTED, "claims")
    share_c = ledger_kpis.share(prov_c, STATE_QUOTE_UNSUPPORTED, "claims")
    provenance_out = {
        "claims": _pair(prov_b.get("claims", 0), prov_c.get("claims", 0)),
        "quoteUnsupported": _pair(prov_b.get(STATE_QUOTE_UNSUPPORTED, 0), prov_c.get(STATE_QUOTE_UNSUPPORTED, 0)),
        "quoteUnsupportedShare": _pair(share_b["value"], share_c["value"]),
        "coverage": (provenance or {}).get("coverage"),
    }
    if (prov_b.get("claims") or prov_c.get("claims")) and None in provenance_out["quoteUnsupportedShare"]:
        caveats.append(caveat(
            "below_floor",
            f"quoteUnsupportedShare under its floor (n={prov_b.get('claims', 0)} / {prov_c.get('claims', 0)})",
        ))
    for source in (thumbs, provenance):
        coverage = (source or {}).get("coverage") or {}
        if coverage.get("truncated") or coverage.get("usersTruncated") or coverage.get("sessionsOmitted"):
            caveats.append(caveat("coverage_truncated", "a quality join hit its cap; its counts are a lower bound"))
            break

    def _unit_rows(rows: Sequence[Row], side: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        return [export_row(r) for r in rows if side and side["unit"].matches(r)]

    outcomes = {
        name: _pair(int(t_b.get(counter, 0)), int(t_c.get(counter, 0)))
        for name, counter in (("completed", "completedTurns"), ("paused", "pausedTurns"),
                              ("stopped", "stoppedTurns"), ("error", "errorTurns"),
                              ("interrupted", "interruptedTurns"), ("emptyAnswer", "emptyAnswerTurns"),
                              ("outcomeTurns", "outcomeTurns"))
    }
    quality = {
        "eval": {"baseline": _unit_rows(eval_rows, base), "candidate": _unit_rows(eval_rows, cand)},
        "smoke": {"baseline": _unit_rows(smoke_rows, base), "candidate": _unit_rows(smoke_rows, cand)},
        "emptyAnswerShare": _pair(_value(k_b["emptyAnswerShare"]), _value(k_c["emptyAnswerShare"])),
        "interruptedShare": _pair(_value(k_b["interruptedShare"]), _value(k_c["interruptedShare"])),
        "pausedShare": _pair(_value(k_b["pausedShare"]), _value(k_c["pausedShare"])),
        "forcedCompactionRate": _pair(_value(k_b["forcedCompactionRate"]), _value(k_c["forcedCompactionRate"])),
        "toolErrorRate": _pair(_value(k_b["toolErrorRate"]), _value(k_c["toolErrorRate"])),
        "outcomes": outcomes,
        "provenance": provenance_out,
        # Last on purpose (spec §10 Q2): at prod's rate the arm sits under the
        # floor for most of a version's life.
        "thumbs": thumbs_out,
    }

    return {
        "candidate": _side_summary(cand, v_c),
        "baseline": _side_summary(base, v_b),
        "unit": "build" if selection["byBuild"] else "version",
        "requestedDays": requested,
        "mixShift": {"warn": bool(shifted) or users_shifted, "models": models, "activeUsers": users},
        "kpis": kpis,
        "tools": tools[:TOP_TOOLS],
        "agents": _groups(_side_rows(agent_rows, base), _side_rows(agent_rows, cand), "agentId"),
        "projects": _groups(_side_rows(project_rows, base), _side_rows(project_rows, cand), "projectId"),
        "latency": latency_out,
        "quality": quality,
        "platform": dict(platform) if platform else None,
        "caveats": caveats,
    }


def window_span(selection: Mapping[str, Any]) -> Tuple[str, str]:
    """The earliest and latest date either window covers (what the caller must read)."""
    dates = list(selection["candidate"]["dates"])
    if selection["baseline"]:
        dates += selection["baseline"]["dates"]
    return min(dates), max(dates)


def lookback_start(today: date, days: int) -> str:
    return (today - timedelta(days=days)).isoformat()
