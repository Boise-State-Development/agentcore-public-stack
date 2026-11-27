"""Session-prewarm "prepare" A/B readout: the pure logic (docs/specs/turn-path-ttft.md §5 P7).

A prepare builds a turn's agent ahead of the send. A holdout decides per
conversation whether a requested prepare runs (``prepareArm: "treated"``) or is
replaced by a plain warm (``"holdout"``). This module reads the content-free
records the arms leave behind and compares the two. It lives beside
:mod:`ledger_prelude` because it reads the same Runtime log group the same way;
``backend/scripts/prepare_ab_readout.py`` is the thin shell that fetches.

Records (every one a JSON object; session ids are used to join and never printed):

``turn_prelude`` / ``turn_first_token`` / ``turn_prepare``
    Runtime log lines (``turn_timing.py``). Each is logged twice, once as plain
    text and once inside an OpenTelemetry JSON envelope; :func:`parse_runtime_line`
    reads either and :func:`dedupe` keeps one.
client EMF
    app-api's turn-timing record (``ClientFirstTokenMs`` plus ``prepareArm``,
    ``turnKind``, ``modelId``, ``visible``). A null property is omitted from an
    EMF record, so an absent ``prepareArm`` means "not in the experiment".

**One turn per prepare.** Only the first turn after a prepare or a holdout carries
``prepareArm`` (and ``preparedAgeMs``), on the server lines and the client record
alike: a prepare can only help that turn, and a follow-up counted in its arm would
pull the two medians together.

**Percentiles are nearest-rank** on the sorted values: ``rank = ceil(p/100 * n)``,
the value at that 1-based rank. No interpolation, so a percentile is always a
value that was observed. The comparison is the difference of nearest-rank p50s
(treated minus holdout) with a bootstrap 95% interval (:data:`RESAMPLES`
resamples per arm, stdlib ``random`` with a caller-fixed seed).

**Floor.** An arm with fewer than ``min_n`` turns yields ``None`` for every number
about it, rendered ``—``: a value under its floor is no evidence, never zero.

**Arms are per conversation, turns cluster.** Arm balance is therefore judged on
distinct conversations, against the configured holdout share, at a binomial
3-sigma.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ARMS = ("treated", "holdout")
RESAMPLES = 2000
CACHED_BUILD_MS = 150
NEVER_FOLLOWED_WINDOW_S = 15 * 60
#: Same record, same payload, within this many seconds: the plain and envelope copies.
DUPLICATE_WINDOW_S = 5.0
#: A mix share moving by more than this (total variation distance) between arms raises a caveat.
MIX_SHIFT_TVD = 0.10
#: Treated cached-build share must exceed holdout's by this much for "mechanism works".
MECHANISM_MIN_LIFT = 0.20
QUERY_LIMIT = 10000

RUNTIME_MARKERS = ("turn_prelude", "turn_first_token", "turn_prepare")
_MARKER = re.compile(r"\b(turn_prelude|turn_first_token|turn_prepare) (\{)")
_DECODER = json.JSONDecoder()

Record = Dict[str, Any]
#: One fetched line: ``(epoch seconds, text)``.
Line = Tuple[float, str]

RUNTIME_QUERY = (
    "fields @timestamp, @message\n"
    "| filter @message like /turn_prelude|turn_first_token|turn_prepare/\n"
    "| sort @timestamp asc\n"
    f"| limit {QUERY_LIMIT}"
)
CLIENT_QUERY = (
    "fields @timestamp, @message\n"
    "| filter ispresent(ClientFirstTokenMs)\n"
    "| sort @timestamp asc\n"
    f"| limit {QUERY_LIMIT}"
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _decode_after_marker(text: str) -> Optional[Tuple[str, Record]]:
    match = _MARKER.search(text)
    if not match:
        return None
    try:
        payload, _ = _DECODER.raw_decode(text, match.start(2))
    except ValueError:
        return None
    return (match.group(1), payload) if isinstance(payload, dict) else None


def parse_runtime_line(text: str) -> Optional[Tuple[str, Record]]:
    """``(kind, payload)`` for a plain or OTEL-wrapped Runtime line, else ``None``.

    ``kind`` is ``turn_prelude``, ``turn_first_token`` or ``turn_prepare``. A
    malformed line, or one whose payload has no ``sessionId``, is ``None``.
    """
    candidates: List[str] = [text]
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            candidates = list(_strings(json.loads(stripped))) or [text]
        except ValueError:
            candidates = [text]
    for candidate in candidates:
        found = _decode_after_marker(candidate)
        if found and isinstance(found[1].get("sessionId"), str):
            return found
    return None


def parse_client_line(text: str) -> Optional[Record]:
    """The EMF record for one client turn-timing line, else ``None``."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("ClientFirstTokenMs")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if payload.get("source", "spa") != "spa":
        return None
    return payload


