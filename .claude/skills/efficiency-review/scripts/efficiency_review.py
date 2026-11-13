#!/usr/bin/env python3
"""Weekly efficiency review: cost, speed and quality joined on the turn.

Reads one content-free scan holding per-call ``C#`` cost rows and ``F#``
feedback rows (thumbs and implicit signals) and writes ``summary.json`` and ``review.md``. Stdlib only; run with
``python3 -I``. Nothing here reads message text, titles or file names, and no
session or user id is written to either output.

A value under its floor is ``None`` (``—`` in the markdown), never zero.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

FLOOR = 20  # minimum n behind any rate, share or percentile
TOOL_LEAD_MIN_CALLS = 30
TOOL_LEAD_ERROR_RATE = 0.10
PREMIUM_MARKERS = ("opus", "fable", "gpt-6-astra")
CALL_BANDS = ((1, 1, "1"), (2, 2, "2"), (3, 4, "3-4"), (5, 9, "5-9"), (10, 10**6, "10+"))
REASONING_SIGNATURE_CHARS_PER_TOKEN = 3.1  # as in the compaction policy; an estimate
BAD_ENDINGS = ("error", "stopped", "interrupted")
DURATION_BANDS = ((0, 10, "under 10 s"), (10, 30, "10-30 s"), (30, 90, "30-90 s"), (90, 10**6, "over 90 s"))

Row = Dict[str, Any]


def _num(attr: Optional[Dict[str, Any]], default: float = 0.0) -> float:
    if not attr or "N" not in attr:
        return default
    return float(attr["N"])


def _text(attr: Optional[Dict[str, Any]]) -> Optional[str]:
    return attr.get("S") if attr else None


def _cost(row: Row, part: str = "total") -> float:
    cost = row.get("cost")
    if not cost:
        return 0.0
    if "N" in cost:
        return float(cost["N"]) if part == "total" else 0.0
    return _num(cost.get("M", {}).get(part))


def _model(row: Row) -> str:
    model_id = _text(row.get("modelInfo", {}).get("M", {}).get("modelId")) or "unknown"
    for prefix in ("us.", "global.", "anthropic."):
        model_id = model_id.removeprefix(prefix)
    return model_id.removeprefix("anthropic.")


def _is_premium(model: str) -> bool:
    return any(marker in model for marker in PREMIUM_MARKERS)


def _percentile(values: List[float], fraction: float) -> Optional[float]:
    if len(values) < FLOOR:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _rate(numerator: float, denominator: float) -> Optional[float]:
    if denominator < FLOOR:
        return None
    return numerator / denominator


def _load_items(path: Optional[str]) -> List[Row]:
    if not path:
        return []
    return json.loads(Path(path).read_text()).get("Items", [])


def build_turns(rows: Iterable[Row]) -> List[Dict[str, Any]]:
    """Group a session's calls into turns; a turn ends on the row carrying ``turnDurationMs``."""
    by_session: Dict[str, List[Row]] = defaultdict(list)
    for row in rows:
        session_id = _text(row.get("sessionId")) or ""
        if session_id.startswith("api-converse") or "callKind" in row:
            continue
        by_session[session_id].append(row)

    turns: List[Dict[str, Any]] = []
    for session_id, session_rows in by_session.items():
        session_rows.sort(key=lambda r: _text(r.get("timestamp")) or "")
        current = _new_turn()
        for row in session_rows:
            _add_call(current, row)
            if "turnDurationMs" not in row:
                continue
            current.update(
                session=session_id,
                user=_text(row.get("userId")),
                stamp=_text(row.get("timestamp")) or "",
                day=(_text(row.get("timestamp")) or "")[:10],
                message_id=int(_num(row.get("messageId"), -1)),
                model=_model(row),
                duration_s=_num(row.get("turnDurationMs")) / 1000,
                outcome=_text(row.get("outcome")),
                empty="emptyAnswer" in row,
                version=_text(row.get("appVersion")),
                has_documents=bool(row.get("hasDocuments", {}).get("BOOL")),
                agent=bool(row.get("turnAgentId")),
                project=bool(row.get("projectId")),
            )
            turns.append(current)
            current = _new_turn()
    return turns


