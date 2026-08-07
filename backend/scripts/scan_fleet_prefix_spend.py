"""Where does the model spend actually go, across ALL conversations?

Read-only, content-free projection over the sessions-metadata table. Produces
the numbers in ``docs/one-pagers/fleet-prefix-spend-anatomy.md`` and the §4.1
cohort scan in ``docs/specs/compaction-over-threshold-cache-spiral.md``.

Every cost investigation on this platform so far sized *its own cohort* —
attachments (#836), the compaction spiral (#833), the agent-cache bypass
(#834). This asks the flat question instead: across every conversation, short
and long, what share of the dollars is cache writes, how often does the
cacheable prefix mutate mid-session, and what would #838's ``partial_miss``
classifier have said?

**Content-free by construction.** Compaction summaries and message text are
*measured* (length, presence) and never printed, logged, or written to disk.
The only projections requested are cost, token usage, prefix fingerprint
hashes, and compaction coordinates.

**Denominator rule** (from the #836 validation, and it matters — 10.8% of
call rows and 20.0% of session rows carry no cost): rows without a cost are
counted and excluded, never treated as $0. Every printed share names the
universe it is against.

Usage:

    cd backend
    AWS_PROFILE=prod-ai uv run python scripts/scan_fleet_prefix_spend.py \
        --table boisestateai-v2-sessions-metadata --region us-west-2

    # Just the §4.1 cohort half:
    ... scripts/scan_fleet_prefix_spend.py --table <t> --sections cohort
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import boto3

# apis/shared/observability/prompt_cache.py
CACHE_TTL_SECONDS = 300
PARTIAL_MISS_WRITE_READ_RATIO = 3
# agents/main_agent/config/constants.py
COMPACTION_TOKEN_THRESHOLD = 100_000
# #833 PR-2's proposed budget, for the "how many summaries are over it" read.
SUMMARY_TOKEN_BUDGET = 8_000
CHARS_PER_TOKEN = 4


# ── DynamoDB helpers ─────────────────────────────────────────────────────


def scan_all(table, **kwargs) -> Iterator[Dict[str, Any]]:
    """Paginate a scan, yielding raw items."""
    start_key = None
    while True:
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = table.scan(**kwargs)
        for item in response.get("Items", []):
            yield item
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cost_total(raw: Any) -> Optional[float]:
    """Cost is a breakdown map on the streaming path, a bare number on the
    legacy path. Returns None when absent — *unknown*, never 0."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return to_float(raw.get("total"))
    return to_float(raw)


def cost_parts(raw: Any) -> Dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {k: to_float(v) or 0.0 for k, v in raw.items() if k != "total"}


def parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Section 1: fleet spend anatomy + prefix stability ────────────────────


def scan_calls(table) -> List[Dict[str, Any]]:
    projection = (
        "sessionId, #ts, #cost, tokenUsage, prefixFingerprints, cacheStatus, "
        "agentSwitched"
    )
    rows = scan_all(
        table,
        FilterExpression="begins_with(GSI_SK, :c)",
        ExpressionAttributeValues={":c": "C#"},
        ProjectionExpression=projection,
        ExpressionAttributeNames={"#ts": "timestamp", "#cost": "cost"},
    )

    calls: List[Dict[str, Any]] = []
    skipped = 0
    for item in rows:
        total = cost_total(item.get("cost"))
        if total is None:
            skipped += 1
            continue
        usage = item.get("tokenUsage") or {}
        fingerprints = item.get("prefixFingerprints") or {}
        calls.append({
            "session_id": item.get("sessionId", ""),
            "timestamp": item.get("timestamp", ""),
            "cost": total,
            "parts": cost_parts(item.get("cost")),
            "cache_read": to_float(usage.get("cacheReadInputTokens")) or 0.0,
            "cache_write": to_float(usage.get("cacheWriteInputTokens")) or 0.0,
            "system_hash": fingerprints.get("systemPromptHash"),
            "tool_hash": fingerprints.get("toolConfigHash"),
            "status": item.get("cacheStatus"),
            "agent_switched": bool(item.get("agentSwitched")),
        })
    calls.sort(key=lambda c: (c["session_id"], c["timestamp"]))
    print(f"  call rows with a cost (the universe): {len(calls):,}")
    print(f"  call rows without one (excluded):     {skipped:,} "
          f"({skipped / max(1, skipped + len(calls)) * 100:.1f}%)")
    return calls