def _key(payload: Record) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def dedupe(records: Iterable[Record]) -> List[Record]:
    """Drop a record whose payload equals an earlier one's within :data:`DUPLICATE_WINDOW_S`.

    Records carry ``_ts`` (epoch seconds). The plain and envelope copies of one
    log call differ only by delivery jitter; two real records with identical
    payloads inside five seconds are not a thing (every one carries timings).
    """
    seen: Dict[str, float] = {}
    out: List[Record] = []
    for record in sorted(records, key=lambda r: r["_ts"]):
        payload = {k: v for k, v in record.items() if k != "_ts"}
        key = _key(payload)
        last = seen.get(key)
        if last is not None and record["_ts"] - last <= DUPLICATE_WINDOW_S:
            continue
        seen[key] = record["_ts"]
        out.append(record)
    return out


def parse_runtime(lines: Iterable[Line]) -> Dict[str, List[Record]]:
    """``{prelude, first_token, prepare}`` of deduplicated records from fetched lines."""
    buckets: Dict[str, List[Record]] = {"prelude": [], "first_token": [], "prepare": []}
    names = {"turn_prelude": "prelude", "turn_first_token": "first_token", "turn_prepare": "prepare"}
    for ts, text in lines:
        parsed = parse_runtime_line(text)
        if parsed:
            buckets[names[parsed[0]]].append({**parsed[1], "_ts": float(ts)})
    return {name: dedupe(items) for name, items in buckets.items()}


def parse_client(lines: Iterable[Line]) -> List[Record]:
    records = []
    for ts, text in lines:
        payload = parse_client_line(text)
        if payload:
            records.append({**payload, "_ts": float(ts)})
    return dedupe(records)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile: the value at 1-based rank ``ceil(p/100 * n)``; ``None`` when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def bootstrap_median_diff(
    treated: Sequence[float], holdout: Sequence[float], *, seed: int, resamples: int = RESAMPLES,
) -> Optional[Dict[str, Any]]:
    """Treated minus holdout nearest-rank p50, with a bootstrap 95% interval.

    ``insideNoise`` is true when the interval contains 0. Deterministic for a seed.
    """
    if not treated or not holdout:
        return None
    rng = random.Random(seed)
    nt, nh = len(treated), len(holdout)
    diffs = []
    for _ in range(resamples):
        t = [treated[rng.randrange(nt)] for _ in range(nt)]
        h = [holdout[rng.randrange(nh)] for _ in range(nh)]
        diffs.append(percentile(t, 50) - percentile(h, 50))  # type: ignore[operator]
    low, high = percentile(diffs, 2.5), percentile(diffs, 97.5)
    point = percentile(treated, 50) - percentile(holdout, 50)  # type: ignore[operator]
    return {
        "point": point,
        "low": low,
        "high": high,
        "insideNoise": bool(low <= 0 <= high),  # type: ignore[operator]
    }