def _new_turn() -> Dict[str, Any]:
    return {
        "calls": 0, "cost": 0.0, "output_cost": 0.0, "wasted": 0.0, "ttft_ms": None,
        "tool_calls": 0, "tool_errors": 0, "tool_ms": 0.0, "tool_timed": 0,
        "tools": Counter(), "statuses": Counter(), "rewound": False,
        "model_ms": 0.0, "error_kinds": Counter(),
        "output_tokens": 0.0, "text_chars": 0.0, "reasoning_chars": 0.0, "signature_chars": 0.0, "measured_output_tokens": 0.0,
    }


def _add_call(turn: Dict[str, Any], row: Row) -> None:
    turn["calls"] += 1
    turn["cost"] += _cost(row)
    turn["output_cost"] += _cost(row, "outputCost")
    turn["wasted"] += _num(row.get("wastedUsd"))
    turn["statuses"][_text(row.get("cacheStatus")) or "unknown"] += 1
    turn["rewound"] = turn["rewound"] or "rewound" in row
    if turn["ttft_ms"] is None:
        first_token = _num(row.get("latency", {}).get("M", {}).get("timeToFirstToken"))
        turn["ttft_ms"] = first_token or None
    turn["model_ms"] += _num(row.get("latency", {}).get("M", {}).get("endToEndLatency"))
    output_tokens = _num(row.get("tokenUsage", {}).get("M", {}).get("outputTokens"))
    turn["output_tokens"] += output_tokens
    chars = row.get("outputChars", {}).get("M")
    if chars:
        turn["measured_output_tokens"] += output_tokens
        turn["text_chars"] += _num(chars.get("text"))
        turn["reasoning_chars"] += _num(chars.get("reasoning"))
        turn["signature_chars"] += _num(chars.get("reasoningSignature"))
    for name, entry in row.get("toolCalls", {}).get("M", {}).items():
        fields = entry.get("M", {})
        calls = int(_num(fields.get("calls")))
        turn["tool_calls"] += calls
        for kind, count in fields.get("errorKinds", {}).get("M", {}).items():
            turn["error_kinds"][kind] += int(_num(count))
        turn["tool_errors"] += int(_num(fields.get("errors")))
        turn["tool_ms"] += _num(fields.get("durationMs"))
        turn["tool_timed"] += int(_num(fields.get("timedCalls")))
        turn["tools"][name] += calls


def split_rows(items: Iterable[Row]) -> tuple:
    """Cost rows and feedback rows from one scan. A feedback row has no ``cost``
    and carries a ``value`` (a thumb) or ``signal == "implicit"``."""
    cost_rows: List[Row] = []
    feedback: List[Row] = []
    for item in items:
        if "modelInfo" in item:
            cost_rows.append(item)
        elif "value" in item or "signal" in item:
            feedback.append(item)
    return cost_rows, feedback


def _feedback(feedback_rows: Iterable[Row]) -> tuple:
    """``(thumbs, implicit)`` keyed by ``(sessionId, messageId)``."""
    thumbs: Dict[tuple, Dict[str, Any]] = {}
    implicit: Dict[tuple, Counter] = defaultdict(Counter)
    for row in feedback_rows:
        key = (_text(row.get("sessionId")), int(_num(row.get("messageId"), -1)))
        if _text(row.get("signal")) == "implicit":
            implicit[key][_text(row.get("kind")) or "unknown"] += int(_num(row.get("count"), 1))
            continue
        thumbs[key] = {
            "value": int(_num(row.get("value"))),
            "reason": _text(row.get("reason")),
            "retried": "retryMessageId" in row,
        }
    return thumbs, implicit


