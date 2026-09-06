"""Empirically derive GPT-5.6 rates, and verify the caching path end to end.

The Price List API publishes no commercial-region rates for the GPT-5.6 family
(checked 2026-09-05 across every Bedrock service code — only GovCloud Mantle
rows exist, and `sol` is absent entirely), so
`docs/specs/gpt-5-6-prompt-caching.md` PR-3 cannot verify catalog rates the way
it specifies. This drives real turns to produce a known token split, which a
later Cost Explorer read divides into to recover $/MTok per bucket.

It deliberately does **not** touch the shared dev catalog, RBAC, or the agent
loop. It calls the model through our own transport
(`apis.shared.models.bedrock_responses`), which means one run also exercises,
against a live model for the first time:

- PR-2  the bedrock-runtime base URL, the per-request bearer-token mint, and
        the inference-profile model id
- PR-1  usage normalization — whether the reported buckets are disjoint
- PR-4  explicit cache breakpoints, by comparing `--mode explicit` against
        `--mode implicit`

**Reads only, apart from the model invocations themselves.** Nothing is written
to DynamoDB or to the catalog.

⚠️ Real spend. A run is `--turns` calls against a `--prefix-tokens`-sized
prompt; the script prints an estimate before starting and totals actual tokens
after. Keep it small.

Usage:

    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/probe_gpt56_cache_rates.py \
        --model-id us.openai.gpt-5.6-sol --turns 4

    # A/B the explicit breakpoint against stock implicit caching
    AWS_PROFILE=dev-ai uv run python scripts/probe_gpt56_cache_rates.py \
        --mode both --turns 3

Then, once Cost Explorer has settled (~24h), recover the rates:

    AWS_PROFILE=dev-ai uv run python scripts/probe_gpt56_cache_rates.py \
        --rates-only --since 2026-09-05 \
        --table dev-boisestateai-v2-sessions-metadata

⚠️  Cost Explorer bills these models through AWS Marketplace, under usage types
that name the token bucket and the service tier but NOT the model. Every
OpenAI-family model in the account shares those four rows. A derived rate is
therefore only a given model's rate on a day when it was the ONLY OpenAI-family
model to run — which is what ``--table`` checks and prints.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# A stable, boring preamble. Repeated to reach the requested size so the prefix
# is deterministic across turns — an unstable prefix would defeat the cache and
# make the whole measurement meaningless.
_PREFIX_UNIT = (
    "You are a careful assistant for a university platform. Answer briefly. "
    "Follow institutional policy. Do not speculate. Cite sources when asked. "
)


def build_system_prompt(approx_tokens: int) -> str:
    """Build a deterministic system prompt of roughly ``approx_tokens`` tokens."""
    # ~4 chars/token is the standard rough conversion; exactness does not
    # matter because the model reports the true count back.
    target_chars = approx_tokens * 4
    repeats = max(1, target_chars // len(_PREFIX_UNIT))
    return _PREFIX_UNIT * repeats


@dataclass
class TurnObservation:
    turn: int
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    total_tokens: int
    latency_s: float

    @property
    def disjoint(self) -> bool:
        """Do the buckets partition the reported input total?"""
        return (
            self.input_tokens + self.cache_read + self.cache_write
            == self.total_tokens - self.output_tokens
        )


@dataclass
class ArmResult:
    mode: str
    turns: List[TurnObservation] = field(default_factory=list)
    error: Optional[str] = None

    def totals(self) -> Dict[str, int]:
        return {
            "inputTokens": sum(t.input_tokens for t in self.turns),
            "cacheReadInputTokens": sum(t.cache_read for t in self.turns),
            "cacheWriteInputTokens": sum(t.cache_write for t in self.turns),
            "outputTokens": sum(t.output_tokens for t in self.turns),
        }


async def _run_turn(
    model: Any, system_prompt: str, messages: List[Dict[str, Any]]
) -> TurnObservation:
    """One streamed call; returns the normalized usage the model reported."""
    usage: Dict[str, int] = {}
    started = time.monotonic()
    async for event in model.stream(
        messages,
        system_prompt=system_prompt,
    ):
        if "metadata" in event:
            usage = event["metadata"].get("usage", {}) or {}
    elapsed = time.monotonic() - started
    return TurnObservation(
        turn=0,
        input_tokens=usage.get("inputTokens", 0),
        cache_read=usage.get("cacheReadInputTokens", 0),
        cache_write=usage.get("cacheWriteInputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        total_tokens=usage.get("totalTokens", 0),
        latency_s=elapsed,
    )


async def run_arm(
    mode: str,
    *,
    model_id: str,
    region: str,
    turns: int,
    prefix_tokens: int,
    gap_seconds: float,
    grow_history: bool = False,
    history_chunk_tokens: int = 0,
    prefix_salt: str = "",
) -> ArmResult:
    """Run one arm: N sequential calls sharing one static prefix.

    With ``grow_history`` each turn carries the accumulated conversation, which
    is the scenario PR-4 exists for: history churn behind a stable prefix. A
    fixed single-message workload only shows that explicit mode does not *hurt*.
    """
    from apis.shared.models.bedrock_responses import (
        EXPLICIT_CACHE_ENABLED_ENV,
        build_bedrock_responses_model,
    )

    # The flag is read per call, so flipping it here is enough — no rebuild.
    os.environ[EXPLICIT_CACHE_ENABLED_ENV] = "true" if mode == "explicit" else "false"

    result = ArmResult(mode=f"{mode}+churn" if grow_history else mode)
    system_prompt = build_system_prompt(prefix_tokens)
    if prefix_salt:
        # Distinct prefix bytes -> a distinct cache entry, so the arm starts cold.
        system_prompt = f"[{prefix_salt}] " + system_prompt
    model = build_bedrock_responses_model(
        model_id=model_id, region=region, params={"max_output_tokens": 32}
    )

    label = f"{mode}+churn" if grow_history else mode
    print(f"\n▸ arm={label}  model={model_id}  region={region}  turns={turns}")
    history: List[Dict[str, Any]] = []
    for i in range(1, turns + 1):
        question = f"Reply with the number {i}."
        if history_chunk_tokens:
            question += " Context: " + ("filler context words " * (history_chunk_tokens // 3))
        if grow_history:
            history.append({"role": "user", "content": [{"text": question}]})
            messages = list(history)
        else:
            messages = [{"role": "user", "content": [{"text": question}]}]
        try:
            obs = await _run_turn(model, system_prompt, messages)
        except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
            result.error = f"{type(exc).__name__}: {exc}"
            print(f"   turn {i}: FAILED — {result.error}")
            return result
        obs.turn = i
        result.turns.append(obs)
        if grow_history:
            # Cheap stand-in for the assistant's reply; its exact text does not
            # matter, only that the history grows deterministically.
            history.append({"role": "assistant", "content": [{"text": str(i)}]})
        print(
            f"   turn {i}: input={obs.input_tokens:>7,} "
            f"cacheRead={obs.cache_read:>7,} cacheWrite={obs.cache_write:>7,} "
            f"output={obs.output_tokens:>4,} "
            f"disjoint={'yes' if obs.disjoint else 'NO'} "
            f"({obs.latency_s:.1f}s)"
        )
        if i < turns and gap_seconds:
            await asyncio.sleep(gap_seconds)
    return result


def print_verdicts(results: List[ArmResult]) -> None:
    """Say what each arm proves, or fails to."""
    print("\n" + "=" * 72)
    print("VERDICTS")
    print("=" * 72)

    for r in results:
        print(f"\n▸ arm={r.mode}")
        if r.error:
            print(f"   TRANSPORT: FAILED — {r.error}")
            continue
        if not r.turns:
            print("   no turns recorded")
            continue

        print("   PR-2 transport: reached the model and streamed usage — OK")

        bad = [t.turn for t in r.turns if not t.disjoint]
        print(
            "   PR-1 disjoint buckets: "
            + ("OK on every turn" if not bad else f"VIOLATED on turns {bad}")
        )

        first, rest = r.turns[0], r.turns[1:]
        print(
            f"   turn 1 (cold): write={first.cache_write:,} read={first.cache_read:,}"
        )
        if rest:
            reads = [t.cache_read for t in rest]
            writes = [t.cache_write for t in rest]
            print(f"   turns 2+ read:  min={min(reads):,} max={max(reads):,}")
            print(f"   turns 2+ write: min={min(writes):,} max={max(writes):,}")
            # The check the spec names: a warm turn should READ the static
            # prefix rather than re-write it.
            if max(reads) == 0:
                print("   ⚠️  NO cache reads on warm turns — caching is not engaging.")
            elif min(reads) >= 0.8 * first.total_tokens - first.output_tokens:
                print("   ✅ warm turns read ~the whole prefix — boundary looks right.")
            else:
                print(
                    "   ⚠️  warm reads are well under the prefix — boundary may be "
                    "misplaced (PR-4 kill switch is the way out)."
                )
        if all(t.cache_write == 0 for t in r.turns):
            print(
                "   ⚠️  cacheWrite is 0 on every turn — either the model reports no "
                "writes, or the cache_write_tokens mapping is not landing."
            )

    if len(results) == 2:
        a, b = results
        print(f"\n▸ {a.mode} vs {b.mode} (totals)")
        ta, tb = a.totals(), b.totals()
        for key in ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
            print(f"   {key:<24} {ta.get(key,0):>9,}   {tb.get(key,0):>9,}")


# Cost Explorer names no model. OpenAI-family models on Bedrock bill through
# AWS Marketplace, under usage types that carry the token bucket and the
# service tier but NOT the model id — every OpenAI model in the account lands
# in the same four rows. Verified 2026-09-06 against USAGE_TYPE grouped by
# OPERATION and by BILLING_ENTITY; no finer dimension exists.
_MARKETPLACE_TOKEN_USAGE = re.compile(
    r"MP:\w+?_(?P<bucket>input_tokens|output_tokens|cache_read_tokens|cache_write_tokens)"
    r"_(?P<tier>[A-Za-z0-9-]+)-Units$"
)
# The PascalCase twin is the Converse-family (Claude) naming. Matched only so a
# run can SAY it saw them — attributing these to a GPT model is the exact
# mistake this guard exists to prevent.
_CONVERSE_TOKEN_USAGE = re.compile(r"MP:\w+?_(?:Cache(?:Read|Write)Input|Input|Output)TokenCount-Units$")

_BUCKET_TO_USAGE_KEY = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_tokens": "cacheReadInputTokens",
    "cache_write_tokens": "cacheWriteInputTokens",
}


def _is_openai_family(model_id: str) -> bool:
    lowered = model_id.lower()
    return "openai" in lowered or "gpt" in lowered


def models_that_ran(table_name: str, region: str, since: str, until: str) -> Dict[str, Dict[str, float]]:
    """Per-model token totals we recorded, for the same window.

    This is the attribution guard. Cost Explorer cannot say which model spent
    the money, so a derived rate is only trustworthy on a day when exactly one
    OpenAI-family model ran.
    """
    import boto3

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    totals: Dict[str, Dict[str, float]] = {}
    kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(GSI_SK, :c) AND #ts BETWEEN :s AND :u",
        "ExpressionAttributeValues": {":c": "C#", ":s": since, ":u": until},
        "ExpressionAttributeNames": {"#ts": "timestamp"},
        "ProjectionExpression": "modelInfo, tokenUsage",
    }
    start_key = None
    while True:
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = table.scan(**kwargs)
        for item in response.get("Items", []):
            info = item.get("modelInfo") or {}
            model_id = info.get("modelId") or info.get("model") or "(unknown)"
            usage = item.get("tokenUsage") or {}
            bucket = totals.setdefault(model_id, {"calls": 0.0})
            bucket["calls"] += 1
            for key in _BUCKET_TO_USAGE_KEY.values():
                try:
                    bucket[key] = bucket.get(key, 0.0) + float(usage.get(key) or 0)
                except (TypeError, ValueError):
                    continue
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break
    return totals


def derive_rates(
    since: str,
    until: Optional[str],
    region: str,
    table_name: Optional[str] = None,
) -> int:
    """Recover $/MTok per bucket from Cost Explorer usage + cost.

    Rate = unblended cost / usage quantity, per usage type. The Price List API
    cannot supply this: there is no Marketplace service code in it at all
    (checked 2026-09-06 — 269 service codes, none for Marketplace), and the
    four Bedrock codes carry no commercial GPT-5.6 rows.

    The unit is read from Cost Explorer's own ``Unit`` field rather than
    assumed. Marketplace token rows report ``1M tokens``; the natively-billed
    Bedrock rows (Nova, Titan, Mantle-served models) report ``1K tokens``. An
    earlier version of this function assumed 1K for everything, which
    overstated every Marketplace rate by 1000x.
    """
    import boto3

    from datetime import date, timedelta

    end = until or (date.today() + timedelta(days=1)).isoformat()
    ce = boto3.client("ce", region_name="us-east-1")
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": since, "End": end},
        Granularity="DAILY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )

    per_day: Dict[str, List[Dict[str, Any]]] = {}
    converse_days: Dict[str, float] = {}
    for period in resp.get("ResultsByTime", []):
        day = period["TimePeriod"]["Start"]
        for group in period.get("Groups", []):
            usage_type = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
            if _CONVERSE_TOKEN_USAGE.search(usage_type):
                converse_days[day] = converse_days.get(day, 0.0) + cost
                continue
            match = _MARKETPLACE_TOKEN_USAGE.search(usage_type)
            if not match or not qty:
                continue
            unit = group["Metrics"]["UsageQuantity"].get("Unit", "")
            per_mtok = _to_per_mtok(cost / qty, unit)
            per_day.setdefault(day, []).append({
                "bucket": match.group("bucket"),
                "tier": match.group("tier"),
                "usage_type": usage_type,
                "qty": qty,
                "unit": unit,
                "cost": cost,
                "per_mtok": per_mtok,
            })

    if not per_day:
        print(
            f"No Marketplace token usage types in Cost Explorer for {since}..{end}.\n"
            "Marketplace line items settle slower than native AWS ones — allow "
            "24-48h, not 24h."
        )
        return 1

    for day in sorted(per_day):
        print(f"\n▸ {day}")
        attribution = _print_attribution(table_name, region, day)
        print(f"   {'bucket':<20}{'tier':<10}{'unit':>12}{'qty':>14}{'cost USD':>11}{'$/MTok':>11}")
        for row in sorted(per_day[day], key=lambda r: r["bucket"]):
            rate = f"{row['per_mtok']:.4f}" if row["per_mtok"] is not None else "unit?"
            print(
                f"   {row['bucket']:<20}{row['tier']:<10}{row['unit']:>12}"
                f"{row['qty']:>14,.6f}{row['cost']:>11.4f}{rate:>11}"
            )
        if converse_days.get(day):
            print(
                f"   (also ${converse_days[day]:.4f} of Converse-family "
                "*TokenCount rows that day — Claude, not GPT; excluded)"
            )
        if attribution is False:
            print(
                "   ⚠️  NOT ATTRIBUTABLE. More than one OpenAI-family model ran "
                "this day and Cost Explorer does not break the buckets down by "
                "model. Re-run the probe on a day when only one model runs."
            )

    print(
        "\nMethod: these rows are model-agnostic, so a rate is only a given "
        "model's rate on a day when that model was the only OpenAI-family "
        "model to run. Check the attribution line above before using a number."
    )
    return 0


def _to_per_mtok(rate_per_unit: float, unit: str) -> Optional[float]:
    """Convert a $/unit rate to $/MTok using Cost Explorer's declared unit."""
    normalized = (unit or "").strip().lower()
    if normalized in ("1m tokens", "1m token"):
        return rate_per_unit
    if normalized in ("1k tokens", "1k token"):
        return rate_per_unit * 1000
    if normalized in ("tokens", "token"):
        return rate_per_unit * 1_000_000
    return None