def compare(
    treated: Sequence[Optional[float]], holdout: Sequence[Optional[float]], *, min_n: int, seed: int,
) -> Dict[str, Any]:
    """One metric, both arms. Every number about an arm under ``min_n`` is ``None``.

    ``n`` counts the values present, so a metric a record lacks is judged on the
    records that have it.
    """
    t = [float(v) for v in treated if isinstance(v, (int, float)) and not isinstance(v, bool)]
    h = [float(v) for v in holdout if isinstance(v, (int, float)) and not isinstance(v, bool)]

    def side(values: List[float]) -> Dict[str, Any]:
        enough = len(values) >= min_n
        return {
            "n": len(values),
            "p50": percentile(values, 50) if enough else None,
            "p90": percentile(values, 90) if enough else None,
        }

    available = len(t) >= min_n and len(h) >= min_n
    return {
        "treated": side(t),
        "holdout": side(h),
        "diff": bootstrap_median_diff(t, h, seed=seed) if available else None,
        "available": available,
    }


# ---------------------------------------------------------------------------
# Joining and grouping
# ---------------------------------------------------------------------------


def eligible_preludes(preludes: Iterable[Record]) -> List[Record]:
    """Turns in the experiment that were not resumes."""
    return [p for p in preludes if p.get("prepareArm") in ARMS and not p.get("isResume")]


def join_first_tokens(preludes: Sequence[Record], first_tokens: Sequence[Record]) -> Tuple[List[Record], int]:
    """Attach ``firstTokenMs`` to each prelude from the nearest later ``turn_first_token`` of its session.

    A first-token record is used once, and only if it lands before that
    session's next prelude. Returns ``(preludes with firstTokenMs or None,
    joined count)``; the inputs are not mutated.
    """
    by_session: Dict[str, List[Record]] = {}
    for record in first_tokens:
        by_session.setdefault(record["sessionId"], []).append(record)
    for items in by_session.values():
        items.sort(key=lambda r: r["_ts"])
    starts: Dict[str, List[float]] = {}
    for record in preludes:
        starts.setdefault(record["sessionId"], []).append(record["_ts"])
    for items in starts.values():
        items.sort()
    out, joined = [], 0
    used: set = set()
    for record in sorted(preludes, key=lambda r: r["_ts"]):
        sid = record["sessionId"]
        later = [t for t in starts[sid] if t > record["_ts"]]
        horizon = later[0] if later else math.inf
        match = next(
            (f for f in by_session.get(sid, []) if id(f) not in used and record["_ts"] <= f["_ts"] < horizon), None,
        )
        merged = dict(record)
        merged["firstTokenMs"] = None
        if match is not None and isinstance(match.get("firstTokenMs"), (int, float)):
            used.add(id(match))
            merged["firstTokenMs"] = match["firstTokenMs"]
            joined += 1
        out.append(merged)
    return out, joined


def _by_arm(records: Iterable[Record]) -> Dict[str, List[Record]]:
    arms: Dict[str, List[Record]] = {arm: [] for arm in ARMS}
    for record in records:
        if record.get("prepareArm") in arms:
            arms[record["prepareArm"]].append(record)
    return arms


def _group(record: Record, name: str) -> Optional[float]:
    value = (record.get("groups") or {}).get(name)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


SERVER_METRICS: Dict[str, Callable[[Record], Optional[float]]] = {
    "totalMs": lambda r: r.get("totalMs"),
    "agent_build": lambda r: _group(r, "agent_build"),
    "preamble": lambda r: _group(r, "preamble"),
    "firstTokenMs": lambda r: r.get("firstTokenMs"),
}


def server_comparison(turns: Sequence[Record], *, min_n: int, seed: int) -> Dict[str, Any]:
    arms = _by_arm(turns)
    return {
        name: compare([fn(r) for r in arms["treated"]], [fn(r) for r in arms["holdout"]], min_n=min_n, seed=seed)
        for name, fn in SERVER_METRICS.items()
    }