def report_spend_anatomy(calls: List[Dict[str, Any]]) -> None:
    spend = sum(c["cost"] for c in calls)
    buckets: Counter = Counter()
    for c in calls:
        for key in ("cacheWriteCost", "outputCost", "cacheReadCost", "inputCost"):
            buckets[key] += c["parts"].get(key, 0.0)
    broken = sum(buckets.values()) or 1.0

    print(f"\ntotal recorded model spend: ${spend:,.2f}")
    for key, label in (
        ("cacheWriteCost", "cache WRITE"),
        ("outputCost", "output"),
        ("inputCost", "input (uncached)"),
        ("cacheReadCost", "cache read"),
    ):
        print(f"  {label:<18} ${buckets[key]:>9,.2f}   "
              f"{buckets[key] / broken * 100:>5.1f}%")

    read = sum(c["cache_read"] for c in calls)
    write = sum(c["cache_write"] for c in calls)
    if read:
        print(f"  cache tokens: read {read:,.0f} / write {write:,.0f} "
              f"→ write:read {write / read:.2f}  "
              f"(tokens; the DOLLARS above are what matter — a written token "
              f"costs ~12x a read one)")


def group_sessions(calls: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in calls:
        grouped[c["session_id"]].append(c)
    return grouped


def report_prefix_mutation(sessions: Dict[str, List[Dict[str, Any]]]) -> None:
    multi = {k: v for k, v in sessions.items() if len(v) > 1}
    transitions = sys_flips = tool_flips = 0
    sys_sessions = tool_sessions = 0
    flip_write_cost = 0.0

    for calls in multi.values():
        sf = tf = 0
        for prev, cur in zip(calls, calls[1:]):
            transitions += 1
            if prev["system_hash"] and cur["system_hash"] and prev["system_hash"] != cur["system_hash"]:
                sf += 1
                flip_write_cost += cur["parts"].get("cacheWriteCost", 0.0)
            if prev["tool_hash"] and cur["tool_hash"] and prev["tool_hash"] != cur["tool_hash"]:
                tf += 1
        sys_flips += sf
        tool_flips += tf
        sys_sessions += 1 if sf else 0
        tool_sessions += 1 if tf else 0

    if not transitions:
        return
    print(f"\nsessions with >1 model call: {len(multi):,}   "
          f"transitions: {transitions:,}")
    print(f"  systemPromptHash changed: {sys_flips:,} "
          f"({sys_flips / transitions * 100:.1f}% of transitions) in "
          f"{sys_sessions:,} sessions ({sys_sessions / len(multi) * 100:.1f}%)")
    print(f"  toolConfigHash changed:   {tool_flips:,} "
          f"({tool_flips / transitions * 100:.1f}%) in {tool_sessions:,} "
          f"sessions ({tool_sessions / len(multi) * 100:.1f}%)")
    print(f"  cache-write $ on calls following a system-prompt flip: "
          f"${flip_write_cost:,.2f}")


def report_offline_partial_miss(
    sessions: Dict[str, List[Dict[str, Any]]], total_spend: float
) -> Dict[str, float]:
    """What #838's predicate would have said, applied to historical rows.

    A FLOOR, deliberately: compares each call to its immediate predecessor
    only (the shipped classifier uses a 10-row same-prefix lookback), and
    keeps #838's TTL gate so a legitimately expired entry is never booked as
    waste — the #753 mistake.
    """
    hits: List[tuple] = []
    waste_total = 0.0
    for session_id, calls in sessions.items():
        for prev, cur in zip(calls, calls[1:]):
            if cur["cache_read"] <= 0:
                continue
            if cur["cache_write"] <= PARTIAL_MISS_WRITE_READ_RATIO * cur["cache_read"]:
                continue
            before, after = parse_ts(prev["timestamp"]), parse_ts(cur["timestamp"])
            if not before or not after:
                continue
            if (after - before).total_seconds() > CACHE_TTL_SECONDS:
                continue
            write_cost = cur["parts"].get("cacheWriteCost", 0.0)
            read_cost = cur["parts"].get("cacheReadCost", 0.0)
            waste = 0.0
            if write_cost and read_cost and cur["cache_write"] and cur["cache_read"]:
                premium = (write_cost / cur["cache_write"]) - (read_cost / cur["cache_read"])
                waste = max(0.0, (cur["cache_write"] - cur["cache_read"]) * premium)
            waste_total += waste
            hits.append((session_id, cur["status"], waste))

    print(f"\ncalls classified partial_miss: {len(hits):,}")
    if total_spend:
        print(f"estimated avoidable waste:     ${waste_total:,.2f} "
              f"({waste_total / total_spend * 100:.1f}% of total model spend)")
    print(f"sessions touched:              {len({h[0] for h in hits}):,}")
    print(f"  status those rows carry today: {dict(Counter(h[1] for h in hits))}")
    per_session: Dict[str, float] = defaultdict(float)
    for session_id, _, waste in hits:
        per_session[session_id] += waste
    for session_id, waste in sorted(per_session.items(), key=lambda x: -x[1])[:5]:
        print(f"    {session_id}  ${waste:>8,.2f}")
    return per_session


def report_by_length(
    sessions: Dict[str, List[Dict[str, Any]]],
    partial_miss_by_session: Optional[Dict[str, float]] = None,
) -> None:
    """Is this only a long-conversation problem? (It is not — see the
    one-pager's §4: 43% of spend sits in sessions of 15 calls or fewer.)"""
    waste = partial_miss_by_session or {}
    print(f"\n{'calls in session':<18}{'sessions':>9}{'spend':>11}"
          f"{'w:r':>8}{'sysflips/1k':>13}{'pm waste $':>12}"
          f"{'write-no-read $':>17}")
    for lo, hi, label in ((1, 2, "1"), (2, 6, "2-5"), (6, 16, "6-15"),
                          (16, 41, "16-40"), (41, 10 ** 9, "41+")):
        group = [c for c in sessions.values() if lo <= len(c) < hi]
        if not group:
            continue
        spend = sum(c["cost"] for calls in group for c in calls)
        read = sum(c["cache_read"] for calls in group for c in calls)
        write = sum(c["cache_write"] for calls in group for c in calls)
        transitions = sum(len(calls) - 1 for calls in group)
        flips = sum(
            1
            for calls in group
            for prev, cur in zip(calls, calls[1:])
            if prev["system_hash"] and cur["system_hash"]
            and prev["system_hash"] != cur["system_hash"]
        )
        unread = sum(
            c["parts"].get("cacheWriteCost", 0.0)
            for calls in group
            if sum(x["cache_read"] for x in calls) == 0
            for c in calls
        )
        pm = sum(waste.get(calls[0]["session_id"], 0.0) for calls in group)
        print(f"{label:<18}{len(group):>9,}{spend:>11,.2f}"
              f"{(write / read if read else float('nan')):>8.2f}"
              f"{(flips / transitions * 1000 if transitions else 0):>13.0f}"
              f"{pm:>12,.2f}{unread:>17,.2f}")


# ── Section 2: §4.1 cohort scan ──────────────────────────────────────────


def report_cohort(table) -> None:
    """The #833 §4.1 questions: how big is the over-threshold cohort, how long
    are persisted compaction summaries, and does the checkpoint/anchor
    coordinate mismatch (D3) reproduce outside the incident session?"""
    projection = (
        "sessionId, userId, totalCost, lastContextTokens, "
        "totalCacheReadTokens, totalCacheWriteTokens, partialMissCount, "
        "partialMissUsd, wastedUsd, compaction"
    )
    rows = list(scan_all(
        table,
        FilterExpression="GSI_SK = :meta",
        ExpressionAttributeValues={":meta": "META"},
        ProjectionExpression=projection,
    ))

    priced = [r for r in rows if to_float(r.get("totalCost")) is not None]
    print(f"\nsession rows: {len(rows):,}   with totalCost (universe): "
          f"{len(priced):,}   without: {len(rows) - len(priced):,} "
          f"({(len(rows) - len(priced)) / max(1, len(rows)) * 100:.1f}%)")

    fleet = sum(to_float(r["totalCost"]) for r in priced)
    over = [r for r in priced
            if (to_float(r.get("lastContextTokens")) or 0) > COMPACTION_TOKEN_THRESHOLD]
    over_cost = sum(to_float(r["totalCost"]) for r in over)
    print(f"over-threshold sessions (> {COMPACTION_TOKEN_THRESHOLD:,} tokens): "
          f"{len(over):,} ({len(over) / max(1, len(priced)) * 100:.2f}%), "
          f"${over_cost:,.2f} ({over_cost / max(fleet, 1e-9) * 100:.1f}% of "
          f"${fleet:,.2f})")

    # Summary length — measured, never printed.
    lengths = []
    coords = []
    for r in rows:
        state = r.get("compaction") or {}
        summary = state.get("summary") or ""
        if isinstance(summary, str) and summary:
            lengths.append(len(summary))
        checkpoint = to_float(state.get("checkpoint"))
        anchor = to_float(state.get("truncationAnchor"))
        if checkpoint is not None and anchor is not None:
            coords.append((checkpoint, anchor))

    if lengths:
        lengths.sort()
        budget_chars = SUMMARY_TOKEN_BUDGET * CHARS_PER_TOKEN
        over_budget = [n for n in lengths if n > budget_chars]
        print(f"persisted summaries: {len(lengths):,}   "
              f"median {int(statistics.median(lengths)):,} chars   "
              f"max {lengths[-1]:,} (~{lengths[-1] // CHARS_PER_TOKEN:,} tokens)")
        print(f"  over PR-2's {SUMMARY_TOKEN_BUDGET:,}-token budget "
              f"({budget_chars:,} chars): {len(over_budget):,} "
              f"({len(over_budget) / len(lengths) * 100:.1f}%)")

    if coords:
        mismatched = [c for c in coords if c[0] != c[1]]
        ahead = [c for c in coords if c[1] > c[0]]
        print(f"checkpoint/truncationAnchor pairs: {len(coords):,}   "
              f"mismatched {len(mismatched):,} "
              f"({len(mismatched) / len(coords) * 100:.1f}%)   "
              f"anchor > checkpoint: {len(ahead):,}")


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="sessions-metadata table name")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--sections",
        default="all",
        choices=["all", "spend", "cohort"],
        help="'spend' = fleet anatomy + prefix stability; 'cohort' = #833 §4.1",
    )
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    if args.sections in ("all", "spend"):
        print("=" * 74)
        print("FLEET SPEND ANATOMY — every conversation, short and long")
        print("=" * 74)
        calls = scan_calls(table)
        report_spend_anatomy(calls)
        sessions = group_sessions(calls)
        report_prefix_mutation(sessions)
        waste = report_offline_partial_miss(sessions, sum(c["cost"] for c in calls))
        report_by_length(sessions, waste)

    if args.sections in ("all", "cohort"):
        print()
        print("=" * 74)
        print("§4.1 COHORT SCAN — over-threshold sessions, summaries, D3 coords")
        print("=" * 74)
        report_cohort(table)

    print("\nCAVEATS: the partial_miss figure is an offline reimplementation of "
          "#838's predicate over historical rows — a FLOOR, not the shipped "
          "classifier's output. Fingerprints exist only on rows written since "
          "#697. Rows without a cost are excluded everywhere, never zeroed.")


if __name__ == "__main__":
    main()