def _print_attribution(table_name: Optional[str], region: str, day: str) -> Optional[bool]:
    """Print which models we recorded that day. Returns False if ambiguous."""
    if not table_name:
        print("   models that ran: unknown (pass --table to attribute)")
        return None
    from datetime import date, timedelta

    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    try:
        ran = models_that_ran(table_name, region, day, nxt)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"   models that ran: lookup failed ({exc})")
        return None

    openai_models = sorted(m for m in ran if _is_openai_family(m))
    others = sorted(m for m in ran if not _is_openai_family(m))
    if not openai_models:
        print("   models that ran: no OpenAI-family model recorded "
              "(usage may be from a direct-transport probe, which is not recorded)")
    else:
        print(f"   models that ran: {', '.join(openai_models)}")
    if others:
        print(f"   (also non-OpenAI: {', '.join(others)} — billed separately)")
    return len(openai_models) == 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="us.openai.gpt-5.6-sol")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument(
        "--prefix-tokens",
        type=int,
        default=8000,
        help="Approximate size of the stable system prompt. Must exceed the "
        "1024-token minimum cacheable prefix by a wide margin.",
    )
    parser.add_argument("--gap-seconds", type=float, default=3.0)
    parser.add_argument(
        "--mode",
        default="explicit",
        choices=["explicit", "implicit", "both"],
        help="explicit = PR-4 breakpoints on; implicit = stock caching.",
    )
    parser.add_argument("--json-out", default=None)
    parser.add_argument(
        "--history-chunk-tokens",
        type=int,
        default=0,
        help="Pad each history message to roughly this size, so the effect of "
        "history growth on the cache is large enough to read.",
    )
    parser.add_argument(
        "--prefix-salt",
        default="",
        help="Change the static prefix so the run starts against a COLD cache.",
    )
    parser.add_argument(
        "--grow-history",
        action="store_true",
        help="Accumulate conversation history across turns — the churn "
        "scenario explicit breakpoints are meant to protect against.",
    )
    parser.add_argument(
        "--rates-only",
        action="store_true",
        help="Skip the turns; just read Cost Explorer and derive rates.",
    )
    parser.add_argument("--since", default=None, help="YYYY-MM-DD for --rates-only")
    parser.add_argument(
        "--table",
        default=None,
        help="sessions-metadata table, for the --rates-only attribution guard "
        "(e.g. dev-boisestateai-v2-sessions-metadata).",
    )
    parser.add_argument("--until", default=None)
    args = parser.parse_args()

    if args.rates_only:
        if not args.since:
            parser.error("--rates-only requires --since YYYY-MM-DD")
        return derive_rates(args.since, args.until, args.region, args.table)

    modes = ["explicit", "implicit"] if args.mode == "both" else [args.mode]
    approx_calls = args.turns * len(modes)
    print(
        f"About to make {approx_calls} real model calls against {args.model_id} "
        f"with a ~{args.prefix_tokens:,}-token prefix.\n"
        f"Rough worst case if nothing caches: "
        f"~{approx_calls * args.prefix_tokens:,} input tokens."
    )

    results = []
    for mode in modes:
        results.append(
            await run_arm(
                mode,
                model_id=args.model_id,
                region=args.region,
                turns=args.turns,
                prefix_tokens=args.prefix_tokens,
                gap_seconds=args.gap_seconds,
                grow_history=args.grow_history,
                history_chunk_tokens=args.history_chunk_tokens,
                prefix_salt=args.prefix_salt,
            )
        )

    print_verdicts(results)

    grand = {
        "inputTokens": sum(r.totals()["inputTokens"] for r in results),
        "cacheReadInputTokens": sum(r.totals()["cacheReadInputTokens"] for r in results),
        "cacheWriteInputTokens": sum(r.totals()["cacheWriteInputTokens"] for r in results),
        "outputTokens": sum(r.totals()["outputTokens"] for r in results),
    }
    print("\n▸ tokens consumed this run (the denominator for rate derivation)")
    for key, value in grand.items():
        print(f"   {key:<24} {value:>10,}")
    print(
        "\nNext: wait for Cost Explorer to settle (~24h), then\n"
        f"   AWS_PROFILE=dev-ai uv run python {os.path.basename(__file__)} "
        f"--rates-only --since {time.strftime('%Y-%m-%d')}"
    )

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "modelId": args.model_id,
                    "region": args.region,
                    "prefixTokens": args.prefix_tokens,
                    "totals": grand,
                    "arms": [
                        {
                            "mode": r.mode,
                            "error": r.error,
                            "turns": [vars(t) for t in r.turns],
                        }
                        for r in results
                    ],
                },
                fh,
                indent=2,
            )
        print(f"\nRaw observations -> {args.json_out}")

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