def _share_cached(turns: Sequence[Record], *, min_n: int) -> Dict[str, Any]:
    values = [_group(t, "agent_build") for t in turns]
    values = [v for v in values if v is not None]
    if len(values) < min_n:
        return {"n": len(values), "share": None}
    return {"n": len(values), "share": sum(1 for v in values if v < CACHED_BUILD_MS) / len(values)}


def mechanism(turns: Sequence[Record], *, min_n: int) -> Dict[str, Any]:
    arms = _by_arm(turns)
    treated = _share_cached(arms["treated"], min_n=min_n)
    holdout = _share_cached(arms["holdout"], min_n=min_n)
    ages = [t["preparedAgeMs"] for t in arms["treated"] if isinstance(t.get("preparedAgeMs"), (int, float))]
    enough = len(ages) >= min_n
    contaminated = sum(1 for t in arms["holdout"] if t.get("preparedAgeMs") is not None)
    lift = (
        treated["share"] - holdout["share"]
        if treated["share"] is not None and holdout["share"] is not None
        else None
    )
    return {
        "treated": treated,
        "holdout": holdout,
        "lift": lift,
        "works": lift is not None and lift >= MECHANISM_MIN_LIFT,
        "preparedAge": {
            "n": len(ages),
            "p50": percentile(ages, 50) if enough else None,
            "p90": percentile(ages, 90) if enough else None,
        },
        "holdoutWithPreparedAge": contaminated,
    }


def prepares_summary(
    prepares: Sequence[Record], preludes: Sequence[Record], *, window_end: float, min_n: int,
) -> Dict[str, Any]:
    """Count by outcome, cacheHit share, ``ms`` percentiles and the share never followed by a send.

    A prepare within 15 minutes of ``window_end`` cannot be judged yet and is
    left out of the never-followed share.
    """
    reasons: Dict[str, int] = {}
    for record in prepares:
        label = f"{'prepared' if record.get('prepared') else 'not prepared'}/{record.get('reason') or 'none'}"
        reasons[label] = reasons.get(label, 0) + 1
    prelude_times: Dict[str, List[float]] = {}
    for record in preludes:
        prelude_times.setdefault(record["sessionId"], []).append(record["_ts"])
    judged = followed = 0
    for record in prepares:
        if record["_ts"] + NEVER_FOLLOWED_WINDOW_S > window_end:
            continue
        judged += 1
        if any(record["_ts"] < t <= record["_ts"] + NEVER_FOLLOWED_WINDOW_S
               for t in prelude_times.get(record["sessionId"], [])):
            followed += 1
    n = len(prepares)
    ms = [p["ms"] for p in prepares if isinstance(p.get("ms"), (int, float))]
    prepared = [p for p in prepares if p.get("prepared")]
    enough = n >= min_n
    return {
        "n": n,
        "byReason": dict(sorted(reasons.items())),
        "cacheHitShare": (sum(1 for p in prepared if p.get("cacheHit")) / len(prepared)) if enough and prepared else None,
        "ms": {"p50": percentile(ms, 50) if enough else None, "p90": percentile(ms, 90) if enough else None},
        "neverFollowed": {
            "judged": judged,
            "share": ((judged - followed) / judged) if judged >= min_n else None,
        },
    }


# ---------------------------------------------------------------------------
# Caveats
# ---------------------------------------------------------------------------


def _shares(values: Iterable[Any]) -> Dict[Any, float]:
    items = list(values)
    counts: Dict[Any, int] = {}
    for value in items:
        counts[value] = counts.get(value, 0) + 1
    return {k: v / len(items) for k, v in counts.items()} if items else {}


def _tvd(a: Dict[Any, float], b: Dict[Any, float]) -> float:
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def _mix_shift(arms: Dict[str, List[Record]], field: str, *, min_n: int) -> Optional[float]:
    if min(len(arms["treated"]), len(arms["holdout"])) < min_n:
        return None
    return _tvd(_shares(r.get(field) for r in arms["treated"]), _shares(r.get(field) for r in arms["holdout"]))


