"""Counterfactual cost replay: what would each session have cost under another compaction policy?

The cost half of the compaction experiments; the quality half is
``compaction_quality_harness.py``. A strategy only ships if this says it saves
money AND that harness says it loses no facts.

Input is a content-free scan of the ``C#`` cost rows (the same scan the
``usage-patterns-dashboard`` skill runs). Each session's calls are replayed in
order and re-priced under every arm. The observed rows are the control, so an
arm's number is a *delta* against what production actually billed: anything
the model cannot change (output, uncached input) is carried over unchanged.

Arms (``--arms``):

  observed              Production as billed (the control).
  idle:<T>k:<F>k[:<G>]  Idle compaction. A background job runs <G> seconds
                        (default 300, the cache TTL) after a session goes
                        quiet with at least <T>k tokens of context, and cuts
                        its history to <F>k. The user's next call re-writes the
                        smaller prefix, and every later call reads less. Each
                        job costs ``--summary-usd`` whether the user returns or
                        not, which is why the return rate is reported.
  cold:<T>k:<F>k        The same cut, made on the user's return instead (in
                        the turn). No job is wasted on a session that never
                        returns, but the summarizer sits in front of the first
                        token (~14 s measured on dev, 2026-09-26): it is
                        priced here to show what the TTFT budget is buying.
  ttl1h                 A 1h cache TTL on every cache point. Every write costs
                        2x base instead of 1.25x; a return within the hour
                        reads the prefix instead of re-writing it.
  ttl1h+idle:<T>k:<F>k  Both: 1h TTL, and an idle job after 3600 s.

Model, stated so the reader can disagree with it:

- **Rates** are recovered per call from the row's own cost and tokens
  (write = 1.25x base, read = 0.1x base), so every model prices at what it
  was billed. A call with neither falls back to its model's median base.
- **History** is a call's context (uncached + read + write tokens) minus the
  session's smallest context, which stands in for the static prefix
  (system + tools + the first message). That under-counts what a cut can
  remove, so savings are conservative.
- **A cut** removes ``history - max(F, tail + summary)`` tokens, where the
  tail is the last ``--protected-turns`` user turns (production never cuts
  them; turns are inferred from 60 s gaps, as the dashboard does) and the
  summary is ``--summary-tokens`` (3.4k measured on dev). A session whose tail
  alone is over F cuts less, or not at all: production's own
  ``floor_unreachable``. The removal carries forward to
  every later call (reads first, then writes) until production's own
  compaction shrinks the observed context by at least as much.
- **Idle** is a gap over ``--ttl`` seconds between consecutive calls (the
  dashboard's definition). A session's last call is an idle with no return,
  unless it falls within ``--censor-hours`` of the window end (then unknown,
  and excluded from the return rate).
- **What it cannot see:** per-message content, so tool-result clearing and
  summary quality are out of scope; the protected tail is folded into F.

Usage (stdlib only; run with ``python3 -I`` like the dashboard scripts):

    python3 -I scripts/compaction_cost_replay.py --cost-rows <scan.json> \\
        --window-end 2026-11-04 --arms observed,idle:50k:20k,idle:100k:20k,cold:50k:20k,ttl1h

``--json <path>`` writes the same tables as JSON. The output is aggregate
only: no session ids, user ids or titles.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

WRITE_5M = 1.25
WRITE_1H = 2.0
READ = 0.1
DEFAULT_TTL_SECONDS = 300
HOUR_SECONDS = 3600
# 2026-09-26 dev cut: extraction + narrative on Nova 2 Lite, 19,566 in / 2,832 out,
# producing a 3,394-token summary.
DEFAULT_SUMMARY_USD = 0.015
DEFAULT_SUMMARY_TOKENS = 3_400
DEFAULT_PROTECTED_TURNS = 3
TURN_GAP_SECONDS = 60
CONTEXT_BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("<25k", 0, 25_000),
    ("25-50k", 25_000, 50_000),
    ("50-100k", 50_000, 100_000),
    ("100k+", 100_000, float("inf")),
)
GAP_BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("5-15m", 300, 900),
    ("15-60m", 900, 3_600),
    ("1-4h", 3_600, 14_400),
    ("4-24h", 14_400, 86_400),
    ("1d+", 86_400, float("inf")),
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def unmarshal(value: Dict[str, Any]) -> Any:
    (kind, raw), = value.items()
    if kind == "S":
        return raw
    if kind == "N":
        return float(raw)
    if kind in ("BOOL", "NULL"):
        return None if kind == "NULL" else raw
    if kind == "M":
        return {k: unmarshal(v) for k, v in raw.items()}
    if kind == "L":
        return [unmarshal(v) for v in raw]
    return raw


def dig(d: Any, *keys: str, default: Any = 0) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


@dataclass
class Call:
    ts: datetime
    model: str
    uncached: float
    read: float
    write: float
    cost: float
    read_cost: float
    write_cost: float
    base: Optional[float]  # $/token at 1x, recovered from this row

    @property
    def context(self) -> float:
        return self.uncached + self.read + self.write


def call_from_row(row: Dict[str, Any]) -> Optional[Call]:
    cost = row.get("cost")
    if not isinstance(cost, dict) or cost.get("total") is None:
        return None
    read = float(dig(row, "tokenUsage", "cacheReadInputTokens"))
    write = float(dig(row, "tokenUsage", "cacheWriteInputTokens"))
    read_cost = float(cost.get("cacheReadCost") or 0)
    write_cost = float(cost.get("cacheWriteCost") or 0)
    base = None
    if write > 0 and write_cost > 0:
        base = write_cost / (WRITE_5M * write)
    elif read > 0 and read_cost > 0:
        base = read_cost / (READ * read)
    return Call(
        ts=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
        model=str(dig(row, "modelInfo", "modelId", default="unknown")),
        uncached=float(dig(row, "tokenUsage", "inputTokens")),
        read=read,
        write=write,
        cost=float(cost["total"]),
        read_cost=read_cost,
        write_cost=write_cost,
        base=base,
    )


def load_sessions(items: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, List[Call]], int]:
    """Group rows into sessions of priced calls. API-converse sessions are dropped (no cachePoint)."""
    sessions: Dict[str, List[Call]] = collections.defaultdict(list)
    skipped = 0
    for item in items:
        row = {k: unmarshal(v) for k, v in item.items()} if _is_marshalled(item) else item
        sid = str(row.get("sessionId", ""))
        if not sid or sid.startswith("api-converse"):
            skipped += 1
            continue
        call = call_from_row(row)
        if call is None:
            skipped += 1
            continue
        sessions[sid].append(call)
    for calls in sessions.values():
        calls.sort(key=lambda c: c.ts)
    return dict(sessions), skipped


def _is_marshalled(item: Dict[str, Any]) -> bool:
    ts = item.get("timestamp")
    return isinstance(ts, dict) and len(ts) == 1


def fill_bases(sessions: Dict[str, List[Call]]) -> None:
    """Give a call with no recoverable rate its model's median base (then the fleet's)."""
    by_model: Dict[str, List[float]] = collections.defaultdict(list)
    for calls in sessions.values():
        for c in calls:
            if c.base:
                by_model[c.model].append(c.base)
    medians = {m: statistics.median(v) for m, v in by_model.items()}
    fleet = statistics.median([b for v in by_model.values() for b in v]) if by_model else 0.0
    for calls in sessions.values():
        for c in calls:
            if not c.base:
                c.base = medians.get(c.model, fleet)


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    name: str
    kind: str  # observed | idle | cold | ttl1h | ttl1h+idle
    trigger: float = 0.0
    target: float = 0.0
    idle_after: float = DEFAULT_TTL_SECONDS
    ttl: float = DEFAULT_TTL_SECONDS

    @property
    def compacts(self) -> bool:
        return self.kind in ("idle", "cold", "ttl1h+idle")

    @property
    def write_multiplier(self) -> float:
        return WRITE_1H if self.kind.startswith("ttl1h") else WRITE_5M


def parse_arm(spec: str, ttl: float) -> Arm:
    spec = spec.strip()
    if spec == "observed":
        return Arm(spec, "observed", ttl=ttl)
    if spec == "ttl1h":
        return Arm(spec, "ttl1h", ttl=HOUR_SECONDS)
    kind, _, rest = spec.partition(":")
    parts = rest.split(":")
    if kind not in ("idle", "cold", "ttl1h+idle") or len(parts) < 2:
        raise ValueError(f"unknown arm {spec!r}")
    trigger, target = (_tokens(p) for p in parts[:2])
    if target >= trigger:
        raise ValueError(f"arm {spec!r}: target must be below the trigger")
    if kind == "ttl1h+idle":
        return Arm(spec, kind, trigger, target, idle_after=HOUR_SECONDS, ttl=HOUR_SECONDS)
    idle_after = float(parts[2]) if len(parts) > 2 else ttl
    return Arm(spec, kind, trigger, target, idle_after=idle_after, ttl=ttl)


def _tokens(text: str) -> float:
    text = text.strip().lower()
    return float(text[:-1]) * 1000 if text.endswith("k") else float(text)


@dataclass
class ArmResult:
    cost: float = 0.0
    summary_cost: float = 0.0
    cuts: int = 0
    wasted_cuts: int = 0
    tokens_removed: float = 0.0
    sessions_cut: int = 0
    by_band: Dict[str, float] = field(default_factory=lambda: collections.defaultdict(float))


def context_band(tokens: float) -> str:
    return next(name for name, lo, hi in CONTEXT_BANDS if lo <= tokens < hi)


@dataclass(frozen=True)
class CutModel:
    summary_usd: float = DEFAULT_SUMMARY_USD
    summary_tokens: float = DEFAULT_SUMMARY_TOKENS
    protected_turns: int = DEFAULT_PROTECTED_TURNS


def replay(calls: List[Call], arm: Arm, model: CutModel, result: ArmResult) -> None:
    """Re-price one session under ``arm`` and add it to ``result``."""
    static = min(c.context for c in calls)
    removed = 0.0           # tokens the arm has cut that production still carries
    prev: Optional[Call] = None
    turn_ends: List[float] = []  # context of the last call before each user turn
    cut_this_session = False

    def cut(at: Call, *, wasted: bool = False) -> None:
        nonlocal removed, cut_this_session
        tail = _tail_tokens(at, turn_ends, model.protected_turns)
        removed, did = _cut(at, static, tail, removed, arm, model, result)
        if did:
            result.summary_cost += model.summary_usd
            result.wasted_cuts += int(wasted)
            cut_this_session = True

    for c in calls:
        gap = (c.ts - prev.ts).total_seconds() if prev else None
        if prev is not None:
            if gap is not None and gap > TURN_GAP_SECONDS:
                turn_ends.append(prev.context)
            # Production's own compaction shrank the context: it has caught up with the arm.
            drop = prev.context - c.context
            if drop > 0:
                removed = max(0.0, removed - drop)
            if gap is not None and arm.compacts and gap > (arm.ttl if arm.kind == "cold" else arm.idle_after):
                cut(prev)
        cost = _price(c, prev, gap, removed, arm)
        result.cost += cost
        result.by_band[context_band(c.context)] += cost
        prev = c
    # The job after the last call runs whether or not anyone comes back.
    if arm.compacts and arm.kind != "cold" and prev is not None:
        turn_ends.append(prev.context)
        cut(prev, wasted=True)
    if cut_this_session:
        result.sessions_cut += 1


def _tail_tokens(at: Call, turn_ends: List[float], protected_turns: int) -> float:
    """Tokens in the last ``protected_turns`` user turns up to and including ``at``.

    ``turn_ends`` holds the context of the last call of every finished turn,
    ``at``'s own turn last. The tail starts after the turn ``protected_turns``
    back from it. Too few turns: everything is tail, as in production.
    """
    if len(turn_ends) <= protected_turns:
        return at.context
    return max(0.0, at.context - turn_ends[-(protected_turns + 1)])


def _cut(at: Call, static: float, tail: float, removed: float, arm: Arm, model: CutModel,
         result: ArmResult) -> Tuple[float, bool]:
    ctx = at.context - removed
    hist = ctx - static
    keep = max(arm.target, min(hist, tail) + model.summary_tokens)
    if ctx < arm.trigger or hist <= keep:
        return removed, False
    extra = hist - keep
    result.cuts += 1
    result.tokens_removed += extra
    return removed + extra, True


def _price(c: Call, prev: Optional[Call], gap: Optional[float], removed: float, arm: Arm) -> float:
    """Cost of ``c`` under ``arm``: observed, with its cached tokens re-priced."""
    if arm.kind == "observed":
        return c.cost
    base = c.base or 0.0
    read, write = c.read, c.write
    # A 1h TTL turns a return within the hour into a read of what the previous call cached.
    # In observed tokens: the cut below comes off the same prefix either way.
    if arm.kind.startswith("ttl1h") and prev is not None and gap is not None and DEFAULT_TTL_SECONDS < gap <= arm.ttl:
        hit = max(min(prev.context, read + write), read)
        read, write = hit, (c.read + c.write) - hit
    # A cut prefix is smaller: take the removal from reads first, then writes.
    cut_read = min(read, removed)
    cut_write = min(write, removed - cut_read)
    read -= cut_read
    write -= cut_write
    other = c.cost - c.read_cost - c.write_cost
    return other + read * READ * base + write * arm.write_multiplier * base


# ---------------------------------------------------------------------------
# Step 0: the idle cliff, measured
# ---------------------------------------------------------------------------


def idle_profile(sessions: Dict[str, List[Call]], ttl: float, window_end: datetime,
                 censor_hours: float) -> Dict[str, Any]:
    total = sum(c.cost for calls in sessions.values() for c in calls)
    idle_write = collections.defaultdict(float)
    idle_write_gap = collections.defaultdict(float)
    idle_calls = collections.defaultdict(int)
    returns = collections.defaultdict(lambda: [0, 0])  # band -> [returned, idle events]
    censor = window_end - timedelta(hours=censor_hours)
    for calls in sessions.values():
        for a, b in zip(calls, calls[1:] + [None]):
            band = context_band(a.context)
            if b is None:
                if a.ts < censor:
                    returns[band][1] += 1
                continue
            gap = (b.ts - a.ts).total_seconds()
            if gap <= ttl:
                continue
            returns[band][0] += 1
            returns[band][1] += 1
            idle_write[context_band(b.context)] += b.write_cost
            idle_write_gap[next(n for n, lo, hi in GAP_BANDS if lo <= gap < hi)] += b.write_cost
            idle_calls[context_band(b.context)] += 1
    return {
        "total_usd": total,
        "idle_rewrite_usd": sum(idle_write.values()),
        "by_context": {n: {"usd": idle_write[n], "calls": idle_calls[n]} for n, _, _ in CONTEXT_BANDS},
        "by_gap": {n: idle_write_gap[n] for n, _, _ in GAP_BANDS},
        "return_rate": {n: {"returned": r, "idle_events": e, "rate": (r / e if e else None)}
                        for n, (r, e) in ((n, returns[n]) for n, _, _ in CONTEXT_BANDS)},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(sessions: Dict[str, List[Call]], arms: List[Arm], model: CutModel) -> Dict[str, ArmResult]:
    results = {arm.name: ArmResult() for arm in arms}
    for calls in sessions.values():
        for arm in arms:
            replay(calls, arm, model, results[arm.name])
    return results


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cost-rows", required=True, help="DynamoDB scan JSON of C# rows")
    p.add_argument("--window-end", required=True, help="YYYY-MM-DD the scan ran up to (for censoring)")
    p.add_argument("--arms", default="observed,idle:50k:20k,idle:100k:25k,cold:50k:20k,ttl1h,ttl1h+idle:50k:20k")
    p.add_argument("--ttl", type=float, default=DEFAULT_TTL_SECONDS)
    p.add_argument("--summary-usd", type=float, default=DEFAULT_SUMMARY_USD)
    p.add_argument("--summary-tokens", type=float, default=DEFAULT_SUMMARY_TOKENS)
    p.add_argument("--protected-turns", type=int, default=DEFAULT_PROTECTED_TURNS)
    p.add_argument("--censor-hours", type=float, default=72.0)
    p.add_argument("--json", help="also write the tables here")
    args = p.parse_args(argv)

    with open(args.cost_rows) as fh:
        items = json.load(fh).get("Items", [])
    sessions, skipped = load_sessions(items)
    fill_bases(sessions)
    window_end = datetime.fromisoformat(args.window_end).replace(tzinfo=timezone.utc)
    arms = [parse_arm(s, args.ttl) for s in args.arms.split(",") if s.strip()]
    if not any(a.kind == "observed" for a in arms):
        arms.insert(0, parse_arm("observed", args.ttl))

    profile = idle_profile(sessions, args.ttl, window_end, args.censor_hours)
    model = CutModel(args.summary_usd, args.summary_tokens, args.protected_turns)
    results = run(sessions, arms, model)
    _print(profile, results, arms, len(sessions), sum(len(c) for c in sessions.values()), skipped)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"profile": profile, "arms": {n: _arm_json(r) for n, r in results.items()},
                       "model": model.__dict__, "ttl": args.ttl}, fh, indent=1)
    return 0


def _arm_json(r: ArmResult) -> Dict[str, Any]:
    return {"cost": r.cost, "summary_cost": r.summary_cost, "cuts": r.cuts, "wasted_cuts": r.wasted_cuts,
            "tokens_removed": r.tokens_removed, "sessions_cut": r.sessions_cut, "by_band": dict(r.by_band)}


def _print(profile: Dict[str, Any], results: Dict[str, ArmResult], arms: List[Arm],
           n_sessions: int, n_calls: int, skipped: int) -> None:
    total = profile["total_usd"]
    out = sys.stdout.write
    out(f"{n_sessions:,} sessions, {n_calls:,} priced calls ({skipped:,} rows skipped), ${total:,.2f} billed\n\n")
    idle = profile["idle_rewrite_usd"]
    out(f"Idle re-write (cache writes on calls after a {int(arms[0].ttl)}s+ pause): "
        f"${idle:,.2f} = {100 * idle / total:.1f}% of spend\n")
    out("  by context at return: " + "  ".join(
        f"{n} ${v['usd']:,.0f} ({v['calls']:,})" for n, v in profile["by_context"].items()) + "\n")
    out("  by pause length:      " + "  ".join(f"{n} ${v:,.0f}" for n, v in profile["by_gap"].items()) + "\n")
    out("  return rate after a pause, by context: " + "  ".join(
        f"{n} {100 * v['rate']:.0f}% of {v['idle_events']:,}" if v["rate"] is not None else f"{n} —"
        for n, v in profile["return_rate"].items()) + "\n\n")

    baseline = results[next(a.name for a in arms if a.kind == "observed")]
    control = baseline.cost
    out(f"{'arm':<24}{'total $':>11}{'delta $':>10}{'delta %':>9}{'jobs $':>9}{'cuts':>7}{'wasted':>8}{'sessions':>10}{'Mtok cut':>10}\n")
    for arm in arms:
        r = results[arm.name]
        t = r.cost + r.summary_cost
        out(f"{arm.name:<24}{t:>11,.2f}{t - control:>+10,.2f}{100 * (t - control) / control:>+8.1f}%"
            f"{r.summary_cost:>9,.2f}{r.cuts:>7,}{r.wasted_cuts:>8,}{r.sessions_cut:>10,}{r.tokens_removed / 1e6:>10,.1f}\n")
    out("\nDelta by context band (call's observed context; jobs not included):\n")
    out(f"{'arm':<24}" + "".join(f"{n:>11}" for n, _, _ in CONTEXT_BANDS) + "\n")
    for arm in arms:
        r = results[arm.name]
        out(f"{arm.name:<24}" + "".join(
            f"{r.by_band.get(n, 0) - baseline.by_band.get(n, 0):>+11,.2f}" for n, _, _ in CONTEXT_BANDS) + "\n")


if __name__ == "__main__":
    sys.exit(main())