def _pillars(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The three pillars for one slice of turns."""
    count = len(turns)
    cost = sum(t["cost"] for t in turns)
    durations = [t["duration_s"] for t in turns]
    ttfts = [t["ttft_ms"] / 1000 for t in turns if t["ttft_ms"]]
    with_outcome = [t for t in turns if t["outcome"]]
    completed = [t for t in with_outcome if t["outcome"] == "completed" and not t["empty"]]
    up = sum(1 for t in turns if t.get("thumb") == 1)
    down = sum(1 for t in turns if t.get("thumb") == -1)
    return {
        "turns": count,
        "cost_usd": round(cost, 2),
        "cost_per_turn": round(cost / count, 4) if count >= FLOOR else None,
        "cost_per_completed_turn": round(cost / len(completed), 4) if len(completed) >= FLOOR else None,
        "calls_per_turn": round(sum(t["calls"] for t in turns) / count, 2) if count >= FLOOR else None,
        "turn_p50_s": _round(_percentile(durations, 0.5)),
        "turn_p90_s": _round(_percentile(durations, 0.9)),
        "ttft_p50_s": _round(_percentile(ttfts, 0.5)),
        "ttft_p90_s": _round(_percentile(ttfts, 0.9)),
        "completed_share": _round(_rate(len(completed), len(with_outcome)), 4),
        "outcome_n": len(with_outcome),
        "tool_error_turn_share": _round(_rate(sum(1 for t in turns if t["tool_errors"]), sum(1 for t in turns if t["tool_calls"])), 4),
        "thumbs_up": up,
        "thumbs_down": down,
        "down_rate": _round(_rate(down, up + down), 4),
        "copied_share": _round(_rate(sum(1 for t in turns if t.get("copied")), count), 4),
        "model_time_share": _round(_rate(sum(t["model_ms"] for t in turns) / 1000, sum(durations)), 3),
        "wasted_usd": round(sum(t["wasted"] for t in turns), 2),
    }


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


def _by(turns: List[Dict[str, Any]], key: Any, top: int = 12) -> List[Dict[str, Any]]:
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for turn in turns:
        groups[key(turn)].append(turn)
    table = [{"key": name, **_pillars(members)} for name, members in groups.items()]
    table.sort(key=lambda entry: -entry["cost_usd"])
    return table[:top]


def _call_band(calls: int) -> str:
    for low, high, label in CALL_BANDS:
        if low <= calls <= high:
            return label
    return CALL_BANDS[-1][2]


def _duration_band(seconds: float) -> str:
    for low, high, label in DURATION_BANDS:
        if low <= seconds < high:
            return label
    return DURATION_BANDS[-1][2]


def _tool_table(rows: Iterable[Row], turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    totals: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        for name, entry in row.get("toolCalls", {}).get("M", {}).items():
            fields = entry.get("M", {})
            tool = totals[name]
            tool["calls"] += _num(fields.get("calls"))
            tool["errors"] += _num(fields.get("errors"))
            tool["ms"] += _num(fields.get("durationMs"))
            tool["timed"] += _num(fields.get("timedCalls"))
            tool["chars"] += _num(fields.get("resultChars"))
            for kind, count in fields.get("errorKinds", {}).get("M", {}).items():
                tool["kind_" + kind] += _num(count)
    for turn in turns:
        for name in turn["tools"]:
            totals[name]["turn_cost"] += turn["cost"]
            totals[name]["turns"] += 1
            if turn.get("thumb") == -1:
                totals[name]["down"] += 1
            if turn.get("thumb") in (1, -1):
                totals[name]["thumbed"] += 1
    table = []
    for name, tool in totals.items():
        table.append({
            "tool": name,
            "calls": int(tool["calls"]),
            "errors": int(tool["errors"]),
            "error_rate": _round(_rate(tool["errors"], tool["calls"]), 4),
            "error_kinds": {name[5:]: int(count) for name, count in tool.items() if name.startswith("kind_")},
            "error_rate_excluding_auth": (
                _round(_rate(tool["errors"] - tool["kind_auth"], tool["calls"]), 4)
                if any(name.startswith("kind_") for name in tool) else None
            ),
            "mean_s": _round(tool["ms"] / tool["timed"] / 1000, 2) if tool["timed"] >= FLOOR else None,
            "result_chars_per_call": int(tool["chars"] / tool["timed"]) if tool["timed"] >= FLOOR else None,
            "turns": int(tool["turns"]),
            "cost_of_turns_usd": round(tool["turn_cost"], 2),
            "down_rate": _round(_rate(tool["down"], tool["thumbed"]), 4),
        })
    table.sort(key=lambda entry: -entry["cost_of_turns_usd"])
    return table


def _sessions(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    sessions: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"cost": 0.0, "turns": 0, "tools": 0, "models": Counter()})
    for turn in turns:
        session = sessions[turn["session"]]
        session["cost"] += turn["cost"]
        session["turns"] += 1
        session["tools"] += turn["tool_calls"]
        session["models"][turn["model"]] += turn["cost"]
    last_turn: Dict[str, Dict[str, Any]] = {}
    for turn in turns:
        previous = last_turn.get(turn["session"])
        if previous is None or turn["stamp"] > previous["stamp"]:
            last_turn[turn["session"]] = turn
    judged = [t for t in last_turn.values() if t["outcome"]]
    endings = Counter(
        t["outcome"] if t["outcome"] in BAD_ENDINGS
        else "empty answer" if t["empty"]
        else "tool error" if t["tool_errors"] > t["error_kinds"].get("auth", 0)
        else "clean"
        for t in judged
    )
    costs = sorted((s["cost"] for s in sessions.values()), reverse=True)
    total = sum(costs) or 1.0
    light_premium = [
        s for s in sessions.values()
        if s["turns"] < 10 and s["tools"] == 0 and _is_premium(s["models"].most_common(1)[0][0])
    ]
    return {
        "conversations": len(costs),
        "top_1pct_share": round(sum(costs[: max(1, len(costs) // 100)]) / total, 3),
        "top_5pct_share": round(sum(costs[: max(1, len(costs) // 20)]) / total, 3),
        "premium_light_conversations": len(light_premium),
        "premium_light_usd": round(sum(s["cost"] for s in light_premium), 2),
        "endings": dict(endings),
        "ended_badly_share": _round(_rate(len(judged) - endings.get("clean", 0), len(judged)), 4),
    }


def _output_composition(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per model: how much of the measured output was thinking. An estimate:
    signature characters stand in for billed reasoning tokens."""
    groups: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for turn in turns:
        if not turn["measured_output_tokens"]:
            continue
        group = groups[turn["model"]]
        group["turns"] += 1
        for field in ("measured_output_tokens", "text_chars", "reasoning_chars", "signature_chars", "output_cost"):
            group[field] += turn[field]
    table = []
    for model, group in groups.items():
        reasoning_tokens = group["signature_chars"] / REASONING_SIGNATURE_CHARS_PER_TOKEN
        table.append({
            "model": model,
            "turns": int(group["turns"]),
            "output_tokens": int(group["measured_output_tokens"]),
            "text_chars": int(group["text_chars"]),
            "reasoning_chars": int(group["reasoning_chars"]),
            "signature_chars": int(group["signature_chars"]),
            "est_reasoning_share": (
                _round(min(1.0, reasoning_tokens / group["measured_output_tokens"]), 3)
                if group["turns"] >= FLOOR and group["measured_output_tokens"] else None
            ),
        })
    table.sort(key=lambda entry: -entry["output_tokens"])
    return table


def _leads(summary: Dict[str, Any]) -> List[str]:
    """Mechanical leads only. Ranking them is the reader's job."""
    leads: List[str] = []
    this_week, last_week = summary["this_week"], summary["last_week"]
    for field, label, worse_when_up in (
        ("cost_per_turn", "cost per turn", True),
        ("turn_p50_s", "median turn time", True),
        ("ttft_p50_s", "median first token", True),
        ("completed_share", "completed share", False),
        ("down_rate", "thumbs-down rate", True),
        ("tool_error_turn_share", "share of tool turns with an error", True),
    ):
        now, before = this_week.get(field), last_week.get(field)
        if now is None or before is None or not before:
            continue
        change = (now - before) / before
        if abs(change) >= 0.10:
            direction = "worse" if (change > 0) == worse_when_up else "better"
            leads.append(f"{label} moved {change:+.0%} week over week ({before} -> {now}): {direction}")
    for tool in summary["tools_this_week"]:
        rate = tool["error_rate_excluding_auth"] if tool["error_rate_excluding_auth"] is not None else tool["error_rate"]
        if tool["calls"] >= TOOL_LEAD_MIN_CALLS and (rate or 0) >= TOOL_LEAD_ERROR_RATE:
            basis = " (auth retries excluded)" if tool["error_rate_excluding_auth"] is not None else ""
            leads.append(f"tool `{tool['tool']}` failed {rate:.0%} of {tool['calls']} calls in the last 7 days{basis}")
    bad = summary["sessions"]["ended_badly_share"]
    if bad is not None and bad >= 0.05:
        leads.append(f"{bad:.0%} of conversations ended on an error, a stop, an empty answer or a failed tool")
    statuses = {entry["status"]: entry for entry in summary["cache"]}
    idle = statuses.get("miss_ttl_expired")
    if idle and idle["share"] >= 0.20:
        leads.append(f"calls after the cache expired are {idle['share']:.0%} of spend (${idle['cost_usd']})")
    unexplained = sum(statuses.get(name, {}).get("wasted_usd", 0) for name in ("partial_miss", "miss_avoidable"))
    if unexplained >= 0.02 * summary["window"]["cost_usd"]:
        leads.append(f"unexplained cache waste is ${unexplained:.0f}, over 2% of spend")
    sessions = summary["sessions"]
    if sessions["premium_light_usd"] >= 0.02 * summary["window"]["cost_usd"]:
        leads.append(
            f"premium models on short, tool-free conversations: ${sessions['premium_light_usd']} "
            f"across {sessions['premium_light_conversations']}"
        )
    multi = [band for band in summary["turn_shape"] if band["key"] != "1"]
    multi_share = sum(band["time_share"] or 0 for band in multi)
    if multi_share >= 0.40:
        leads.append(f"turns with 2+ model calls carry {multi_share:.0%} of all turn time")
    return leads


def build(items: List[Row], start: str, end: str, previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rows, feedback = split_rows(items)
    rows = [r for r in rows if start <= (_text(r.get("timestamp")) or "")[:10] <= end]
    turns = build_turns(rows)
    thumbs, implicit = _feedback(feedback)
    for turn in turns:
        key = (turn["session"], turn["message_id"])
        thumb = thumbs.get(key)
        turn["thumb"] = thumb["value"] if thumb else None
        turn["retried"] = bool(thumb and thumb["retried"])
        turn["copied"] = implicit.get(key, {}).get("copy", 0) > 0
        turn["continued"] = implicit.get(key, {}).get("continue", 0) > 0

    end_day = date.fromisoformat(end)
    week_start = (end_day - timedelta(days=6)).isoformat()
    prior_start = (end_day - timedelta(days=13)).isoformat()
    this_week = [t for t in turns if t["day"] >= week_start]
    last_week = [t for t in turns if prior_start <= t["day"] < week_start]

    total_cost = sum(_cost(r) for r in rows) or 1.0
    total_time = sum(t["duration_s"] for t in turns) or 1.0
    cache: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        status = cache[_text(row.get("cacheStatus")) or "unknown"]
        status["calls"] += 1
        status["cost"] += _cost(row)
        status["wasted"] += _num(row.get("wastedUsd"))

    turn_shape = []
    for entry in _by(turns, lambda t: _call_band(t["calls"])):
        members = [t for t in turns if _call_band(t["calls"]) == entry["key"]]
        entry["time_share"] = round(sum(t["duration_s"] for t in members) / total_time, 3)
        entry["cost_share"] = round(entry["cost_usd"] / total_cost, 3)
        turn_shape.append(entry)

    timed_tool_turns = [t for t in turns if t["tool_timed"]]
    tool_turns = [t for t in turns if t["tool_calls"]]
    summary: Dict[str, Any] = {
        "window": {
            "start": start, "end": end, "calls": len(rows), "cost_usd": round(total_cost, 2),
            "users": len({_text(r.get("userId")) for r in rows}), "thumbs_joined": sum(1 for t in turns if t["thumb"]),
            "thumbs_read": len(thumbs),
        },
        "overall": _pillars(turns),
        "this_week": _pillars(this_week),
        "last_week": _pillars(last_week),
        "cache": sorted(
            ({"status": name, "calls": int(s["calls"]), "cost_usd": round(s["cost"], 2),
              "share": round(s["cost"] / total_cost, 3), "wasted_usd": round(s["wasted"], 2)} for name, s in cache.items()),
            key=lambda entry: -entry["cost_usd"],
        ),
        "models": _by(turns, lambda t: t["model"]),
        "versions": _by([t for t in turns if t["version"]], lambda t: t["version"], top=6),
        "turn_shape": turn_shape,
        "duration_bands": _by(turns, lambda t: _duration_band(t["duration_s"])),
        "tool_error_split": _by(tool_turns, lambda t: "tool error" if t["tool_errors"] else "clean"),
        "surface": _by(turns, lambda t: "project" if t["project"] else "agent" if t["agent"] else "plain chat"),
        "documents": _by(turns, lambda t: "with documents" if t["has_documents"] else "no documents"),
        "tool_time_share": _round(
            _rate(sum(t["tool_ms"] for t in timed_tool_turns) / 1000, sum(t["duration_s"] for t in timed_tool_turns)), 3
        ),
        "tools": _tool_table(rows, turns)[:25],
        "tools_this_week": _tool_table([r for r in rows if (_text(r.get("timestamp")) or "")[:10] >= week_start], this_week),
        "sessions": _sessions(turns),
        "quality": {
            "outcomes": dict(Counter(t["outcome"] for t in turns if t["outcome"])),
            "empty_answers": sum(1 for t in turns if t["empty"]),
            "rewound_turns": sum(1 for t in turns if t["rewound"]),
            "down_reasons": dict(Counter(v["reason"] or "none" for v in thumbs.values() if v["value"] == -1)),
            "down_then_retried": sum(1 for v in thumbs.values() if v["retried"]),
            "implicit_signals": dict(sum((counts for counts in implicit.values()), Counter())),
            "tool_error_kinds": dict(sum((t["error_kinds"] for t in turns), Counter())),
            "estimated_usage_calls": sum(1 for r in rows if "usageEstimated" in r),
        },
        "coverage": {
            "turns_with_outcome": _round(_rate(sum(1 for t in turns if t["outcome"]), len(turns)), 3),
            "turns_with_ttft": _round(_rate(sum(1 for t in turns if t["ttft_ms"]), len(turns)), 3),
            "tool_turns_with_timing": _round(_rate(len(timed_tool_turns), len(tool_turns)), 3),
            "turns_with_thumb": _round(_rate(sum(1 for t in turns if t["thumb"]), len(turns)), 4),
            "turns_with_version": _round(_rate(sum(1 for t in turns if t["version"]), len(turns)), 3),
            "turns_with_implicit_signal": _round(_rate(sum(1 for t in turns if t["copied"] or t["continued"]), len(turns)), 4),
            "tool_errors_with_kind": _round(
                _rate(sum(sum(t["error_kinds"].values()) for t in turns), sum(t["tool_errors"] for t in turns)), 3
            ),
            "output_tokens_with_composition": _round(
                _rate(sum(t["measured_output_tokens"] for t in turns), sum(t["output_tokens"] for t in turns)), 3
            ),
        },
        "output": _output_composition(turns),
    }
    summary["leads"] = _leads(summary)
    if previous:
        summary["previous_run"] = {"window": previous.get("window"), "this_week": previous.get("this_week")}
    return summary


def _cell(value: Any) -> str:
    return "—" if value is None else str(value)


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1%}"


def _pillar_table(rows: List[Dict[str, Any]], label: str) -> List[str]:
    lines = [
        f"| {label} | Turns | Cost | $/turn | Turn p50 / p90 (s) | First token p50 (s) | Model time | Completed | Copied | Down rate (n) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        thumbs = row["thumbs_up"] + row["thumbs_down"]
        per_turn = "—" if row["cost_per_turn"] is None else f"${row['cost_per_turn']}"
        lines.append(
            f"| {row.get('key', '')} | {row['turns']} | ${row['cost_usd']} | {per_turn} | "
            f"{_cell(row['turn_p50_s'])} / {_cell(row['turn_p90_s'])} | {_cell(row['ttft_p50_s'])} | "
            f"{_pct(row['model_time_share'])} | {_pct(row['completed_share'])} | {_pct(row['copied_share'])} | "
            f"{_pct(row['down_rate'])} ({thumbs}) |"
        )
    return lines


def render(summary: Dict[str, Any]) -> str:
    window = summary["window"]
    lines = [
        f"# Efficiency review, {window['start']} to {window['end']}",
        "",
        f"{window['calls']} calls, ${window['cost_usd']}, {window['users']} users, "
        f"{summary['sessions']['conversations']} conversations. `—` is under the floor of {FLOOR}, never zero.",
        "",
        "## Coverage (read first)",
        "",
    ]
    lines += [f"- {name.replace('_', ' ')}: {_pct(value)}" for name, value in summary["coverage"].items()]
    lines += ["", "## Leads", ""]
    lines += [f"- {lead}" for lead in summary["leads"]] or ["- none over threshold"]
    lines += ["", "## Week over week", ""]
    lines += _pillar_table([{"key": "last 7 days", **summary["this_week"]}, {"key": "7 days before", **summary["last_week"]}], "Window")
    for title, key, label in (
        ("By model", "models", "Model"),
        ("By release", "versions", "Version"),
        ("By model calls per turn", "turn_shape", "Calls"),
        ("By turn length", "duration_bands", "Length"),
        ("Tool turns, with and without an error", "tool_error_split", "Tool turns"),
        ("By surface", "surface", "Surface"),
        ("By documents", "documents", "Documents"),
    ):
        lines += ["", f"## {title}", ""] + _pillar_table(summary[key], label)
    lines += ["", f"Tool execution is {_pct(summary['tool_time_share'])} of turn time in timed tool turns.", "", "## Cache", ""]
    lines += ["| Status | Calls | Cost | Share | Wasted |", "|---|---:|---:|---:|---:|"]
    lines += [
        f"| {c['status']} | {c['calls']} | ${c['cost_usd']} | {c['share']:.1%} | ${c['wasted_usd']} |" for c in summary["cache"]
    ]
    lines += ["", "## Tools (by cost of the turns that used them)", ""]
    lines += [
        "| Tool | Calls | Error rate | Excl. auth | Mean s | Result chars | Turn cost | Down rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| `{t['tool']}` | {t['calls']} | {_pct(t['error_rate'])} | {_pct(t['error_rate_excluding_auth'])} | {_cell(t['mean_s'])} | "
        f"{_cell(t['result_chars_per_call'])} | ${t['cost_of_turns_usd']} | {_pct(t['down_rate'])} |"
        for t in summary["tools"]
    ]
    lines += ["", "## Output composition (estimate; only calls that recorded it)", ""]
    lines += ["| Model | Turns | Output tokens | Text chars | Reasoning chars | Signature chars | Est. reasoning share |", "|---|---:|---:|---:|---:|---:|---:|"]
    lines += [
        f"| {o['model']} | {o['turns']} | {o['output_tokens']} | {o['text_chars']} | {o['reasoning_chars']} | "
        f"{o['signature_chars']} | {_pct(o['est_reasoning_share'])} |"
        for o in summary["output"]
    ] or ["| none recorded yet | | | | | | |"]
    sessions, quality = summary["sessions"], summary["quality"]
    lines += [
        "", "## Concentration and quality signals", "",
        f"- top 1% of conversations: {sessions['top_1pct_share']:.0%} of spend; top 5%: {sessions['top_5pct_share']:.0%}",
        f"- premium model on short, tool-free conversations: ${sessions['premium_light_usd']} across {sessions['premium_light_conversations']}",
        f"- outcomes: {quality['outcomes']}; empty answers: {quality['empty_answers']}; rewound turns: {quality['rewound_turns']}",
        f"- down-thumb reasons: {quality['down_reasons']}; followed by a retry: {quality['down_then_retried']}",
        f"- implicit signals: {quality['implicit_signals']}",
        f"- how conversations ended (last turn): {sessions['endings']}; not clean: {_pct(sessions['ended_badly_share'])}",
        f"- tool error kinds: {quality['tool_error_kinds']}",
        f"- calls with estimated usage (stopped mid-generation): {quality['estimated_usage_calls']}",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", required=True, nargs="+", help="DynamoDB scan JSON of C# and F# rows (one file or several)")
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--previous", help="the previous run's summary.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    items = [row for path in args.rows for row in _load_items(path)]
    previous = json.loads(Path(args.previous).read_text()) if args.previous else None
    summary = build(items, args.window_start, args.window_end, previous)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    (out / "review.md").write_text(render(summary))
    print(render(summary))


if __name__ == "__main__":
    main()