def _stamp(record: Record) -> str:
    return f"{record.get('appVersion') or 'unstamped'}#{record.get('build') or '-'}"


def build_caveats(
    *,
    all_preludes: Sequence[Record],
    turns: Sequence[Record],
    client: Sequence[Record],
    holdout_percent: float,
    min_n: int,
    window: Tuple[float, float],
) -> List[Dict[str, Any]]:
    """Each ``{id, fired, text, note}``. A fired caveat blocks "effect confirmed"; a ``note`` (the
    not-in-experiment share) is informational, never fires and never blocks."""
    out: List[Dict[str, Any]] = []

    def add(cid: str, fired: bool, text: str, note: bool = False) -> None:
        out.append({"id": cid, "fired": fired, "text": text, "note": note})

    arms = _by_arm(turns)
    stamps = {arm: sorted({_stamp(r) for r in arms[arm]}) for arm in ARMS}
    union = sorted(set(stamps["treated"]) | set(stamps["holdout"]))
    when = (
        f"{datetime.fromtimestamp(window[0], timezone.utc):%Y-%m-%d %H:%M} to "
        f"{datetime.fromtimestamp(window[1], timezone.utc):%Y-%m-%d %H:%M} UTC"
    )
    add(
        "mixed_builds",
        len(union) > 1,
        f"Window {when}. Builds seen: treated {', '.join(stamps['treated']) or 'none'}; "
        f"holdout {', '.join(stamps['holdout']) or 'none'}."
        + (" The window mixes builds." if len(union) > 1 else ""),
    )
    n_t, n_h = len(arms["treated"]), len(arms["holdout"])
    add("n_per_arm", False, f"Eligible server turns (arm set, not a resume): treated {n_t}, holdout {n_h}.")

    sessions = {arm: {r["sessionId"] for r in arms[arm]} for arm in ARMS}
    conversations = len(sessions["treated"]) + len(sessions["holdout"])
    if conversations:
        p = holdout_percent / 100.0
        observed = len(sessions["holdout"]) / conversations
        bound = 3 * math.sqrt(p * (1 - p) / conversations)
        add(
            "arm_imbalance",
            abs(observed - p) > bound,
            f"Arm balance: {len(sessions['holdout'])} of {conversations} conversations in holdout "
            f"({observed:.1%}) against {holdout_percent:g}% configured (3-sigma bound ±{bound:.1%}).",
        )
    else:
        add("arm_imbalance", False, "Arm balance: no conversations in either arm.")

    for arm, n in (("treated", n_t), ("holdout", n_h)):
        add("arm_under_floor", n < min_n,
            f"{arm.capitalize()} arm: {n} eligible turns against a floor of {min_n}"
            + (": every comparison for it prints —." if n < min_n else "."))
    out[-2:] = [
        {"id": "arm_under_floor", "fired": any(c["fired"] for c in out[-2:]), "note": False,
         "text": " ".join(c["text"] for c in out[-2:])},
    ]

    seen = [p for p in all_preludes if not p.get("isResume")]
    null_share = (sum(1 for p in seen if p.get("prepareArm") not in ARMS) / len(seen)) if seen else 0.0
    add(
        "not_in_experiment",
        False,
        f"Turns with no prepareArm (not in the experiment): {null_share:.1%} of {len(seen)} non-resume turns "
        "(expected to be most turns: only the first turn after a prepare or a holdout carries an arm).",
        note=True,
    )

    shift = _mix_shift(arms, "hasAssistant", min_n=min_n)
    add(
        "mix_shift_has_assistant",
        shift is not None and shift > MIX_SHIFT_TVD,
        "Mix, hasAssistant: " + ("unchecked (an arm is under the floor)." if shift is None
                                 else f"arms differ by {shift:.1%} (total variation)."),
    )

    carms = _by_arm(client)
    for field, cid in (("turnKind", "mix_shift_turn_kind"), ("modelId", "mix_shift_model")):
        cshift = _mix_shift(carms, field, min_n=min_n)
        add(
            cid,
            cshift is not None and cshift > MIX_SHIFT_TVD,
            f"Mix, client {field}: " + ("unchecked (an arm is under the floor)." if cshift is None
                                        else f"arms differ by {cshift:.1%} (total variation)."),
        )
    return out


