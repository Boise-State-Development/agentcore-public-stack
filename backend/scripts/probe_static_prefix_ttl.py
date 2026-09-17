"""Does a 1h TTL on the tools + system cachePoints pay for itself on Claude/Bedrock?

The gate for AGENTCORE_PROMPT_CACHE_STATIC_PREFIX_TTL=1h
(docs/specs/compaction-model-relative-thresholds.md §3.6, PR-5). A caching
default must never be adopted on inspection alone — #954 shipped on a wrong
premise and measured 57% more expensive live before #956 reverted it — so this
script measures the two arms against the same static prefix:

  arm 5m : tools + system points with no ttl (today's shape)
  arm 1h : tools + system points with ttl "1h"; message point unchanged

Per arm: call 1 (the write), sleep --gap-seconds, call 2 (the read-or-rewrite).
It reports cacheRead / cacheWrite per call, whether Bedrock accepted the 1h
point at all, and prices the pair at the model's own rates — 1.25x base for a
5m write, 2x base for a 1h write, 0.1x for a read — so the break-even is read
off the output rather than argued.

What "pays" means: with a gap between 5 and 60 minutes, the 1h arm's second
call should READ the static prefix (cacheRead ≈ static tokens) where the 5m
arm re-WRITES it. Over a session the 1h arm costs +0.75x base on every static
write and saves 1.15x base on every cold-within-the-hour return; it pays when
the second event is more frequent than the first. Run this with a gap of
~420s (past 5m) and again with ~60s (inside 5m) to see both regimes.

Read-only apart from the model invocations. ⚠️ Real spend: four calls at
~8k input tokens each plus the sleep. Nothing is written anywhere.

Usage:
    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/probe_static_prefix_ttl.py --gap-seconds 420
    AWS_PROFILE=dev-ai uv run python scripts/probe_static_prefix_ttl.py --gap-seconds 60 --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0

Baseline, dev-ai us-west-2, 2026-09-16, Haiku 4.5, gap 420s:
    5m  first  read 0     write 6251   second  read 0     write 6251
    1h  first  read 0     write 6251   second  read 5924  write 327   <- honored
    pair $0.017197 (5m) vs $0.015130 (1h): 1h CHEAPER by 12% at this gap
Same day, gap 60s (both arms warm):
    5m  second read 6251 write 0 ; 1h second read 6251 write 0
    pair $0.009289 (5m) vs $0.014446 (1h): 1h MORE EXPENSIVE by $0.005157
    = the 0.75x-base premium on the first write, nothing to recover inside 5m
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List

import boto3

# Clears every Claude family's cache minimum (4,096 on Haiku) with margin.
_SYSTEM_TEXT = ("You are a careful assistant. " * 40 + "Policy: answer briefly. ") * 20
_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "toolSpec": {
            "name": f"tool_{i}",
            "description": "A probe tool that does nothing useful. " * 12,
            "inputSchema": {"json": {"type": "object", "properties": {"q": {"type": "string"}}}},
        }
    }
    for i in range(6)
]

RATES = {  # $/MTok base input, Global CRIS; adjust for the model under test
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": 1.10,
    "global.anthropic.claude-sonnet-5": 2.00,
    "us.anthropic.claude-sonnet-4-6": 3.30,
}


def _request(model_id: str, ttl: str | None, marker: str) -> Dict[str, Any]:
    point: Dict[str, Any] = {"type": "default"}
    if ttl:
        point["ttl"] = ttl
    return {
        "modelId": model_id,
        "system": [{"text": _SYSTEM_TEXT + f"\nProbe arm: {marker}."}, {"cachePoint": dict(point)}],
        "toolConfig": {"tools": _TOOL_SPECS + [{"cachePoint": dict(point)}]},
        "messages": [{"role": "user", "content": [{"text": "Reply with the single word OK."}, {"cachePoint": {"type": "default"}}]}],
        "inferenceConfig": {"maxTokens": 5},
    }


def _call(client: Any, req: Dict[str, Any]) -> Dict[str, int]:
    resp = client.converse(**req)
    u = resp.get("usage", {})
    return {
        "input": int(u.get("inputTokens", 0)),
        "read": int(u.get("cacheReadInputTokens", 0)),
        "write": int(u.get("cacheWriteInputTokens", 0)),
    }


def _price(usage: Dict[str, int], base: float, write_mult: float) -> float:
    return (usage["input"] * base + usage["read"] * base * 0.1 + usage["write"] * base * write_mult) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-id", default="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--gap-seconds", type=int, default=420)
    ap.add_argument("--base-rate", type=float, default=None, help="$/MTok base input; defaults from a small table")
    args = ap.parse_args()

    base = args.base_rate or RATES.get(args.model_id)
    if base is None:
        print(f"no base rate for {args.model_id}; pass --base-rate", file=sys.stderr)
        return 2
    client = boto3.client("bedrock-runtime", region_name=args.region)

    arms = [("5m", None), ("1h", "1h")]
    results: Dict[str, Any] = {}
    # Distinct markers per arm so the two arms never share a cache entry.
    for name, ttl in arms:
        marker = f"{name}-{int(time.time())}"
        req = _request(args.model_id, ttl, marker)
        try:
            first = _call(client, req)
        except Exception as e:  # noqa: BLE001
            results[name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        results[name] = {"first": first, "marker": marker}
    if all("error" in r for r in results.values()):
        print(results)
        return 1

    print(f"sleeping {args.gap_seconds}s so the 5m entry {'expires' if args.gap_seconds > 300 else 'stays warm'} ...")
    time.sleep(args.gap_seconds)

    for name, ttl in arms:
        if "error" in results[name]:
            continue
        # Re-send the SAME request (same marker) — identical bytes.
        try:
            results[name]["second"] = _call(client, _request(args.model_id, ttl, results[name]["marker"]))
        except Exception as e:  # noqa: BLE001
            results[name]["error"] = f"{type(e).__name__}: {e}"

    print(f"\nmodel={args.model_id} base=${base}/MTok gap={args.gap_seconds}s\n")
    print(f"{'arm':<4} {'call':<7} {'input':>7} {'read':>8} {'write':>8} {'$':>10}")
    total = {}
    for name, ttl in arms:
        r = results[name]
        if "error" in r:
            print(f"{name:<4} ERROR {r['error']}")
            continue
        mult = 2.0 if ttl == "1h" else 1.25
        cost = 0.0
        for which in ("first", "second"):
            u = r[which]
            c = _price(u, base, mult)
            cost += c
            print(f"{name:<4} {which:<7} {u['input']:>7} {u['read']:>8} {u['write']:>8} {c:>10.6f}")
        total[name] = cost
        print(f"{name:<4} {'pair':<7} {'':>7} {'':>8} {'':>8} {cost:>10.6f}")
    if "5m" in total and "1h" in total:
        delta = total["1h"] - total["5m"]
        verdict = "1h CHEAPER" if delta < 0 else "1h MORE EXPENSIVE"
        print(f"\n{verdict} by ${abs(delta):.6f} for this pair at a {args.gap_seconds}s gap")
        second = results["1h"].get("second", {})
        if args.gap_seconds > 300 and second.get("read", 0) == 0:
            print("⚠️ the 1h arm did NOT read after the gap — Bedrock may not honor ttl=1h for this model; do not enable the flag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