# ---------------------------------------------------------------------------
# Readout
# ---------------------------------------------------------------------------


def _improves(diff: Optional[Dict[str, Any]]) -> bool:
    return bool(diff) and not diff["insideNoise"] and diff["point"] < 0


def verdict(report: Dict[str, Any]) -> Tuple[str, str]:
    """``(verdict, rule)`` from fixed rules, evaluated in this order.

    1. server ``totalMs`` unavailable (an arm under the floor): *cannot tell*.
    2. server ``totalMs`` and client first token both improve (interval excludes
       0, treated faster) and no caveat fired: *effect confirmed*.
    3. the cached-build share is higher in treated by at least 20 points:
       *mechanism works, user-facing effect not shown*.
    4. server and client both available: *no effect* (no improvement, and the
       mechanism did not show).
    5. otherwise: *cannot tell*.
    """
    server = report["server"]["overall"]["totalMs"]
    client = report["client"]["overall"]
    if not server["available"]:
        return "cannot tell", "an arm is under the floor on the server comparison"
    fired = [c["id"] for c in report["caveats"] if c["fired"]]
    if _improves(server["diff"]) and client["available"] and _improves(client["diff"]) and not fired:
        return "effect confirmed", "server totalMs and client first token both improve, intervals exclude 0, no caveat fired"
    if report["mechanism"]["works"]:
        return (
            "mechanism works, user-facing effect not shown",
            "treated turns build from a cached agent at least 20 points more often, "
            "but the user-facing comparison is not confirmed",
        )
    if client["available"]:
        return "no effect", "no confirmed improvement and the cached-build share did not rise"
    return "cannot tell", "the client comparison is unavailable and the mechanism is not shown"


def build_report(
    *,
    runtime: Dict[str, List[Record]],
    client: Sequence[Record],
    window: Tuple[float, float],
    holdout_percent: float,
    min_n: int,
    seed: int,
    include_hidden: bool = False,
) -> Dict[str, Any]:
    """Everything the readout prints, as data."""
    preludes = runtime["prelude"]
    turns, joined = join_first_tokens(eligible_preludes(preludes), runtime["first_token"])
    visible = [c for c in client if include_hidden or c.get("visible") is True]
    in_experiment = [c for c in visible if c.get("prepareArm") in ARMS]
    by_assistant = {
        flag: server_comparison([t for t in turns if bool(t.get("hasAssistant")) is flag], min_n=min_n, seed=seed)
        for flag in (True, False)
    }
    kinds = sorted({c.get("turnKind") for c in in_experiment if c.get("turnKind")})
    report: Dict[str, Any] = {
        "window": window,
        "minN": min_n,
        "holdoutPercent": holdout_percent,
        "seed": seed,
        "joined": {"joined": joined, "of": len(turns)},
        "server": {
            "overall": server_comparison(turns, min_n=min_n, seed=seed),
            "byHasAssistant": by_assistant,
        },
        "client": {
            "overall": compare(
                [c["ClientFirstTokenMs"] for c in in_experiment if c["prepareArm"] == "treated"],
                [c["ClientFirstTokenMs"] for c in in_experiment if c["prepareArm"] == "holdout"],
                min_n=min_n, seed=seed,
            ),
            "byTurnKind": {
                kind: compare(
                    [c["ClientFirstTokenMs"] for c in in_experiment
                     if c["prepareArm"] == "treated" and c.get("turnKind") == kind],
                    [c["ClientFirstTokenMs"] for c in in_experiment
                     if c["prepareArm"] == "holdout" and c.get("turnKind") == kind],
                    min_n=min_n, seed=seed,
                )
                for kind in kinds
            },
            "visibleOnly": not include_hidden,
        },
        "mechanism": mechanism(turns, min_n=min_n),
        "prepares": prepares_summary(runtime["prepare"], preludes, window_end=window[1], min_n=min_n),
        "caveats": build_caveats(
            all_preludes=preludes, turns=turns, client=in_experiment, holdout_percent=holdout_percent,
            min_n=min_n, window=window,
        ),
    }
    report["verdict"], report["verdictRule"] = verdict(report)
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

DASH = "—"


def _ms(value: Optional[float]) -> str:
    return DASH if value is None else f"{value:,.0f} ms"


def _pct(value: Optional[float]) -> str:
    return DASH if value is None else f"{value:.1%}"


def _diff(cmp: Dict[str, Any]) -> str:
    diff = cmp.get("diff")
    if not diff:
        return DASH
    note = "inside noise" if diff["insideNoise"] else ("treated faster" if diff["point"] < 0 else "treated slower")
    return f"{diff['point']:+,.0f} ms (95% {diff['low']:+,.0f} to {diff['high']:+,.0f}), {note}"


def _table(title: str, comparisons: Dict[str, Dict[str, Any]]) -> List[str]:
    lines = [f"**{title}**", "", "| Metric | Treated n | Treated p50 / p90 | Holdout n | Holdout p50 / p90 | Median difference |",
             "|---|---|---|---|---|---|"]
    for name, cmp in comparisons.items():
        t, h = cmp["treated"], cmp["holdout"]
        lines.append(
            f"| {name} | {t['n']} | {_ms(t['p50'])} / {_ms(t['p90'])} | {h['n']} | {_ms(h['p50'])} / {_ms(h['p90'])} "
            f"| {_diff(cmp)} |"
        )
    return lines + [""]


def render_markdown(report: Dict[str, Any]) -> str:
    """The readout, caveats first. Content-free: no session ids."""
    out = ["# Prepare A/B readout", "", "## Caveats", ""]
    for caveat in report["caveats"]:
        if not caveat["note"]:
            out.append(f"- {'**FIRED** ' if caveat['fired'] else ''}{caveat['text']}")
    out += ["", "Notes (informational, never block the verdict):", ""]
    out += [f"- {c['text']}" for c in report["caveats"] if c["note"]]
    out += [
        "",
        f"Floor: {report['minN']} turns per arm; under it a number prints {DASH}. "
        f"Percentiles are nearest-rank; intervals are a {RESAMPLES}-resample bootstrap, seed {report['seed']}.",
        "",
        "## Server, eligible turns (arm set, not a resume), treated vs holdout",
        "",
        f"First token joined from `turn_first_token` for {report['joined']['joined']} of {report['joined']['of']} turns.",
        "",
    ]
    out += _table("All turns", report["server"]["overall"])
    for flag, label in ((True, "Agent turns (hasAssistant)"), (False, "Plain turns (no assistant)")):
        out += _table(label, report["server"]["byHasAssistant"][flag])
    visible = "visible turns only" if report["client"]["visibleOnly"] else "all turns"
    out += [f"## Client first token ({visible})", ""]
    out += _table("Overall", {"ClientFirstTokenMs": report["client"]["overall"]})
    if report["client"]["byTurnKind"]:
        out += _table("By turn kind", {k: v for k, v in report["client"]["byTurnKind"].items()})
    mech = report["mechanism"]
    out += [
        "## Mechanism check", "",
        f"- Agent build under {CACHED_BUILD_MS} ms (a cached agent): treated {_pct(mech['treated']['share'])} "
        f"(n {mech['treated']['n']}), holdout {_pct(mech['holdout']['share'])} (n {mech['holdout']['n']}).",
        f"- preparedAgeMs on treated turns: p50 {_ms(mech['preparedAge']['p50'])}, p90 {_ms(mech['preparedAge']['p90'])} "
        f"(n {mech['preparedAge']['n']}). Holdout turns carrying a preparedAgeMs: {mech['holdoutWithPreparedAge']}.",
        "",
    ]
    prep = report["prepares"]
    out += [
        "## Prepares", "",
        f"- {prep['n']} `turn_prepare` records: "
        + (", ".join(f"{k} {v}" for k, v in prep["byReason"].items()) or "none") + ".",
        f"- cacheHit share of prepared: {_pct(prep['cacheHitShare'])}; ms p50 {_ms(prep['ms']['p50'])}, "
        f"p90 {_ms(prep['ms']['p90'])}.",
        f"- Never followed by a turn within 15 minutes: {_pct(prep['neverFollowed']['share'])} "
        f"of {prep['neverFollowed']['judged']} judged (those inside the last 15 minutes of the window are left out).",
        "",
        "## Verdict", "",
        f"**{report['verdict']}**",
        "",
        f"Rule: {report['verdictRule']}. Fixed rules: 'effect confirmed' needs server totalMs and client first "
        "token both faster with intervals excluding 0 and no caveat fired; 'mechanism works, user-facing effect "
        "not shown' needs the cached-build share up by 20 points or more; 'no effect' needs both comparisons "
        "available; otherwise 'cannot tell'.",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Fetch (the runner is injected; boto3 only in :func:`logs_insights_runner`)
# ---------------------------------------------------------------------------

Runner = Callable[[str, float, float, str], List[Line]]


def fetch_lines(run: Runner, log_group: str, start: float, end: float, query: str) -> List[Line]:
    """Run ``query`` over ``[start, end)``, bisecting any span that hits the row limit."""
    rows = run(log_group, start, end, query)
    if len(rows) < QUERY_LIMIT or end - start <= 1:
        return rows
    mid = (start + end) / 2
    return fetch_lines(run, log_group, start, mid, query) + fetch_lines(run, log_group, mid, end, query)


def _timestamp(value: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    raise ValueError("unrecognised @timestamp")


def logs_insights_runner(client: Any, *, timeout: float = 300.0, sleep: Callable[[float], None] = time.sleep) -> Runner:
    """A :data:`Runner` over a CloudWatch Logs client. Raises ``RuntimeError`` on failure or timeout."""

    def run(log_group: str, start: float, end: float, query: str) -> List[Line]:
        query_id = client.start_query(
            logGroupName=log_group, startTime=int(start), endTime=int(end), queryString=query, limit=QUERY_LIMIT,
        )["queryId"]
        deadline = time.monotonic() + timeout
        while True:
            response = client.get_query_results(queryId=query_id)
            status = response.get("status")
            if status == "Complete":
                lines: List[Line] = []
                for row in response.get("results") or []:
                    cells = {c.get("field"): c.get("value") for c in row}
                    if "@timestamp" in cells and "@message" in cells:
                        try:
                            lines.append((_timestamp(cells["@timestamp"]), cells["@message"]))
                        except ValueError:
                            continue
                return lines
            if status in ("Failed", "Cancelled", "Timeout", "Unknown"):
                raise RuntimeError(f"query ended {status}")
            if time.monotonic() >= deadline:
                try:
                    client.stop_query(queryId=query_id)
                except Exception:  # noqa: BLE001 - best effort
                    pass
                raise RuntimeError("query timed out")
            sleep(1.0)

    return run


_DURATION = re.compile(r"^(\d+)([dhm])$")


def parse_when(value: str, *, now: float) -> float:
    """Epoch seconds for an ISO date or datetime (UTC if naive) or a relative ``7d`` / ``12h`` / ``30m`` before ``now``."""
    match = _DURATION.match(value.strip())
    if match:
        unit = {"d": 86400, "h": 3600, "m": 60}[match.group(2)]
        return now - int(match.group(1)) * unit
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
